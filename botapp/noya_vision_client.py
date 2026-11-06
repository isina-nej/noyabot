"""Bounded Vision provider adapter for archived Telegram photo and animated media.

Neither Telegram file IDs, binary frames, image data URLs, Authorization
headers nor provider response bodies are written to logs. The caller must
enforce Telegram chat/topic/bot authorization *before* passing frames here.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from time import monotonic

import httpx

from botapp.telegram_media import to_data_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisionRead:
    text: str = ""
    reason: str = ""
    model: str = ""
    http_status: int | None = None


def _bounded_timeout() -> float:
    try:
        timeout = float(os.getenv("NOYA_VISION_TIMEOUT_S", "90"))
    except (ValueError, TypeError):
        timeout = 90.0
    return max(15.0, min(150.0, timeout))


def _unpack_vision_content(data) -> tuple[str, str, int]:
    """Use final visible output only, never provider-private reasoning text."""
    if not isinstance(data, dict):
        return "", "invalid_json_shape", 0
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return "", "missing_choices", 0
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        return "", "missing_message", 0
    value = message.get("content")
    if isinstance(value, str):
        visible = value.strip()
    elif isinstance(value, list):
        visible = " ".join(
            part.get("text", "").strip()
            for part in value
            if isinstance(part, dict)
            and part.get("type", "text") in {"text", "output_text"}
            and isinstance(part.get("text"), str)
        ).strip()
    else:
        visible = ""
    usage = data.get("usage") or {}
    details = usage.get("completion_tokens_details") or {} if isinstance(usage, dict) else {}
    reasoning_tokens = (
        int(details.get("reasoning_tokens") or 0)
        if isinstance(details, dict) else 0
    )
    if visible:
        return visible, "", reasoning_tokens
    finish = str(choice.get("finish_reason") or "")[:30]
    return "", ("empty_content_length" if finish == "length" else "empty_visible_content"), reasoning_tokens


def _read_data(response) -> tuple[str, str, int]:
    try:
        data = response.json()
    except (ValueError, TypeError):
        # Some 9Router model combinations send SSE even when stream=false.
        from botapp.agent.loop import _parse_sse_stream
        data = _parse_sse_stream(str(getattr(response, "text", "") or ""))
        if data is None:
            return "", "invalid_json_or_sse", 0
    return _unpack_vision_content(data)


def _vision_request(model: str, instruction: str, frames, *, max_tokens: int, effort: str) -> dict:
    blocks = [{"type": "text", "text": instruction}]
    samples = frames[:3]
    for i, (mime, content) in enumerate(samples):
        if len(samples) > 1:
            blocks.append({"type": "text", "text": f"فریم نمونه {i+1} از {len(samples)}"})
        blocks.append({"type": "image_url", "image_url": {"url": to_data_url(mime, content)}})
    payload = {
        "model": model,
        "stream": False,
        "max_tokens": max(600, min(3200, max_tokens)),
        "messages": [{"role": "user", "content": blocks}],
    }
    if effort in {"none", "low", "medium", "high"}:
        payload["reasoning_effort"] = effort
    return payload


async def read_vision_frames(
    frames: list[tuple[str, bytes]], instruction: str, *,
    max_chars: int = 1600, max_tokens: int = 1700,
) -> VisionRead:
    """Read *visible* content via a configured multimodal model with safe logs.

    On opt-in fallback, a different vision model may be tried on provider or
    empty-output failures; a 401/403 is never retried with the same credential.
    """
    url = (os.getenv("NOYA_API_URL") or "").strip()
    key = (os.getenv("NOYA_API_KEY") or "").strip()
    model = (
        os.getenv("NOYA_VISION_MODEL")
        or os.getenv("NOYA_TOOL_MODEL")
        or os.getenv("NOYA_MODEL") or ""
    ).strip()
    fallback = (os.getenv("NOYA_VISION_FALLBACK_MODEL") or "").strip()
    effort = (os.getenv("NOYA_VISION_REASONING_EFFORT") or "").strip().lower()
    if not url or not key or not model:
        logger.warning("[NOYA-VISION] stage=config result=missing model_configured=%s",
                       bool(model))
        return VisionRead(reason="not_configured", model=model)
    if not frames or len(frames) > 3 or any(
        mime not in ("image/jpeg", "image/png", "image/webp")
        or not isinstance(data, bytes) or not data
        or len(data) > 3_500_000
        for mime, data in frames
    ):
        logger.warning("[NOYA-VISION] stage=input result=invalid")
        return VisionRead(reason="invalid_frames", model=model)

    models = [model]
    if fallback and fallback != model:
        models.append(fallback)
    last = VisionRead(reason="provider_unavailable", model=model)
    async with httpx.AsyncClient(timeout=_bounded_timeout()) as client:
        for index, chosen in enumerate(models):
            started = monotonic()
            payload = _vision_request(chosen, instruction, frames,
                                      max_tokens=max_tokens, effort=effort)
            try:
                response = await client.post(
                    url,
                    headers={"Authorization": "Bearer " + key},
                    json=payload,
                )
                response.raise_for_status()
                content, reason, reasoning = _read_data(response)
                elapsed = (monotonic() - started) * 1000
                if content:
                    logger.info(
                        "[NOYA-VISION] stage=provider result=ok model=%s latency_ms=%.0f "
                        "frames=%d output_chars=%d reasoning_tokens=%d",
                        chosen, elapsed, len(frames), len(content), reasoning,
                    )
                    return VisionRead(text=content[:max(200, min(3500, max_chars))], model=chosen)
                last = VisionRead(reason=reason or "empty_visible_content", model=chosen)
                logger.warning(
                    "[NOYA-VISION] stage=provider result=%s model=%s "
                    "latency_ms=%.0f reasoning_tokens=%d",
                    last.reason, chosen, elapsed, reasoning,
                )
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last = VisionRead(reason=f"http_{status}", model=chosen, http_status=status)
                logger.warning(
                    "[NOYA-VISION] stage=provider result=http_error status=%d "
                    "model=%s latency_ms=%.0f",
                    status, chosen, (monotonic() - started) * 1000,
                )
                if status in (401, 403):
                    return last
            except httpx.TimeoutException as exc:
                last = VisionRead(reason="timeout", model=chosen)
                logger.warning(
                    "[NOYA-VISION] stage=provider result=timeout model=%s error_type=%s "
                    "latency_ms=%.0f",
                    chosen, type(exc).__name__, (monotonic() - started) * 1000,
                )
            except httpx.RequestError as exc:
                last = VisionRead(reason="network_error", model=chosen)
                logger.warning(
                    "[NOYA-VISION] stage=provider result=network_error model=%s error_type=%s",
                    chosen, type(exc).__name__,
                )
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                last = VisionRead(reason="invalid_response", model=chosen)
                logger.warning(
                    "[NOYA-VISION] stage=provider result=invalid_response model=%s error_type=%s",
                    chosen, type(exc).__name__,
                )
            # A configured, distinct backup model is used only once.
            if index == 0 and len(models) > 1:
                logger.info("[NOYA-VISION] fallback_model_attempted=%s", fallback)
    return last
