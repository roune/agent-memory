#!/usr/bin/env python3
"""connect --editor and uninstall: MCP entries in Claude Code, Cursor and Codex.

Standard library only. Every test has a temporary HOME, so the real
~/.claude.json, ~/.cursor, ~/.codex and Pi are never read or written. A stub
server on 127.0.0.1 (stub_server.py) plays the agent-memory server and mints
the editor tokens.
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory
import editors
import server_client
from stub_server import StubServer

WRITE_TOKEN = "am_laptop_write"
OTHER_TOKEN = "am_other_write"

CLAUDE_BEFORE = {
    "numStartups": 12,
    "oauthAccount": {"emailAddress": "someone@example.invalid"},
    "mcpServers": {"notes": {"type": "stdio", "command": "notes-mcp", "args": []}},
    "projects": {"/work/app": {"mcpServers": {"local-only": {"type": "http", "url": "http://localhost:9/mcp"}}}},
}
CURSOR_BEFORE = {"mcpServers": {"github": {"url": "https://api.example.invalid/mcp", "headers": {"X-Key": "keep"}}}}
CODEX_BEFORE = """# my Codex settings
model = "gpt-5"

[mcp_servers.docs]
command = "docs-mcp"
args = ["--fast"]

[mcp_servers.docs.env]
DOCS_HOME = "/tmp/docs"

[profiles.work]
model = "o3"
"""


def mode_bits(path):
    return stat.S_IMODE(path.stat().st_mode)


class Case(unittest.TestCase):
    token = WRITE_TOKEN

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.config = self.home / ".config" / "agent-memory"
        self.config.mkdir(parents=True)
        for patcher in (
            mock.patch.dict(os.environ, {"HOME": str(self.home), "AGENT_MEMORY_DIR": str(Path(self.tmp.name) / "state")}),
            mock.patch.object(agent_memory, "HOME", self.home),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = self.stub(token=self.token)
        server_client.write_config(self.server.url, self.token, "shared")

    def stub(self, **kwargs):
        server = StubServer(**kwargs)
        self.addCleanup(server.close)
        return server

    def run_cli(self, *argv, tty_answer=None):
        """Run agent-memory in-process. Returns (exit code, stdout)."""
        args = agent_memory.build_parser().parse_args(list(argv))
        out = io.StringIO()
        patches = [contextlib.redirect_stdout(out)]
        if tty_answer is not None:
            patches += [mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value=tty_answer)]
        code = 0
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            try:
                result = args.func(args)
                code = 0 if result is None else result
            except SystemExit as error:
                code = error.code
                if isinstance(code, str):
                    out.write(code + "\n")
                    code = 1
        return code, out.getvalue()

    def seed(self):
        (self.home / ".claude.json").write_text(json.dumps(CLAUDE_BEFORE, indent=2), encoding="utf-8")
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps(CURSOR_BEFORE), encoding="utf-8")
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text(CODEX_BEFORE, encoding="utf-8")

    def claude(self):
        return json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))

    def cursor(self):
        return json.loads((self.home / ".cursor" / "mcp.json").read_text(encoding="utf-8"))

    def codex_text(self):
        return (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")

    def codex(self):
        return tomllib.loads(self.codex_text())

    def files(self):
        """Every file under HOME with its bytes, for 'nothing was written'."""
        return {
            str(path.relative_to(self.home)): path.read_bytes()
            for path in sorted(self.home.rglob("*"))
            if path.is_file()
        }

    def tokens_in_files(self):
        found = []
        for servers in (self.claude()["mcpServers"], self.cursor()["mcpServers"]):
            for name, value in servers.items():
                if name.startswith("agent-memory") and value.get("headers"):
                    found.append(value["headers"]["Authorization"])
        for name, value in self.codex().get("mcp_servers", {}).items():
            if name.startswith("agent-memory") and value.get("http_headers"):
                found.append(value["http_headers"]["Authorization"])
        return found


class MergeTest(Case):
    def test_it_merges_into_existing_files_without_touching_other_servers(self):
        self.seed()
        code, out = self.run_cli("connect", "--editor", "claude-code,cursor,codex", "--yes")
        self.assertEqual(code, 0, out)

        claude = self.claude()
        self.assertEqual({key: value for key, value in claude.items() if key != "mcpServers"},
                         {key: value for key, value in CLAUDE_BEFORE.items() if key != "mcpServers"})
        self.assertEqual(claude["mcpServers"]["notes"], CLAUDE_BEFORE["mcpServers"]["notes"])
        self.assertEqual(
            claude["mcpServers"]["agent-memory"],
            {"type": "http", "url": self.server.url + "/mcp", "headers": {"Authorization": "Bearer am_child1_editor-claude-code"}},
        )

        cursor = self.cursor()
        self.assertEqual(cursor["mcpServers"]["github"], CURSOR_BEFORE["mcpServers"]["github"])
        self.assertEqual(
            cursor["mcpServers"]["agent-memory"],
            {"url": self.server.url + "/mcp", "headers": {"Authorization": "Bearer am_child2_editor-cursor"}},
        )

        text = self.codex_text()
        self.assertTrue(text.startswith(CODEX_BEFORE.rstrip("\n")), text)
        codex = self.codex()
        before = tomllib.loads(CODEX_BEFORE)
        self.assertEqual(codex["mcp_servers"]["docs"], before["mcp_servers"]["docs"])
        self.assertEqual(codex["profiles"], before["profiles"])
        self.assertEqual(codex["model"], "gpt-5")
        self.assertEqual(
            codex["mcp_servers"]["agent-memory"],
            {"url": self.server.url + "/mcp", "http_headers": {"Authorization": "Bearer am_child3_editor-codex"}},
        )
        for path in (self.home / ".claude.json", self.home / ".cursor" / "mcp.json", self.home / ".codex" / "config.toml"):
            self.assertEqual(mode_bits(path) & 0o077, 0, path)

    def test_new_files_are_created(self):
        code, out = self.run_cli("connect", "--editor", "cursor,codex", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(list(self.cursor()["mcpServers"]), ["agent-memory"])
        self.assertEqual(list(self.codex()["mcp_servers"]), ["agent-memory"])
        self.assertFalse((self.home / ".claude.json").exists())

    def test_running_it_again_replaces_the_token_and_keeps_one_entry(self):
        self.seed()
        self.assertEqual(self.run_cli("connect", "--editor", "cursor,codex", "--yes")[0], 0)
        first = self.cursor()["mcpServers"]["agent-memory"]["headers"]["Authorization"]
        self.assertEqual(self.run_cli("connect", "--editor", "cursor,codex", "--yes")[0], 0)
        second = self.cursor()["mcpServers"]["agent-memory"]["headers"]["Authorization"]
        self.assertNotEqual(first, second)
        self.assertIn(first.removeprefix("Bearer "), self.server.revoked)
        self.assertEqual(sorted(self.cursor()["mcpServers"]), ["agent-memory", "github"])
        self.assertEqual(sorted(self.codex()["mcp_servers"]), ["agent-memory", "docs"])
        self.assertEqual(self.codex_text().count("[mcp_servers.agent-memory]"), 1)
        labels = [body["label"] for body in self.server.posts("/v1/tokens")]
        self.assertEqual(labels, ["editor-cursor", "editor-codex"] * 2)

    def test_a_codex_entry_it_cant_edit_is_refused_and_nothing_is_written(self):
        self.seed()
        path = self.home / ".codex" / "config.toml"
        path.write_text(CODEX_BEFORE + '\n[mcp_servers]\nagent-memory = { url = "http://elsewhere/mcp" }\n', encoding="utf-8")
        before = self.files()
        code, out = self.run_cli("connect", "--editor", "cursor,codex", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("can't edit", out)
        # Codex is checked before anything is minted or written: Cursor is unchanged too.
        self.assertEqual(self.files(), before)

    def test_broken_json_is_refused_before_anything_is_minted(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{not json", encoding="utf-8")
        code, out = self.run_cli("connect", "--editor", "claude-code,cursor", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("not valid JSON", out)
        self.assertEqual(self.server.posts("/v1/tokens"), [])
        self.assertFalse((self.home / ".claude.json").exists())


class PrintTest(Case):
    def test_print_writes_nothing(self):
        self.seed()
        before = self.files()
        code, out = self.run_cli("connect", "--editor", "claude-code,cursor,codex", "--print")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.files(), before)
        self.assertIn(
            f'claude mcp add --transport http --scope user agent-memory {self.server.url}/mcp '
            f'--header "Authorization: Bearer am_child1_editor-claude-code"',
            out,
        )
        self.assertIn('"Authorization": "Bearer am_child2_editor-cursor"', out)
        self.assertIn("[mcp_servers.agent-memory]", out)
        self.assertIn('http_headers = { "Authorization" = "Bearer am_child3_editor-codex" }', out)
        self.assertIn("Nothing was written.", out)

    def test_no_answer_without_a_terminal_writes_and_mints_nothing(self):
        self.seed()
        before = self.files()
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, out = self.run_cli("connect", "--editor", "cursor")
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", out)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.server.posts("/v1/tokens"), [])

    def test_a_no_at_the_prompt_writes_and_mints_nothing(self):
        self.seed()
        before = self.files()
        code, out = self.run_cli("connect", "--editor", "cursor,codex", tty_answer="n")
        self.assertEqual(code, 0, out)
        self.assertIn("left unchanged", out)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.server.posts("/v1/tokens"), [])

    def test_a_yes_at_the_prompt_writes(self):
        code, out = self.run_cli("connect", "--editor", "cursor", tty_answer="y")
        self.assertEqual(code, 0, out)
        self.assertIn("agent-memory", self.cursor()["mcpServers"])

    def test_pi_gets_no_entry(self):
        before = self.files()
        code, out = self.run_cli("connect", "--editor", "pi", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Pi gets no MCP registration", out)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.server.posts("/v1/tokens"), [])

    def test_unknown_editor_and_mixed_flags_are_refused(self):
        self.assertIn("unknown editor", self.run_cli("connect", "--editor", "vim", "--yes")[1])
        self.assertIn("run connect --url", self.run_cli("connect", "--editor", "cursor", "--url", "http://x", "--yes")[1])
        self.assertIn("go with --editor", self.run_cli("connect", "--url", self.server.url, "--yes")[1])


class TokenTest(Case):
    def test_the_editor_token_is_never_the_write_token(self):
        self.seed()
        self.assertEqual(self.run_cli("connect", "--editor", "claude-code,cursor,codex", "--yes")[0], 0)
        found = self.tokens_in_files()
        self.assertEqual(len(found), 3)
        self.assertEqual(len(set(found)), 3, "each editor gets its own token")
        for value in found:
            self.assertNotEqual(value, f"Bearer {WRITE_TOKEN}")
        # The write token only ever went to the server, as the caller of /v1/tokens.
        for path in (self.home / ".claude.json", self.home / ".cursor" / "mcp.json", self.home / ".codex" / "config.toml"):
            self.assertNotIn(WRITE_TOKEN, path.read_text(encoding="utf-8"))
        self.assertNotIn(WRITE_TOKEN, editors.record_path().read_text(encoding="utf-8"))
        calls = [request for request in self.server.requests if request["path"] == "/v1/tokens"]
        self.assertTrue(all(request["headers"]["Authorization"] == f"Bearer {WRITE_TOKEN}" for request in calls))

    def test_a_server_that_hands_back_the_write_token_is_refused(self):
        self.server.fail_path["/v1/tokens"] = (201, {"id": "t_x", "token": WRITE_TOKEN})
        code, out = self.run_cli("connect", "--editor", "cursor", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("never goes in an editor file", out)
        self.assertFalse((self.home / ".cursor" / "mcp.json").exists())

    def test_a_token_without_write_is_refused_before_anything_is_minted(self):
        self.server.scopes = ["read"]
        code, out = self.run_cli("connect", "--editor", "cursor", "--yes")
        self.assertNotEqual(code, 0)
        self.assertIn("read and write scopes", out)
        self.assertEqual(self.server.posts("/v1/tokens"), [])


class AuthOffTest(Case):
    token = None

    def test_auth_off_writes_no_header(self):
        self.seed()
        code, out = self.run_cli("connect", "--editor", "claude-code,cursor,codex", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.claude()["mcpServers"]["agent-memory"], {"type": "http", "url": self.server.url + "/mcp"})
        self.assertEqual(self.cursor()["mcpServers"]["agent-memory"], {"url": self.server.url + "/mcp"})
        self.assertEqual(self.codex()["mcp_servers"]["agent-memory"], {"url": self.server.url + "/mcp"})
        self.assertNotIn("Authorization", self.codex_text())
        self.assertEqual(self.server.posts("/v1/tokens"), [])

    def test_print_with_auth_off_has_no_header(self):
        code, out = self.run_cli("connect", "--editor", "claude-code,cursor,codex", "--print")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Authorization", out)
        self.assertNotIn("--header", out)


class PerRepoTest(Case):
    def setUp(self):
        super().setUp()
        self.other = self.stub(token=OTHER_TOKEN)
        server_client.write_config(self.server.url, WRITE_TOKEN, "per-repo")
        server_client.map_repo("github.com/acme/api", self.other.url, OTHER_TOKEN)
        # A second repo on the same server is still one entry.
        server_client.map_repo("github.com/acme/web", self.other.url, OTHER_TOKEN)

    def test_two_servers_give_two_entries_with_distinct_names(self):
        self.seed()
        code, out = self.run_cli("connect", "--editor", "claude-code,cursor,codex", "--yes")
        self.assertEqual(code, 0, out)
        ours = {name: value for name, value in self.cursor()["mcpServers"].items() if name.startswith("agent-memory")}
        self.assertEqual(sorted(ours), ["agent-memory", "agent-memory-127-0-0-1"])
        self.assertEqual(ours["agent-memory"]["url"], self.server.url + "/mcp")
        self.assertEqual(ours["agent-memory-127-0-0-1"]["url"], self.other.url + "/mcp")
        codex = self.codex()["mcp_servers"]
        self.assertEqual(codex["agent-memory-127-0-0-1"]["url"], self.other.url + "/mcp")
        claude = self.claude()["mcpServers"]
        self.assertEqual(claude["agent-memory-127-0-0-1"]["url"], self.other.url + "/mcp")
        # Each server minted with its own caller token.
        self.assertEqual(len(self.server.posts("/v1/tokens")), 3)
        self.assertEqual(len(self.other.posts("/v1/tokens")), 3)
        for request in self.other.requests:
            if request["path"] == "/v1/tokens":
                self.assertEqual(request["headers"]["Authorization"], f"Bearer {OTHER_TOKEN}")
        for value in self.tokens_in_files():
            self.assertNotIn(value, (f"Bearer {WRITE_TOKEN}", f"Bearer {OTHER_TOKEN}"))

    def test_a_server_no_longer_configured_loses_its_entry(self):
        self.assertEqual(self.run_cli("connect", "--editor", "cursor", "--yes")[0], 0)
        (self.config / "servers").write_text("", encoding="utf-8")
        code, out = self.run_cli("connect", "--editor", "cursor", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("no longer configured", out)
        self.assertEqual(list(self.cursor()["mcpServers"]), ["agent-memory"])

    def test_names_for_hosts(self):
        names = editors.entry_names(
            ["http://127.0.0.1:8765", "https://memory.acme.internal", "http://127.0.0.1:9000", "https://memory.acme.internal/b"]
        )
        self.assertEqual(
            list(names.values()),
            ["agent-memory", "agent-memory-memory-acme-internal", "agent-memory-127-0-0-1", "agent-memory-memory-acme-internal-2"],
        )
        self.assertEqual(len(set(names.values())), 4)


class UninstallTest(Case):
    def test_uninstall_removes_only_its_own_entries(self):
        self.seed()
        self.assertEqual(self.run_cli("connect", "--editor", "claude-code,cursor,codex", "--yes")[0], 0)
        # A hand-added server after connect, and the user re-pointing nothing.
        cursor = self.cursor()
        cursor["mcpServers"]["later"] = {"url": "https://later.example.invalid/mcp"}
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps(cursor), encoding="utf-8")

        code, out = self.run_cli("uninstall")
        self.assertEqual(code, 0, out)
        self.assertIn("token revoke t_child1", out)
        self.assertEqual(self.claude(), CLAUDE_BEFORE)
        self.assertEqual(self.cursor(), {"mcpServers": {**CURSOR_BEFORE["mcpServers"], "later": {"url": "https://later.example.invalid/mcp"}}})
        self.assertEqual(self.codex(), tomllib.loads(CODEX_BEFORE))
        self.assertEqual(self.codex_text().rstrip("\n"), CODEX_BEFORE.rstrip("\n"))
        self.assertFalse(editors.record_path().exists())

    def test_an_entry_it_did_not_add_or_that_was_repointed_is_kept(self):
        (self.home / ".cursor").mkdir()
        mine = {"mcpServers": {"agent-memory": {"url": "https://by-hand.example.invalid/mcp"}}}
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps(mine), encoding="utf-8")
        self.assertEqual(self.run_cli("uninstall")[0], 0)
        self.assertEqual(self.cursor(), mine)

        # Written by connect, then pointed elsewhere by hand: it is the user's now.
        self.assertEqual(self.run_cli("connect", "--editor", "cursor,codex", "--yes")[0], 0)
        cursor = self.cursor()
        cursor["mcpServers"]["agent-memory"]["url"] = "https://by-hand.example.invalid/mcp"
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps(cursor), encoding="utf-8")
        self.assertEqual(self.run_cli("uninstall")[0], 0)
        self.assertEqual(self.cursor()["mcpServers"]["agent-memory"]["url"], "https://by-hand.example.invalid/mcp")
        self.assertNotIn("agent-memory", self.codex().get("mcp_servers", {}))

    def test_uninstall_with_nothing_recorded_touches_no_editor_file(self):
        self.seed()
        before = self.files()
        self.assertEqual(self.run_cli("uninstall")[0], 0)
        self.assertEqual(self.files(), before)


class SymlinkTest(Case):
    def test_a_symlinked_file_stays_a_symlink(self):
        dotfiles = Path(self.tmp.name) / "dotfiles"
        dotfiles.mkdir()
        (dotfiles / "mcp.json").write_text(json.dumps(CURSOR_BEFORE), encoding="utf-8")
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").symlink_to(dotfiles / "mcp.json")
        self.assertEqual(self.run_cli("connect", "--editor", "cursor", "--yes")[0], 0)
        self.assertTrue((self.home / ".cursor" / "mcp.json").is_symlink())
        self.assertIn("agent-memory", json.loads((dotfiles / "mcp.json").read_text(encoding="utf-8"))["mcpServers"])


if __name__ == "__main__":
    unittest.main()
