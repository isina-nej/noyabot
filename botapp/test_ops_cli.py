"""Noyaops terminal client unit checks, no sockets or remote credentials."""
from contextlib import redirect_stdout
import io
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from noyaops import cli


class NoyaOpsCLITests(SimpleTestCase):
    def test_refuses_non_tls_external_url(self):
        with self.assertRaises(cli.OpsError):
            cli.Client("http://example.com/api/internal/ops/v1", "key", 5)
        client = cli.Client("http://127.0.0.1:8000/api/internal/ops/v1", "key", 5)
        self.assertIn("127.0.0.1", client.base)

    def test_read_and_run_endpoints(self):
        fake = MagicMock()
        fake.post.return_value = {"status": "queued", "run_id": "ab" * 16}
        with patch("noyaops.cli.Client", return_value=fake), redirect_stdout(io.StringIO()):
            result = cli.main(["tests", "run", "process", "--json"])
        self.assertEqual(result, 0)
        fake.post.assert_called_once_with("diagnostics/runs/", {"test_id": "process"})

        fake.get.return_value = {"status": "completed", "outcome": "ok"}
        with patch("noyaops.cli.Client", return_value=fake), redirect_stdout(io.StringIO()):
            result = cli.main(["tests", "report", "ab" * 16])
        self.assertEqual(result, 0)
        fake.get.assert_called_with("diagnostics/runs/" + "ab" * 16 + "/report/")

    def test_logs_follow_uses_cursor_instead_of_repeating_history(self):
        fake = MagicMock()
        fake.get.side_effect = [
            {"entries": [{"event": "startup"}], "next_cursor": 44},
            KeyboardInterrupt(),
        ]
        args = SimpleNamespace(
            cursor=0, limit=10, level="", service="", trace_id="",
            correlation_id="", since=None, until=None, poll_seconds=0.5,
            as_json=True,
        )
        with patch("noyaops.cli.time.sleep", return_value=None):
            with redirect_stdout(io.StringIO()):
                status = cli._follow(fake, args)
        self.assertEqual(status, 0)
        first_query = fake.get.call_args_list[0].args[1]
        second_query = fake.get.call_args_list[1].args[1]
        self.assertEqual(first_query["cursor"], 0)
        self.assertEqual(second_query["cursor"], 44)
