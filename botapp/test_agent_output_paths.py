"""End-to-end regression tests for the agent output paths.

P0 bug under test: Noya delivered provider *internal* planning annotations to
Telegram users, e.g.::

    → skipped: پیاده‌سازی کامل نمونه کدهای موازی، add when نیاز به بنچمارک بود.

These tests drive the real pipelines (``run_agent_loop``, ``call_noya_api``,
``_call_noya_api_direct``) against fake provider responses: SSE and plain JSON,
with and without a tool-call round.  No network, no sleeps.
"""

import json
import os
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.agent.loop import run_agent_loop
from botapp.services import _call_noya_api_direct, call_noya_api

# The exact shape of the leaked note (SSE ``content`` / JSON ``content``).
NOTE_AR = "→ skipped: پیاده‌سازی کامل نمونه کدهای موازی، add when نیاز به بنچمارک بود."
NOTE_EN = "→ skipped: enthusiasm, add when: funny."
REASONING_NOTE = "→ skipped: internal plan, add when the user asks twice."
HUMAN = "سلام! خوبی؟ من نویا هستم."
EMPTY_REPLY = "پاسخی از هوش مصنوعی دریافت نشد."


# ── fake provider transport ──────────────────────────────────────────────────


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` as used by the agent loop."""

    def __init__(self, *, body=None, text=None, sse=False, status_code=200):
        self.status_code = status_code
        if sse:
            self._text = text if text is not None else ""
            self._json = None
        else:
            self._json = body
            self._text = text if text is not None else json.dumps(body, ensure_ascii=False)

    @property
    def text(self):
        return self._text

    def json(self):
        if self._json is None:
            # httpx raises on a non-JSON body; the loop must fall back to SSE.
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        return None


def _json_body(content, *, reasoning_content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


def _sse(*chunks):
    """Render provider chunks as an SSE stream body."""
    lines = []
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False))
        lines.append("")
    lines.append("data: [DONE]")
    return "\n".join(lines)


def _delta(**fields):
    return {"choices": [{"delta": fields}]}


async def _agent(**kwargs):
    kwargs.setdefault("question", "سلام، چطوری؟")
    kwargs.setdefault("session_id", "sess-1")
    return await run_agent_loop(**kwargs)


def _assert_no_note(case, text):
    case.assertNotIn("skipped", text)
    case.assertNotIn("add when", text)
    case.assertNotIn("reasoning", text.lower())
    case.assertNotIn("internal plan", text)


def _assert_metadata_clean(case, metadata):
    """Metadata may carry a reasoning *length*, never reasoning text."""
    blob = json.dumps({k: v for k, v in (metadata or {}).items() if k != "reasoning_chars_dropped"},
                      ensure_ascii=False, default=str)
    case.assertNotIn("skipped", blob)
    case.assertNotIn("add when", blob)
    case.assertNotIn("internal plan", blob)


# ── 1. plain JSON provider response ─────────────────────────────────────────


class PlainJsonPathTests(SimpleTestCase):
    def test_json_content_note_is_stripped_human_text_intact(self):
        body = _json_body(f"{HUMAN}\n{NOTE_AR}\nبدرود")
        fake = _FakeResponse(body=body)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)) as post:
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(post.await_count, 1)
        self.assertEqual(text, f"{HUMAN}\nبدرود")
        _assert_no_note(self, text)
        _assert_no_note(self, json.dumps(metadata, ensure_ascii=False))

    def test_json_reasoning_content_is_never_merged(self):
        body = _json_body(HUMAN, reasoning_content=REASONING_NOTE)
        fake = _FakeResponse(body=body)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, HUMAN)
        _assert_no_note(self, text)
        _assert_metadata_clean(self, metadata)

    def test_json_reasoning_only_reply_yields_empty_text(self):
        body = _json_body("", reasoning_content=REASONING_NOTE)
        fake = _FakeResponse(body=body)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, "")
        self.assertTrue(metadata.get("sanitized_empty"))
        _assert_metadata_clean(self, metadata)

    def test_json_note_only_reply_returns_empty_not_the_note(self):
        body = _json_body(NOTE_EN)
        fake = _FakeResponse(body=body)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, "")
        self.assertTrue(metadata.get("sanitized_empty"))
        _assert_no_note(self, text)


# ── 2. SSE provider response ────────────────────────────────────────────────


class SseReasoningPathTests(SimpleTestCase):
    def test_sse_reasoning_content_never_reaches_output(self):
        stream = _sse(
            _delta(reasoning_content=REASONING_NOTE),
            _delta(content=HUMAN),
            _delta(reasoning_content=" more private planning "),
        )
        fake = _FakeResponse(sse=True, text=stream)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, HUMAN)
        _assert_no_note(self, text)
        _assert_metadata_clean(self, metadata)

    def test_sse_reasoning_and_thinking_fields_are_ignored(self):
        stream = _sse(
            _delta(reasoning=REASONING_NOTE),
            _delta(thinking=REASONING_NOTE),
            _delta(content=HUMAN),
        )
        fake = _FakeResponse(sse=True, text=stream)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, HUMAN)
        _assert_no_note(self, text)
        _assert_metadata_clean(self, metadata)
        self.assertEqual(metadata.get("reasoning_chars_dropped"), 2 * len(REASONING_NOTE))


class SseContentPathTests(SimpleTestCase):
    def test_sse_single_line_note_in_content_is_stripped(self):
        stream = _sse(
            _delta(content=HUMAN + "\n"),
            _delta(content=NOTE_AR),
            _delta(content="\nبدرود"),
        )
        fake = _FakeResponse(sse=True, text=stream)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, f"{HUMAN}\nبدرود")
        _assert_no_note(self, text)

    def test_sse_multiline_notes_in_content_are_stripped(self):
        stream = _sse(
            _delta(content="خط اول\n"),
            _delta(content="⇒ skipped: one\n• note: two\n"),
            _delta(content="رد شد: سه\n"),
            _delta(content="خط آخر"),
        )
        fake = _FakeResponse(sse=True, text=stream)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, "خط اول\nخط آخر")
        _assert_no_note(self, text)


# ── 3. fenced code must survive ─────────────────────────────────────────────


FENCED = (
    "این نمونه کد را ببین:\n"
    "```python\n"
    "# → skipped: keep this comment, add when: never\n"
    "print('سلام')\n"
    "```\n"
    "→ skipped: پیاده‌سازی کامل، add when نیاز به تست بود.\n"
    "تمام."
)


class FencePreservationPathTests(SimpleTestCase):
    def test_fenced_code_is_byte_identical_while_sibling_note_is_removed(self):
        body = _json_body(FENCED)
        fake = _FakeResponse(body=body)
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
            text, metadata = async_to_sync(_agent)()

        fenced_block = (
            "```python\n"
            "# → skipped: keep this comment, add when: never\n"
            "print('سلام')\n"
            "```"
        )
        self.assertIn(fenced_block, text)
        self.assertNotIn("پیاده‌سازی کامل", text)
        self.assertTrue(text.startswith("این نمونه کد را ببین:"))
        self.assertTrue(text.endswith("تمام."))


# ── 4. tool-call round trip ─────────────────────────────────────────────────


TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_time", "arguments": "{}"},
}


class ToolLoopPathTests(SimpleTestCase):
    def test_note_after_tool_call_is_stripped_and_continuation_is_sanitized(self):
        first = _FakeResponse(body=_json_body(NOTE_EN, tool_calls=[TOOL_CALL]))
        second = _FakeResponse(body=_json_body(f"{HUMAN}\n{NOTE_AR}\nبدرود"))
        post = AsyncMock(side_effect=[first, second])
        dispatch = AsyncMock(return_value="۱۰:۳۰")
        with patch("httpx.AsyncClient.post", new=post), patch(
            "botapp.agent.registry.registry.dispatch_async", new=dispatch
        ):
            text, metadata = async_to_sync(_agent)()

        # the tool was actually reached, exactly once
        self.assertEqual(dispatch.await_count, 1)
        self.assertEqual(dispatch.await_args.args[0], "get_time")

        # final text is clean, human part intact
        self.assertEqual(text, f"{HUMAN}\nبدرود")
        _assert_no_note(self, text)
        self.assertEqual(metadata.get("tools_used"), ["get_time"])

        # the assistant turn fed back into the model was sanitized …
        self.assertEqual(post.await_count, 2)
        continuation = post.await_args_list[1].kwargs["json"]["messages"]
        assistant_turns = [m for m in continuation if m.get("role") == "assistant"]
        self.assertEqual(len(assistant_turns), 1)
        self.assertEqual(assistant_turns[0]["content"], "")
        self.assertEqual(assistant_turns[0]["tool_calls"], [TOOL_CALL])
        for message in continuation:
            if message.get("role") == "assistant":
                _assert_no_note(self, json.dumps(message["content"], ensure_ascii=False))

        # … tool output stays untouched (it is data, not outbound text)
        tool_turns = [m for m in continuation if m.get("role") == "tool"]
        self.assertEqual(tool_turns, [{"role": "tool", "tool_call_id": "call_1", "content": "۱۰:۳۰"}])

    def test_tool_round_that_ends_with_only_a_note_returns_empty(self):
        first = _FakeResponse(body=_json_body(NOTE_EN, tool_calls=[TOOL_CALL]))
        second = _FakeResponse(body=_json_body(NOTE_AR))
        post = AsyncMock(side_effect=[first, second])
        dispatch = AsyncMock(return_value="ok")
        with patch("httpx.AsyncClient.post", new=post), patch(
            "botapp.agent.registry.registry.dispatch_async", new=dispatch
        ):
            text, metadata = async_to_sync(_agent)()
        self.assertEqual(text, "")
        self.assertTrue(metadata.get("sanitized_empty"))


# ── 5. call_noya_api (agent path) ───────────────────────────────────────────


class CallNoyaApiPathTests(SimpleTestCase):
    def _call(self):
        async def _go():
            return await call_noya_api("سلام، چطوری؟", "sess-1")

        return async_to_sync(_go)()

    def test_agent_reply_is_sanitized_before_delivery(self):
        agent_reply = (f"{HUMAN}\n{NOTE_AR}", {"tools_used": []})
        with patch("botapp.agent.loop.run_agent_loop", new=AsyncMock(return_value=agent_reply)), patch(
            "botapp.services.is_clock_question", return_value=False
        ), patch("botapp.ai.prompts.get_noya_system_prompt", return_value="sys"):
            reply, metadata = self._call()
        self.assertEqual(reply, HUMAN)
        _assert_no_note(self, reply)
        self.assertEqual(metadata, {"tools_used": []})

    def test_note_only_agent_reply_falls_back_to_persian_error(self):
        for agent_reply in ((NOTE_AR, {}), ("", {})):
            with self.subTest(agent_reply=agent_reply):
                with patch(
                    "botapp.agent.loop.run_agent_loop", new=AsyncMock(return_value=agent_reply)
                ), patch("botapp.services.is_clock_question", return_value=False), patch(
                    "botapp.ai.prompts.get_noya_system_prompt", return_value="sys"
                ):
                    reply, metadata = self._call()
                self.assertEqual(reply, EMPTY_REPLY)
                self.assertEqual(metadata, {})


# ── 6. legacy direct path ───────────────────────────────────────────────────


class DirectApiPathTests(SimpleTestCase):
    def test_direct_json_note_is_removed(self):
        body = _json_body(f"{HUMAN}\n{NOTE_AR}")
        fake = _FakeResponse(body=body)
        with patch.dict(os.environ, {"NOYA_API_KEY": "test-key"}, clear=False), patch(
            "httpx.AsyncClient.post", new=AsyncMock(return_value=fake)
        ), patch(
            "botapp.services.maybe_web_search", new=AsyncMock(return_value="")
        ), patch(
            "botapp.services.build_ai_messages", return_value=[{"role": "user", "content": "hi"}]
        ):
            reply, metadata = async_to_sync(_call_direct)()
        self.assertEqual(reply, HUMAN)
        self.assertEqual(metadata, {})

    def test_direct_note_only_reply_returns_fallback_string(self):
        fake = _FakeResponse(body=_json_body(NOTE_EN))
        with patch.dict(os.environ, {"NOYA_API_KEY": "test-key"}, clear=False), patch(
            "httpx.AsyncClient.post", new=AsyncMock(return_value=fake)
        ), patch(
            "botapp.services.maybe_web_search", new=AsyncMock(return_value="")
        ), patch(
            "botapp.services.build_ai_messages", return_value=[{"role": "user", "content": "hi"}]
        ):
            reply, _ = async_to_sync(_call_direct)()
        self.assertTrue(reply)
        _assert_no_note(self, reply)


async def _call_direct():
    return await _call_noya_api_direct("سلام", "sess-1")
