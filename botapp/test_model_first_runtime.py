"""Regression checks for model-first Noya chat (no network/Telegram required)."""
from contextlib import nullcontext
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from botapp.agent.loop import run_agent_loop
from botapp.agent.model_runtime import (
    available_skills, current_turn, ensure_model_first_tools, telegram_turn,
)


class ModelFirstRuntimeTests(SimpleTestCase):
    def test_chat_only_and_fixed_skill_names(self):
        ensure_model_first_tools()
        from botapp.agent.registry import registry
        names = {x["function"]["name"] for x in registry.chat_definitions()}
        self.assertIn("load_skill", names)
        self.assertIn("telegram_progress", names)
        self.assertIn("edit_image", names)
        self.assertTrue(all(name in available_skills() for name in ("web-research", "conversation")))
        self.assertNotIn("member.ban", names)
        self.assertNotIn("channel.post_text", names)

    def test_turn_state_is_isolated(self):
        m = SimpleNamespace(reply=AsyncMock())
        self.assertIsNone(current_turn())
        with telegram_turn(m, [{"data": b"abc", "mime": "image/jpeg"}]) as t:
            self.assertIs(current_turn(), t)
            t.image_result = b"result"
            self.assertEqual(t.image_result, b"result")
        self.assertIsNone(current_turn())

    def test_telegram_progress_is_bounded_and_clean(self):
        from botapp.agent.model_runtime import _telegram_progress
        m = SimpleNamespace(reply=AsyncMock())
        with telegram_turn(m) as state:
            async_to_sync(_telegram_progress)("دارم منابع را بررسی می‌کنم.")
            async_to_sync(_telegram_progress)("هنوز در حال بررسی‌ام.")
            msg = async_to_sync(_telegram_progress)("بار سوم")
            self.assertIn("budget", msg)
            self.assertEqual(state.progress_count, 2)
            self.assertEqual(m.reply.await_count, 2)

    def test_first_call_sees_original_prompt_skill_catalog_and_image(self):
        response = {
            "choices": [{"message": {"role": "assistant", "content": "این عکس را دیدم."}, "finish_reason": "stop"}]
        }
        captured = []
        async def post(url, **kwargs):
            captured.append(kwargs["json"])
            return SimpleNamespace(text=json.dumps(response), json=lambda: response, raise_for_status=lambda: None)
        client = AsyncMock()
        client.post.side_effect = post
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        manager.__aexit__.return_value = None
        with patch("botapp.agent.loop.httpx.AsyncClient", return_value=manager):
            result, meta = async_to_sync(run_agent_loop)(
                "تصویر را توصیف کن", "test-session", system_prompt="SYSTEM",
                model_first=True, images=[{"mime": "image/jpeg", "data": b"sample"}],
            )
        self.assertEqual(result, "این عکس را دیدم.")
        messages = captured[0]["messages"]
        self.assertIn("SYSTEM", messages[0]["content"])
        self.assertIn("load_skill", messages[0]["content"])
        self.assertEqual(messages[-1]["content"][0]["text"], "تصویر را توصیف کن")
        self.assertEqual(messages[-1]["content"][1]["type"], "image_url")
        self.assertTrue(any(x["function"]["name"] == "load_skill" for x in captured[0]["tools"]))

    def test_context_updated_after_skill_tool(self):
        calls = [
            {"choices":[{"message":{"role":"assistant","content":None,"tool_calls":[{
                "id":"c1","type":"function","function":{"name":"load_skill","arguments":json.dumps({"name":"web-research"})}
            }]},"finish_reason":"tool_calls"}]},
            {"choices":[{"message":{"role":"assistant","content":"Ready"},"finish_reason":"stop"}]},
        ]
        captured=[]
        async def post(url, **kwargs):
            captured.append(kwargs["json"])
            payload=calls[len(captured)-1]
            return SimpleNamespace(text=json.dumps(payload),json=lambda:payload,raise_for_status=lambda:None)
        client=AsyncMock()
        client.post.side_effect=post
        manager=AsyncMock()
        manager.__aenter__.return_value=client
        manager.__aexit__.return_value=None
        with patch("botapp.agent.loop.httpx.AsyncClient",return_value=manager):
            result,metadata=async_to_sync(run_agent_loop)(
                "اخبار هفته اخیر چیست؟","test-session",system_prompt="SYSTEM",model_first=True
            )
        self.assertEqual(result,"Ready")
        second=captured[1]["messages"]
        self.assertTrue(any(m.get("role")=="tool" and "Web research" in m.get("content","") for m in second))
        self.assertTrue(any(m.get("role")=="assistant" and m.get("tool_calls") for m in second))
