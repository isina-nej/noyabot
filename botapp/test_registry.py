"""Registry contract + chat/admin visibility tests.

Covers the registry P0s without touching product behaviour: admin
metadata is preserved and validated, the normal Noya chat loop sees
the conversational tools plus the read-only research catalog
(``botapp.research``), and the admin catalog stays invisible to the
chat path.
"""
from django.test import SimpleTestCase

import botapp.agent  # noqa: F401  (registers chat + research tools on import)
import botapp.agent_tools  # noqa: F401  (registers the admin tools)
from botapp.agent.errors import InvalidToolInput, UnknownTool
from botapp.agent.registry import registry


BASE_CHAT_TOOLS = {"web_search", "fetch_url", "get_time", "generate_image", "text_to_speech"}

RESEARCH_CHAT_TOOLS = {
    "x.search_posts", "x.get_post", "x.get_thread", "x.get_user",
    "x.get_user_posts", "x.get_replies",
    "youtube.search", "youtube.get_video", "youtube.get_channel",
    "youtube.get_comments", "youtube.get_playlist", "youtube.get_transcript",
    "research.social_search",
}

CHAT_TOOLS = BASE_CHAT_TOOLS | RESEARCH_CHAT_TOOLS


class RegistryContractTests(SimpleTestCase):
    def test_admin_tool_metadata_preserved(self):
        tool = registry.get("group.get_info")
        self.assertEqual(tool.permission, "group.read")
        self.assertEqual(tool.risk_level, "low")
        self.assertEqual(tool.target_kind, "none")
        self.assertFalse(tool.requires_confirmation)
        self.assertIsNotNone(tool.input_schema)

    def test_validate_params_accepts_valid_input(self):
        tool = registry.get("group.get_info")
        validated = tool.validate_params({})
        self.assertIsNotNone(validated)

    def test_validate_params_rejects_invalid_input(self):
        tool = registry.get("member.mute")
        with self.assertRaises(InvalidToolInput):
            tool.validate_params({})

    def test_has_resolves_admin_tools(self):
        self.assertTrue(registry.has("group.get_info"))

    def test_get_returns_complete_tool(self):
        tool = registry.get("group.get_info")
        self.assertEqual(tool.name, "group.get_info")
        self.assertTrue(callable(tool.handler))

    def test_unknown_tool_raises(self):
        with self.assertRaises(UnknownTool):
            registry.get("member.__evil__")

    def test_insufficient_permission_rejected_by_gate(self):
        from botapp.agent.permissions import AdminIdentity, ensure_admin_permission

        tool = registry.get("member.ban")  # requires ADMINISTRATOR
        viewer = AdminIdentity(user_id=1, role="viewer")
        from botapp.agent.errors import AgentPermissionDenied

        with self.assertRaises(AgentPermissionDenied):
            ensure_admin_permission(viewer, tool.permission)


class ChatAdminSeparationTests(SimpleTestCase):
    def test_chat_loop_sees_chat_plus_research_tools(self):
        names = {entry["function"]["name"] for entry in registry.chat_definitions()}
        self.assertEqual(names, CHAT_TOOLS)

    def test_research_tools_visible_to_chat_loop(self):
        names = {entry["function"]["name"] for entry in registry.chat_definitions()}
        for tool_name in RESEARCH_CHAT_TOOLS:
            self.assertIn(tool_name, names)

    def test_admin_tool_absent_from_chat_definitions(self):
        names = {entry["function"]["name"] for entry in registry.chat_definitions()}
        self.assertNotIn("member.ban", names)

    def test_research_tools_absent_from_admin_catalog(self):
        self.assertFalse(registry.has("x.search_posts"))
        self.assertFalse(registry.has("youtube.search"))
        self.assertFalse(registry.has("research.social_search"))
        self.assertNotIn("research.social_search", registry.names())

    def test_research_tool_dispatched_on_chat_path(self):
        import asyncio

        from botapp.research import credentials as creds

        orig = (creds.CredentialStore.x_enabled, creds.CredentialStore.x_bearer)
        creds.CredentialStore.x_enabled = staticmethod(lambda: True)
        creds.CredentialStore.x_bearer = staticmethod(lambda: "test-bearer-token")
        try:
            from unittest.mock import AsyncMock, patch

            from botapp.research.models import ResearchItem

            fake = [ResearchItem(
                platform="x", source_type="post", source_id="1",
                author="A", title="@a", text="hello",
                published_at=None, url="https://x.com/a/status/1",
            )]
            with patch(
                "botapp.research.providers.x.XResearchProvider.search",
                AsyncMock(return_value=fake),
            ):
                out = asyncio.run(registry.dispatch_async(
                    "x.search_posts", {"query": "test"},
                ))
            self.assertIn("x.com", out)
            self.assertNotIn("test-bearer-token", out)
        finally:
            creds.CredentialStore.x_enabled, creds.CredentialStore.x_bearer = orig

    def test_admin_catalog_still_lists_admin_tools(self):
        self.assertIn("member.ban", registry.names())
        self.assertGreaterEqual(len(registry.all()), 50)

    def test_admin_dispatch_refused_on_chat_path(self):
        from asgiref.sync import async_to_sync

        result = async_to_sync(registry.dispatch_async)("member.ban", {"target_user_id": 7})
        self.assertIn("not available in this context", result)
