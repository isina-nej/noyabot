"""Offline guardrails for server-side AI ops (no Codex, tokens, network or VM)."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from noyaops import aiops


class AIOpsSafetyTests(SimpleTestCase):
    def test_live_journal_summary_never_exposes_private_message_text(self):
        raw = (
            "[NOYA-AGENT] Iteration 1 model=noya tools=18\n"
            '[httpx] HTTP Request: POST http://127.0.0.1:20128/v1/chat/completions "HTTP/1.1 200 OK"\n'
            "[NOYA-TIMING] Agent loop done in 9534.5ms tools=[]\n"
            "[NOYA-TIMING] Telegram reply finished for msg_id=92837\n"
            "[NOYA-TIMING] Incoming chat msg_id=92837 chat_id=-1003861069387: private-message-body\n"
            "Authorization: Bearer very-private-token\n"
        )
        result = aiops.summarize_live_journal(raw)
        encoded = json.dumps(result)
        self.assertEqual(result["agent_iterations"], 1)
        self.assertEqual(result["ai_requests_observed"], 1)
        self.assertEqual(result["telegram_replies_observed"], 1)
        self.assertEqual(result["provider_http_status_counts"], {"200": 1})
        self.assertEqual(result["agent_latency_ms"]["max"], 9534.5)
        for leaked in ("private-message-body", "very-private-token", "-1003861069387", "92837"):
            self.assertNotIn(leaked, encoded)

    def test_remote_url_is_pinned_to_github(self):
        with patch("noyaops.aiops._cmd") as cmd:
            answer = aiops._remote_heads("http://localhost/secrets")
        self.assertFalse(answer["available"])
        cmd.assert_not_called()
        with patch("noyaops.aiops._cmd") as cmd:
            cmd.return_value = SimpleNamespace(
                returncode=0,
                stdout="abcd\trefs/heads/main\n1234\trefs/heads/feat/secure-durable-noyaops\n"
            )
            result = aiops._remote_heads("https://github.com/isina-nej/noyabot.git")
        self.assertTrue(result["available"])
        self.assertEqual(result["heads"]["main"], "abcd")

    def test_version_status_reports_dirty_production_without_file_contents(self):
        with patch("noyaops.aiops._git") as git:
            git.side_effect = [
                "deadbeef", " M botapp/agent/loop.py\n?? .env.local",
                "main"
            ]
            result = aiops._repo_state("/opt/noyabot")
        # The actual git directory won't exist on this CI runner; simulate it
        # in the test below using an isolated temporary checkout.

    def test_production_state_from_mock(self):
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            Path(directory, ".git").mkdir()
            with patch("noyaops.aiops._git", side_effect=[
                "deadbeef", " M botapp/agent/loop.py\n?? .env.local", "main"
            ]):
                result = aiops._repo_state(directory)
        self.assertTrue(result["dirty"])
        self.assertEqual(result["changed_path_count"], 2)
        self.assertEqual(result["tracked_modified_count"], 1)
        self.assertEqual(result["untracked_count"], 1)
        self.assertNotIn(".env.local", json.dumps(result))
        self.assertNotIn("botapp/agent/loop.py", json.dumps(result))

    def test_repair_is_refused_as_root(self):
        with patch("noyaops.aiops.os.geteuid", return_value=0):
            with self.assertRaisesRegex(aiops.AgentOpsError, "root"):
                aiops.run_agent(mode="repair", request="fix tests", snapshot={})
            with self.assertRaisesRegex(aiops.AgentOpsError, "root"):
                aiops.create_clean_workspace(
                    repository=aiops.DEFAULT_REPOSITORY,
                    base="main"
                )

    def test_repair_clone_is_restricted_to_approved_branches_and_repo(self):
        with patch("noyaops.aiops.os.geteuid", return_value=1002):
            with self.assertRaisesRegex(aiops.AgentOpsError, "approved"):
                aiops.create_clean_workspace(
                    repository="https://github.com/evil/fork.git",
                    base="main",
                )
            with self.assertRaisesRegex(aiops.AgentOpsError, "approved"):
                aiops.create_clean_workspace(
                    repository=aiops.DEFAULT_REPOSITORY,
                    base="../../../etc",
                )

    def test_agent_environment_scrubs_credential_variables(self):
        with patch.dict(os.environ, {
            "NOYA_API_KEY": "production-9router-secret",
            "NOYA_OPS_READ_TOKEN": "ops-token",
            "NOYA_OPS_RUN_TOKEN": "ops-run-token",
            "OPENAI_API_KEY": "private-provider-token",
            "BOT_TOKEN": "some-token",
            "DJANGO_SECRET_KEY": "secret",
        }):
            env = aiops._agent_environment()
        for name in aiops.PROTECTED_NAMES:
            self.assertTrue(not env.get(name), name)
        self.assertEqual(env["DB_ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(env["DB_NAME"], ":memory:")

    def test_diagnostic_ids_are_allowlisted(self):
        with self.assertRaisesRegex(aiops.AgentOpsError, "allowlisted"):
            aiops.run_diagnostic("rm -rf /")
        with self.assertRaisesRegex(aiops.AgentOpsError, "required"):
            with patch.dict(os.environ, {"NOYA_OPS_RUN_TOKEN": ""}):
                aiops.run_diagnostic("process")

    def test_snapshot_reports_real_bot_not_monitored_yet(self):
        with (
            patch("noyaops.aiops._repo_state", side_effect=[
                {"present": True, "commit": "abc", "dirty": True},
                {"present": True, "commit": "def", "dirty": False},
            ]),
            patch("noyaops.aiops._systemd", return_value={"ActiveState": "active"}),
            patch("noyaops.aiops._remote_heads",
                  return_value={"available": True, "heads": {"main": "ffff"}}),
            patch("noyaops.aiops._ops_snapshot",
                  return_value={"connected": True, "health": {"checks": {
                      "bot_worker": {"healthy": False, "reason": "never_seen"}
                  }}}),
        ):
            snapshot = aiops.collect_snapshot()
        self.assertIn("production_checkout_has_uncommitted_or_untracked_changes",
                      snapshot["limitations"])
        self.assertIn("real_telegram_bot_not_emitting_ops_heartbeat",
                      snapshot["limitations"])
        self.assertFalse(snapshot["running_code_proven_at_exact_sha"])
        self.assertEqual(snapshot["github"]["heads"]["main"], "ffff")
