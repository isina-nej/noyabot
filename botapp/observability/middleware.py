"""Best-effort correlation for Django requests and aiogram updates."""
from __future__ import annotations

import time
import uuid

from aiogram import BaseMiddleware

from . import context, logging as obs_logging, metrics


class TelegramMiddleware(BaseMiddleware):
    """Bind one correlation/trace context around each Telegram dispatch."""

    async def __call__(self, handler, event, data):
        update = data.get("event_update")
        update_id = int(getattr(update, "update_id", 0) or 0)
        message_id = int(getattr(event, "message_id", 0) or 0)
        chat = getattr(event, "chat", None)
        user = getattr(event, "from_user", None)
        token = context.correlation_id_var.set(uuid.uuid4().hex[:12])
        trace_token = context.trace_id_var.set(context.new_trace_id())
        try:
            context.bind(
                update_id=update_id,
                message_id=message_id,
                chat_id=getattr(chat, "id", None),
                user_id=getattr(user, "id", None),
                operation="telegram.update",
            )
            metrics.inc(metrics.TELEGRAM_UPDATES_RECEIVED)
            started = time.perf_counter()
            try:
                result = await handler(event, data)
            except Exception as exc:
                metrics.inc(metrics.TELEGRAM_HANDLER_FAILURES, kind=type(exc).__name__)
                obs_logging.emit("telegram.update.failed", level="error", error_type=type(exc).__name__)
                raise
            else:
                metrics.inc(metrics.TELEGRAM_UPDATES_PROCESSED)
                metrics.observe(metrics.TELEGRAM_UPDATE_LATENCY, (time.perf_counter() - started) * 1000)
                return result
        finally:
            context.reset()


class DjangoObservabilityMiddleware:
    """Correlation and bounded timing for private API requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = context.correlation_id_var.set(uuid.uuid4().hex[:12])
        trace_token = context.trace_id_var.set(context.new_trace_id())
        started = time.perf_counter()
        try:
            context.bind(operation=f"http.{request.method.lower()}")
            response = self.get_response(request)
            metrics.observe("noya.http.request_latency_ms", (time.perf_counter() - started) * 1000,
                            method=request.method, status=getattr(response, "status_code", 0))
            return response
        except Exception as exc:
            obs_logging.emit("http.request.failed", level="error", error_type=type(exc).__name__)
            raise
        finally:
            context.reset()
