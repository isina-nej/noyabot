"""Noya agent loop — Hermes-style tool-calling conversation loop.

Pattern from hermes-agent/agent/conversation_loop.py:
  user msg → LLM with tools → tool_calls? → execute → feed back → loop
  LLM returns text (no tool_calls) → done.
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any

import httpx

from .registry import registry

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 8  # safety cap — Hermes uses iteration_budget


def _parse_sse_stream(raw: str) -> dict | None:
    """Parse SSE stream response, reassemble chunks into a single message dict."""
    import json as _json
    message: dict = {"role": "assistant", "content": ""}
    tool_calls_map: dict[int, dict] = {}
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
        # reasoning (for thinking models)
        rc = delta.get("reasoning_content")
        if rc:
            message["content"] = (message.get("content") or "") + rc
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
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


async def run_agent_loop(
    question: str,
    session_id: str,
    *,
    system_prompt: str = "",
    conversation_history: list[dict] | None = None,
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

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    tools = registry.definitions()

    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": question})

    metadata: dict[str, Any] = {}
    used_tools: list[str] = []

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
            except httpx.HTTPStatusError as e:
                logger.error(f"[NOYA-AGENT] API error: {e.response.status_code}")
                return f"خطا در ارتباط با هوش مصنوعی ({e.response.status_code})", metadata
            except httpx.RequestError as e:
                logger.error(f"[NOYA-AGENT] Request error: {e}")
                return "خطا در اتصال به هوش مصنوعی.", metadata

        text = resp.text

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

        # ── No tool calls → done ──
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            final_text = msg.get("content", "") or ""
            if used_tools:
                metadata["tools_used"] = used_tools
            return final_text, metadata

        # ── Execute tool calls (parallel like Hermes) ──
        messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": tool_calls})

        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            logger.info(f"[NOYA-AGENT] 🔧 Calling tool: {tool_name}({args})")
            used_tools.append(tool_name)

            result = await registry.dispatch_async(tool_name, args)

            # ── Handle special results via cache ──
            from botapp.agent.tools import _IMAGE_RESULT_CACHE, _TTS_RESULT_CACHE
            if tool_name == "generate_image" and _IMAGE_RESULT_CACHE.get("last"):
                metadata["generated_image_b64"] = _IMAGE_RESULT_CACHE.pop("last")
            if tool_name == "text_to_speech" and _TTS_RESULT_CACHE.get("last"):
                metadata["generated_tts_audio"] = _TTS_RESULT_CACHE.pop("last")

            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": str(result)[:4000]})

        # ── Loop continues — LLM sees tool results ──

    return "ببخشید، زیاد طول کشید. دوباره امتحان کن.", metadata
