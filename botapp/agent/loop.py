"""Noya agent loop — Hermes-style tool-calling conversation loop.

Pattern from hermes-agent/agent/conversation_loop.py:
  user msg → LLM with tools → tool_calls? → execute → feed back → loop
  LLM returns text (no tool_calls) → done.
"""
from __future__ import annotations
import json
import logging
import os
import re
from typing import Any

import httpx

from .registry import registry
from .responses import sanitize_outgoing_text

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8  # safety cap — Hermes uses iteration_budget

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

    if tool_calls_map:
        message["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map)]
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "reasoning_chars": reasoning_chars,
    }


async def run_agent_loop(
    question: str,
    session_id: str,
    *,
    system_prompt: str = "",
    conversation_history: list[dict] | None = None,
    speaker_user_id: int | None = None,
    speaker_name: str = "",
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

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Chat loop sees only the chat catalog (conversational + read-only
    # research tools). The admin catalog shares this registry but is
    # never offered to the chat model.
    tools = registry.chat_definitions()

    messages: list[dict[str, Any]] = []
    if system_prompt:
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
    messages.append({"role": "user", "content": question})

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

    for iteration in range(MAX_ITERATIONS):
        payload: dict[str, Any] = {
            "model": tool_model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        logger.info(f"[NOYA-AGENT] Iteration {iteration+1}, model={tool_model}, tools={len(tools)}")

        # Send request with explicit stream=false, but parse SSE anyway
        # because some 9Router combos always return SSE
        payload["stream"] = False
        async with httpx.AsyncClient(timeout=120) as client:
            try:
                resp = await client.post(api_url, json=payload, headers=headers)
                resp.raise_for_status()
            except httpx.ReadTimeout as e:
                logger.warning(f"[NOYA-AGENT] Read timeout from AI provider: {type(e).__name__}")
                return "پاسخ هوش مصنوعی زمان‌بر شد (تایم‌اوت). لطفاً دوباره تلاش کنید.", metadata
            except httpx.TimeoutException as e:
                logger.warning(f"[NOYA-AGENT] Timeout contacting AI provider: {type(e).__name__}")
                return "خطا در اتصال به هوش مصنوعی (تایم‌اوت).", metadata
            except httpx.HTTPStatusError as e:
                logger.warning(f"[NOYA-AGENT] API error status: {e.response.status_code}")
                return f"خطا در ارتباط با هوش مصنوعی ({e.response.status_code})", metadata
            except httpx.RequestError as e:
                logger.warning(f"[NOYA-AGENT] Request error: {type(e).__name__}")
                return "خطا در اتصال به هوش مصنوعی.", metadata

        text = (resp.text or "").strip()
        if not text:
            logger.warning("[NOYA-AGENT] Empty response body from AI provider (status=%s)", resp.status_code)
            return "پاسخی از هوش مصنوعی دریافت نشد.", metadata

        # ── Parse response: try JSON first, then SSE stream ──
        try:
            data = resp.json()
        except Exception:
            # SSE stream format: "data: {json}\n\ndata: {json}\n\n..."
            data = _parse_sse_stream(text)
            if not data:
                logger.error(f"[NOYA-AGENT] Empty/unparseable response: {text[:200]}")
                return "خطا در پردازش پاسخ هوش مصنوعی.", metadata

        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        finish = choice.get("finish_reason", "")

        # Reasoning is never user-visible: record only how much was dropped.
        reasoning_chars = data.get("reasoning_chars") if isinstance(data, dict) else None
        if reasoning_chars is None:
            reasoning_chars = _drop_reasoning(msg)
        if reasoning_chars:
            metadata["reasoning_chars_dropped"] = metadata.get("reasoning_chars_dropped", 0) + reasoning_chars

        # Only ``message.content`` may become user-visible text — a JSON (non
        # SSE) response can carry ``reasoning_content`` too, and it is ignored.
        raw_content = msg.get("content") or ""

        # ── No tool calls → done ──
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
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

            logger.info(f"[NOYA-AGENT] 🔧 Calling tool: {tool_name}({args})")

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
                    result = await registry.dispatch_async(tool_name, args)
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
                    result = await registry.dispatch_async(tool_name, args)
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
                    result = await registry.dispatch_async(tool_name, args)
            else:
                result = await registry.dispatch_async(tool_name, args)

            used_tools.append(tool_name)

            # ── Handle special results via cache ──
            from botapp.agent.tools import _IMAGE_RESULT_CACHE, _TTS_RESULT_CACHE
            if tool_name == "generate_image" and _IMAGE_RESULT_CACHE.get("last"):
                metadata["generated_image_b64"] = _IMAGE_RESULT_CACHE.pop("last")
            if tool_name == "text_to_speech" and _TTS_RESULT_CACHE.get("last"):
                metadata["generated_tts_audio"] = _TTS_RESULT_CACHE.pop("last")

            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": str(result)[:4000]})

        # ── Loop continues — LLM sees tool results ──

    return "ببخشید، زیاد طول کشید. دوباره امتحان کن.", metadata
