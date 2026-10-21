"""Deep investigation tool — ReAct harness over read-only agent tools."""

from __future__ import annotations

from botapp.agent.context import AgentContext
from botapp.agent.permissions import ANALYTICS_READ
from botapp.agent.registry import register_tool
from botapp.agent.risk import LOW
from botapp.agent.schemas import EmptyParams


async def investigate(ctx: AgentContext, bot, params) -> str:
    from botapp.harness.bridge import run_application_investigation

    result = await run_application_investigation(
        ctx=ctx,
        bot=bot,
        user_text=ctx.command_text or "بررسی کامل",
    )
    # The harness "finish" answer is raw model prose: run it through the same
    # outbound sanitizer as the chat path before it reaches Telegram.
    from botapp.agent.responses import sanitize_outgoing_text

    answer = sanitize_outgoing_text(result.answer or "")
    if answer:
        return answer
    return "❌ بررسی مرحله‌ای ناموفق بود."


register_tool(
    name="harness.investigate",
    description=(
        "بررسی مرحله‌ای (ReAct): چند ابزار خواندنی را پشت‌سرهم صدا می‌زند، "
        "در صورت نیاز روی داده‌ها کد محدود اجرا می‌کند و پاسخ فارسی نهایی می‌دهد. "
        "برای تحلیل عمیق / بررسی کامل / تحقیق."
    ),
    input_schema=EmptyParams,
    permission=ANALYTICS_READ,
    risk_level=LOW,
    requires_confirmation=False,
    handler=investigate,
    human_verb="بررسی مرحله‌ای",
)
