#!/usr/bin/env python3
"""The server-version warning: review prints it, the hooks never ask. Standard library only.

A stub server stands in for /v1/health, and every test has a temporary HOME and AGENT_MEMORY_DIR.
"""

import argparse
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory
import server_client
from test_cutover import CLI, Case, GitCase

CLI_VERSION = "0.3.0"


def health_calls(server):
    return [request for request in server.requests if request["path"] == "/v1/health"]


class VersionLineTest(unittest.TestCase):
    def test_the_line_for_each_answer(self):
        line = server_client.version_line
        self.assertEqual(line({"version": "0.3.0"}, "0.3.0"), "")
        self.assertEqual(
            line({"version": "0.2.0"}, "0.3.0"),
            "the server runs 0.2.0 and this CLI is 0.3.0: run `agent-memory setup` to upgrade the server",
        )
        self.assertEqual(
            line({"status": "ok"}, "0.3.0"),
            "the server runs a version older than 0.2.0 and this CLI is 0.3.0: run `agent-memory setup` to upgrade the server",
        )
        self.assertEqual(line(None, "0.3.0"), "")
        self.assertEqual(line([], "0.3.0"), "")

    def test_a_newer_server_says_to_upgrade_the_cli(self):
        said = server_client.version_line({"version": "0.4.0"}, "0.3.0", "http://127.0.0.1:8765")
        self.assertEqual(said, "the server runs 0.4.0 and this CLI is 0.3.0: upgrade this CLI")
        self.assertNotIn("agent-memory setup", said)
        self.assertIn("upgrade this CLI", server_client.version_line({"version": "0.10.0"}, "0.9.0"))

    def test_another_url_names_it_and_gives_no_setup_advice(self):
        for health in ({"version": "0.2.0"}, {"version": "0.4.0"}, {}):
            said = server_client.version_line(health, "0.3.0", "https://memory.acme.internal")
            self.assertIn("https://memory.acme.internal", said)
            self.assertIn("this CLI is 0.3.0", said)
            self.assertNotIn("agent-memory setup", said)
        self.assertIn("older than 0.2.0", server_client.version_line({}, "0.3.0", "http://127.0.0.1:9000"))

    def test_loopback_8765_either_way_is_the_local_server(self):
        for url in ("http://127.0.0.1:8765", "http://localhost:8765"):
            self.assertIn("run `agent-memory setup`", server_client.version_line({"version": "0.2.0"}, "0.3.0", url))


class ReviewWarningTest(Case):
    def review(self):
        """review() up to the point it would serve, which a patched server class refuses."""
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.object(agent_memory, "cli_version", lambda: CLI_VERSION),
            mock.patch.object(agent_memory, "ThreadingHTTPServer", side_effect=OSError),
            self.assertRaises(SystemExit),
        ):
            agent_memory.review(argparse.Namespace(port=0))
        return err.getvalue()

    def test_a_different_version_prints_one_line(self):
        self.server.version = "0.2.0"
        said = self.review()
        # The stub is on a random port, so it counts as a remote server: no setup advice.
        self.assertIn(f"the server at {self.server.url} runs 0.2.0 and this CLI is {CLI_VERSION}", said)
        self.assertNotIn("agent-memory setup", said)
        self.assertEqual(said.count("this CLI is"), 1)

    def test_a_local_url_gets_the_setup_advice(self):
        with mock.patch.object(server_client, "read_config", return_value={"url": "http://127.0.0.1:8765", "token": None, "mode": "shared"}):
            with mock.patch.object(server_client.Client, "health", return_value={"status": "ok", "version": "0.2.0"}):
                said = self.review()
        self.assertIn(f"the server runs 0.2.0 and this CLI is {CLI_VERSION}: run `agent-memory setup` to upgrade the server", said)

    def test_a_newer_server_says_to_upgrade_the_cli(self):
        with mock.patch.object(server_client, "read_config", return_value={"url": "http://localhost:8765", "token": None, "mode": "shared"}):
            with mock.patch.object(server_client.Client, "health", return_value={"status": "ok", "version": "0.9.0"}):
                said = self.review()
        self.assertIn(f"the server runs 0.9.0 and this CLI is {CLI_VERSION}: upgrade this CLI", said)

    def test_a_missing_version_says_older_than_0_2_0(self):
        self.assertIn("runs a version older than 0.2.0", self.review())

    def test_the_same_version_says_nothing(self):
        self.server.version = CLI_VERSION
        self.assertNotIn("this CLI is", self.review())

    def test_a_server_that_is_down_adds_no_output_and_no_failure(self):
        self.connect("http://127.0.0.1:1")
        self.assertNotIn("this CLI is", self.review())

    def test_a_server_that_fails_adds_no_output(self):
        self.server.fail_path["/v1/health"] = (500, {"error": "broken"})
        self.assertNotIn("this CLI is", self.review())

    def test_no_server_file_makes_no_call(self):
        (self.config / "server").unlink()
        self.assertNotIn("this CLI is", self.review())
        self.assertEqual(health_calls(self.server), [])

    def test_the_call_uses_the_page_timeout(self):
        with mock.patch.object(server_client, "Client", wraps=server_client.Client) as client:
            self.review()
        self.assertEqual(client.call_args_list[0].args[2], agent_memory.PAGE_TIMEOUT)


class HooksNeverAskTest(GitCase):
    """The commands that run from hooks must not call /v1/health, whatever the server runs."""

    def setUp(self):
        super().setUp()
        self.server.version = "0.0.1"

    def cli_stdin(self, text, *args):
        return subprocess.run(CLI + list(args), cwd=self.repo, env=self.env(), input=text, text=True, capture_output=True, timeout=60)

    def assert_quiet(self, result):
        self.assertNotIn("this CLI is", result.stdout + result.stderr)
        self.assertEqual(health_calls(self.server), [])

    def test_capture(self):
        payload = '{"hook_event_name": "sessionEnd", "session_id": "s1", "cwd": "%s"}' % self.repo
        result = self.cli_stdin(payload, "capture", "--tool", "cursor")
        self.assert_quiet(result)

    def test_git_hook(self):
        self.commit("one")
        self.assert_quiet(self.cli("git-hook", "--event", "post-commit"))

    def test_sync_quiet_with_something_to_send(self):
        import outbox

        outbox.add(self.state, self.server.url, "rejection", {"repo": "github.com/example/proj", "shas": ["a" * 40]})
        result = self.cli("sync", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.server.posts("/v1/rejections"), "the outbox did reach the server")
        self.assert_quiet(result)

    def test_sync_quiet_with_nothing_to_send(self):
        self.assert_quiet(self.cli("sync", "--quiet"))


if __name__ == "__main__":
    unittest.main()
