"""Noya agent loop — Hermes-style tool-calling conversation loop.

Pattern from hermes-agent/agent/conversation_loop.py:
  user msg → LLM with tools → tool_calls? → execute → feed back → loop
  LLM returns text (no tool_calls) → done.
"""
from __future__ import annotations
import json
import logging
import math
import os
import re
import time
from typing import Any

import httpx
from botapp.observability.instrumentation import (
    agent_tool_dispatch,
    ai_provider_request_begin,
    ai_provider_request_end,
    ai_response_extracted,
    ai_sse_malformed,
    instrument_async,
)

from .registry import registry
from .responses import sanitize_outgoing_text
from .tools import isolated_media_results, pop_media_result, pop_chat_outbox, pop_media_sequence, pop_reaction_request

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8  # safety cap — Hermes uses iteration_budget


def _bounded_float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    """Read a per-request latency setting, rejecting NaN, infinity and bad values."""
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value):
            raise ValueError("non-finite value")
    except (TypeError, ValueError):
        logger.warning("[NOYA-AGENT] Invalid %s; using default", name)
        return default
    return max(minimum, min(value, maximum))


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Bound the generation budget even if an environment setting is malformed."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("[NOYA-AGENT] Invalid %s; using default", name)
        return default
    return max(minimum, min(value, maximum))

#: Provider fields that carry *internal* reasoning, never user-facing text.
#: Some 9Router/OpenAI-compatible backends put their planning notes
#: (``→ skipped: … add when: …``) in these fields; merging them into
#: ``content`` leaks them straight into the Telegram chat.
REASONING_FIELDS = ("reasoning_content", "reasoning", "thinking")


def _drop_reasoning(source: dict) -> int:
    """Return the character count of reasoning in *source* — never its text."""
    total = 0
    for field in REASONING_FIELDS:
        value = (source or {}).get(field)
        if isinstance(value, str):
            total += len(value)
    return total


def _parse_sse_stream(raw: str) -> dict | None:
    """Parse SSE stream response, reassemble chunks into a single message dict.

    Only ``delta.content`` is ever used for the assistant message.  Provider
    reasoning (``delta.reasoning_content`` / ``delta.reasoning`` /
    ``delta.thinking``) is dropped entirely; only its character count is
    reported back, under the top-level ``reasoning_chars`` key.
    """
    import json as _json
    message: dict = {"role": "assistant", "content": ""}
    tool_calls_map: dict[int, dict] = {}
    reasoning_chars = 0
    saw_valid_chunk = False
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            chunk = _json.loads(payload)
        except _json.JSONDecodeError:
            continue
        choices = chunk.get("choices", [])
        if not choices:
            continue
        saw_valid_chunk = True
        delta = choices[0].get("delta", {})
        # content
        c = delta.get("content")
        if c:
            message["content"] = (message.get("content") or "") + c
        # Never merge provider reasoning into the user-visible answer.  Some
        # 9Router/SSE responses place internal planning notes (including
        # ``skipped`` annotations) in ``reasoning_content`` / ``reasoning`` /
        # ``thinking``; none of it is part of the assistant message, so only a
        # character count is kept for observability.
        reasoning_chars += _drop_reasoning(delta)
        # tool calls
        for tc_delta in delta.get("tool_calls", []):
            idx = tc_delta.get("index", 0)
            if idx not in tool_calls_map:
                tool_calls_map[idx] = {
                    "id": tc_delta.get("id", ""),
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
            tc = tool_calls_map[idx]
            fn = tc_delta.get("function", {})
            if fn.get("name"):
                tc["function"]["name"] = fn["name"]
            if fn.get("arguments"):
                tc["function"]["arguments"] += fn["arguments"]
            if tc_delta.get("id"):
                tc["id"] = tc_delta["id"]

    if not saw_valid_chunk:
        return None
    if tool_calls_map:
        message["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map)]
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "reasoning_chars": reasoning_chars,
    }


async def _dispatch_observed(tool_name: str, args: dict):
    started = time.perf_counter()
    try:
        result = await registry.dispatch_async(tool_name, args)
    except Exception:
        agent_tool_dispatch(
            tool=tool_name,
            duration_ms=(time.perf_counter() - started) * 1000,
            status="error",
        )
        raise
    agent_tool_dispatch(
        tool=tool_name,
        duration_ms=(time.perf_counter() - started) * 1000,
        status="ok",
    )
    return result


@instrument_async("agent.loop")
@isolated_media_results
async def run_agent_loop(
    question: str,
    session_id: str,
    *,
    system_prompt: str = "",
    conversation_history: list[dict] | None = None,
    speaker_user_id: int | None = None,
    speaker_name: str = "",
    images: list[dict] | None = None,
    social_context: dict | None = None,
    reaction_allowed: bool = False,
    silence_allowed: bool = False,
    telegram_context: dict | None = None,
    on_token=None,
) -> tuple[str, dict]:
    """Run the Hermes-style agent loop.

    Returns (final_text, metadata) where metadata may contain
    'generated_image_b64', 'generated_tts_audio', etc.
    """
    api_key = os.getenv("NOYA_API_KEY", "").strip()
    api_url = os.getenv("NOYA_API_URL", "http://127.0.0.1:20128/v1/chat/completions").strip()
    model = os.getenv("NOYA_MODEL", "FastText").strip()
    tool_model = os.getenv("NOYA_TOOL_MODEL", model).strip()
    if images:
        tool_model = os.getenv("NOYA_VISION_MODEL", "").strip() or tool_model

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Chat loop sees only the chat catalog (conversational + read-only
    # research tools). The admin catalog shares this registry but is
    # never offered to the chat model.
    tools = registry.chat_definitions()
    # Keep slow or misconfigured providers from blocking a Telegram reply for
    # five minutes. A per-run client also reuses its connection for tool rounds.
    provider_timeout_s = _bounded_float_env(
        "NOYA_AGENT_TIMEOUT_S", 90.0, minimum=10.0, maximum=300.0,
    )
    max_completion_tokens = _bounded_int_env(
        "NOYA_CHAT_MAX_TOKENS", 2048, minimum=128, maximum=8192,
    )
    # Optional: the router/model may honor reasoning_effort differently.
    # Disabled by default to preserve the currently deployed behavior.
    reasoning_effort = os.getenv("NOYA_AGENT_REASONING_EFFORT", "").strip().lower()
    if reasoning_effort and reasoning_effort not in {"none", "low", "medium", "high"}:
        logger.warning("[NOYA-AGENT] Invalid NOYA_AGENT_REASONING_EFFORT; ignoring")
        reasoning_effort = ""

    messages: list[dict[str, Any]] = []
    if system_prompt:
        if reaction_allowed:
            from botapp.noya_reactions import reaction_instruction
            system_prompt += "\n\n" + reaction_instruction()
        if silence_allowed:
            from botapp.noya_silence import silence_instruction
            system_prompt += "\n\n" + silence_instruction()
        from botapp.noya_social_roles import verified_social_instruction
        social_block = verified_social_instruction(social_context)
        if social_block:
            system_prompt += "\n\n" + social_block
        if speaker_user_id is not None:
            from botapp.ai.prompts import build_speaker_block
            speaker_block = build_speaker_block(
                speaker_user_id=speaker_user_id,
                speaker_name=speaker_name,
            )
            if speaker_block:
                system_prompt = f"{system_prompt}\n\n{speaker_block}"
        messages.append({"role": "system", "content": system_prompt})
    if conversation_history:
        messages.extend(conversation_history)
    # Keep vision payload inside the request that owns it (no shared cache).
    user_content: str | list[dict] = question
    if images:
        from botapp.telegram_media import images_to_data_urls
        urls = images_to_data_urls(images[:3])
        if urls:
            user_content = [{"type": "text", "text": question}]
            for url, img in zip(urls, images[:3]):
                if img.get("animation_preview_only"):
                    user_content.append({
                        "type": "text",
                        "text": "این فقط یک نمای ثابت است. اگر صرفاً استیکر فرستاده شده، واکنش کوتاه و مرتبط با حال‌وهوای همین نما بده؛ بدون درخواست صریح، درباره فریم یا فرمت توضیح نده و حرکت دیده‌نشده را حدس نزن.",
                    })
                if img.get("frame_index"):
                    user_content.append({
                        "type": "text",
                        "text": f"فریم {img['frame_index']} از {img['frame_count']} "
                                f"(منبع: {img.get('source', 'message')})، به ترتیب زمانی:",
                    })
                user_content.append({"type": "image_url", "image_url": {"url": url}})
    messages.append({"role": "user", "content": user_content})

    metadata: dict[str, Any] = {}
    used_tools: list[str] = []
    seen_search_queries: set[str] = set()
    seen_fetch_urls: set[str] = set()
    seen_research_queries: set[str] = set()
    search_count = 0
    fetch_count = 0
    research_count = 0
    MAX_SEARCHES = 3
    MAX_FETCHES = 6
    MAX_RESEARCH_CALLS = 4

    async with httpx.AsyncClient(timeout=provider_timeout_s) as client:
        for iteration in range(MAX_ITERATIONS):
            payload: dict[str, Any] = {
                "model": tool_model,
                "messages": messages,
                "max_tokens": max_completion_tokens,
            }
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            if reasoning_effort:
                payload["reasoning_effort"] = reasoning_effort

            logger.info(f"[NOYA-AGENT] Iteration {iteration+1}, model={tool_model}, tools={len(tools)}")

            # Send request with explicit stream=false, but parse SSE anyway
            # because some 9Router combos always return SSE
            payload["stream"] = False
            request_started = time.perf_counter()
            ai_provider_request_begin(tool_model)
            try:
                resp = await client.post(api_url, json=payload, headers=headers)
                resp.raise_for_status()
            except httpx.ReadTimeout as e:
                ai_provider_request_end(duration_ms=(time.perf_counter() - request_started) * 1000,
                                        status="timeout", model=tool_model, timed_out=True)
                logger.warning(f"[NOYA-AGENT] Read timeout from AI provider: {type(e).__name__}")
                return "پاسخ هوش مصنوعی زمان‌بر شد (تایم‌اوت). لطفاً دوباره تلاش کنید.", metadata
            except httpx.TimeoutException as e:
                ai_provider_request_end(duration_ms=(time.perf_counter() - request_started) * 1000,
                                        status="timeout", model=tool_model, timed_out=True)
                logger.warning(f"[NOYA-AGENT] Timeout contacting AI provider: {type(e).__name__}")
                return "خطا در اتصال به هوش مصنوعی (تایم‌اوت).", metadata
            except httpx.HTTPStatusError as e:
                ai_provider_request_end(duration_ms=(time.perf_counter() - request_started) * 1000,
                                        status=f"http_{e.response.status_code}", model=tool_model,
                                        http_status=e.response.status_code)
                logger.warning(f"[NOYA-AGENT] API error status: {e.response.status_code}")
                return f"خطا در ارتباط با هوش مصنوعی ({e.response.status_code})", metadata
            except httpx.RequestError as e:
                ai_provider_request_end(duration_ms=(time.perf_counter() - request_started) * 1000,
                                        status="request_error", model=tool_model)
                logger.warning(f"[NOYA-AGENT] Request error: {type(e).__name__}")
                return "خطا در اتصال به هوش مصنوعی.", metadata

            text = (resp.text or "").strip()
            if not text:
                ai_provider_request_end(duration_ms=(time.perf_counter() - request_started) * 1000,
                                        status="malformed", model=tool_model,
                                        http_status=resp.status_code)
                logger.warning("[NOYA-AGENT] Empty response body from AI provider (status=%s)", resp.status_code)
                return "پاسخی از هوش مصنوعی دریافت نشد.", metadata

            # ── Parse response: try JSON first, then SSE stream ──
            try:
                data = resp.json()
            except Exception:
                # SSE stream format: "data: {json}\n\ndata: {json}\n\n..."
                data = _parse_sse_stream(text)
                if not data:
                    ai_provider_request_end(
                        duration_ms=(time.perf_counter() - request_started) * 1000,
                        status="malformed", model=tool_model, http_status=resp.status_code,
                    )
                    ai_sse_malformed(api_url)
                    logger.error("[NOYA-AGENT] Unparseable provider response status=%s bytes=%d", resp.status_code, len(text))
                    return "خطا در پردازش پاسخ هوش مصنوعی.", metadata

            if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
                ai_provider_request_end(
                    duration_ms=(time.perf_counter() - request_started) * 1000,
                    status="invalid_response", model=tool_model, http_status=resp.status_code,
                )
                logger.warning("[NOYA-AGENT] AI provider returned no choices")
                return "خطا در پردازش پاسخ هوش مصنوعی.", metadata

            choice = data["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                ai_provider_request_end(
                    duration_ms=(time.perf_counter() - request_started) * 1000,
                    status="invalid_response", model=tool_model, http_status=resp.status_code,
                )
                logger.warning("[NOYA-AGENT] AI provider returned invalid message")
                return "خطا در پردازش پاسخ هوش مصنوعی.", metadata

            msg = choice["message"]
            finish = choice.get("finish_reason", "")
            usage = data.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            completion_details = usage.get("completion_tokens_details") or {}
            if not isinstance(completion_details, dict):
                completion_details = {}
            duration_ms = (time.perf_counter() - request_started) * 1000
            ai_provider_request_end(
                duration_ms=duration_ms, status="ok", model=tool_model,
                http_status=resp.status_code,
                tokens_in=usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), (int, float)) else None,
                tokens_out=usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), (int, float)) else None,
            )
            logger.info(
                "[NOYA-PROVIDER] model=%s iteration=%d latency_ms=%.1f "
                "prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s finish_reason=%s",
                tool_model, iteration + 1, duration_ms,
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
                completion_details.get("reasoning_tokens"), finish,
            )
            if finish == "length":
                metadata["response_truncated"] = True
                # A truncated tool-call payload may contain incomplete arguments.
                # Never execute partial side effects on the basis of that payload.
                if msg.get("tool_calls") or not (msg.get("content") or "").strip():
                    logger.warning("[NOYA-AGENT] Provider hit completion budget before a usable reply")
                    return "پاسخ مدل به سقف طول رسید؛ لطفاً درخواست را کوتاه‌تر بفرستید.", metadata

            # Reasoning is never user-visible: record only how much was dropped.
            reasoning_chars = data.get("reasoning_chars") if isinstance(data, dict) else None
            if reasoning_chars is None:
                reasoning_chars = _drop_reasoning(msg)
            if reasoning_chars:
                metadata["reasoning_chars_dropped"] = metadata.get("reasoning_chars_dropped", 0) + reasoning_chars

            # Only ``message.content`` may become user-visible text — a JSON (non
            # SSE) response can carry ``reasoning_content`` too, and it is ignored.
            raw_content = msg.get("content") or ""
            ai_response_extracted(
                content_chars=len(raw_content) if isinstance(raw_content, str) else 0,
                reasoning_chars=int(reasoning_chars or 0),
            )

            # ── No tool calls → done ──
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                if silence_allowed and not used_tools and isinstance(raw_content, str):
                    from botapp.noya_silence import silent_token
                    if silent_token(raw_content, allowed=True):
                        metadata["no_reply"] = True
                        return "", metadata
                if reaction_allowed and not used_tools and isinstance(raw_content, str):
                    from botapp.noya_reactions import extract_reaction
                    emoji = extract_reaction(raw_content, allowed=True)
                    if emoji:
                        metadata["reaction_emoji"] = emoji
                        return "", metadata
                final_text = sanitize_outgoing_text(raw_content)
                if not final_text:
                    # Never fall back to the raw text: it may be nothing but an
                    # internal planning annotation.
                    metadata["sanitized_empty"] = True
                    if raw_content.strip():
                        logger.warning(
                            "[NOYA-AGENT] Reply was entirely an internal annotation (%d chars); suppressed",
                            len(raw_content),
                        )
                if used_tools:
                    metadata["tools_used"] = used_tools
                queued = pop_chat_outbox()
                if queued:
                    metadata["queued_chat_messages"] = queued
                media_sequence = pop_media_sequence()
                if media_sequence:
                    metadata["queued_media_sequence"] = media_sequence
                reaction = pop_reaction_request()
                if reaction:
                    metadata["queued_reaction"] = reaction
                return final_text, metadata

            # ── Execute tool calls (parallel like Hermes) ──
            # The assistant turn pushed back into the conversation is sanitized as
            # well, so a leaked note cannot be re-primed into the next iteration.
            # Tool results stay raw: tool output is data, not outbound text.
            messages.append({
                "role": "assistant",
                "content": sanitize_outgoing_text(raw_content),
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}

                logger.info("[NOYA-AGENT] Calling tool=%s arg_keys=%s", tool_name,
                            sorted(args) if isinstance(args, dict) else [])

                # ── Loop & Budget Deduplication Guards ──
                if tool_name == "web_search":
                    raw_q = (args.get("query") or "").strip()
                    q = re.sub(r"\s+", " ", raw_q).casefold()
                    if q in seen_search_queries:
                        result = "این جستجو قبلاً با همین عبارت انجام شده است؛ لطفاً بر اساس اطلاعات بالا پاسخ را ادامه دهید."
                    elif search_count >= MAX_SEARCHES:
                        result = "سقف تعداد جستجو در این مکالمه تکمیل شده است؛ لطفاً بر اساس اطلاعات موجود پاسخ دهید."
                    else:
                        seen_search_queries.add(q)
                        search_count += 1
                        result = await _dispatch_observed(tool_name, args)
                elif tool_name == "fetch_url":
                    from botapp.web.ranking import canonicalize_url
                    u = canonicalize_url(args.get("url") or "")
                    if u in seen_fetch_urls:
                        result = "این صفحه قبلاً دریافت شده است؛ لطفاً از محتوای بالا استفاده کنید."
                    elif fetch_count >= MAX_FETCHES:
                        result = "سقف تعداد دریافت صفحات وب تکمیل شده است؛ لطفاً بر اساس اطلاعات موجود پاسخ دهید."
                    else:
                        seen_fetch_urls.add(u)
                        fetch_count += 1
                        result = await _dispatch_observed(tool_name, args)
                elif tool_name in (
                    "research.social_search", "x.search_posts", "x.get_post",
                    "x.get_thread", "x.get_user", "x.get_user_posts",
                    "x.get_replies", "youtube.search", "youtube.get_video",
                    "youtube.get_channel", "youtube.get_comments",
                    "youtube.get_playlist", "youtube.get_transcript",
                ):
                    raw_q = (args.get("query") or args.get("post_id")
                             or args.get("video_id") or args.get("handle")
                             or args.get("channel_id") or args.get("playlist_id") or "").strip()
                    norm_q = re.sub(r"\s+", " ", raw_q).casefold()
                    q = tool_name + ":" + norm_q
                    if q in seen_research_queries:
                        result = "این تحقیق قبلاً با همین ورودی انجام شده است؛ لطفاً بر اساس اطلاعات بالا پاسخ را ادامه دهید."
                    elif research_count >= MAX_RESEARCH_CALLS:
                        result = "سقف تعداد تحقیق (X/YouTube/اجتماعی) در این مکالمه تکمیل شده است؛ لطفاً بر اساس اطلاعات موجود پاسخ دهید."
                    else:
                        seen_research_queries.add(q)
                        research_count += 1
                        result = await _dispatch_observed(tool_name, args)
                else:
                    result = await _dispatch_observed(tool_name, args)

                used_tools.append(tool_name)

                # This result is owned by this agent task, never another user's
                # concurrent request. Tool-call IDs remain in the LLM transcript.
                if tool_name in ("generate_image", "edit_image"):
                    image_b64 = pop_media_result("image")
                    if image_b64:
                        metadata["generated_image_b64"] = image_b64
                if tool_name == "text_to_speech":
                    audio_bytes = pop_media_result("tts")
                    if audio_bytes:
                        metadata["generated_tts_audio"] = audio_bytes

                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": str(result)[:4000]})

            # ── Loop continues — LLM sees tool results ──

        return "ببخشید، زیاد طول کشید. دوباره امتحان کن.", metadata
