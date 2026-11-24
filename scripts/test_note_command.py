#!/usr/bin/env python3
"""agent-memory note: the tool it files under, and the repo it needs (battle test v0.1.0, findings 4 and 5)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_MEMORY = ["python3", str(ROOT / "scripts" / "agent_memory.py")]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from stub_server import StubServer


class NoteCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        cfg = self.home / ".config" / "agent-memory"
        cfg.mkdir(parents=True)
        (cfg / "identity").write_text("org = TestOrg\nteam = TestTeam\nperson = tester\n", encoding="utf-8")
        self.server = StubServer()
        self.addCleanup(self.server.close)
        (cfg / "server").write_text(f"url = {self.server.url}\n", encoding="utf-8")
        self.store = base / "store"
        self.store.mkdir()
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["AGENT_MEMORY_DIR"] = str(self.store)
        self.env["GIT_CEILING_DIRECTORIES"] = str(base)
        for name in ("GIT_DIR", "GIT_WORK_TREE"):
            self.env.pop(name, None)

    def cli(self, args, cwd, stdin=None):
        return subprocess.run(
            AGENT_MEMORY + args, cwd=cwd, env=self.env, text=True, capture_output=True, input=stdin
        )

    def note(self, cwd, session_id="my-note", text="what to keep"):
        return self.cli(
            ["note", "--session-id", session_id, "--kind", "learning", "--domain", "code", "--text", text], cwd
        )

    def make_repo(self, name="repo", origin="https://github.com/example/proj.git"):
        repo = Path(self.tmp.name) / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=self.env)
        if origin:
            subprocess.run(["git", "remote", "add", "origin", origin], cwd=repo, check=True, env=self.env)
        return repo

    def inbox(self):
        return sorted(str(p.relative_to(self.store / "inbox")) for p in (self.store / "inbox").glob("*/*/bundle.json"))

    def bundle(self, tool, session_id="my-note"):
        return json.loads((self.store / "inbox" / tool / session_id / "bundle.json").read_text(encoding="utf-8"))

    def python(self, script):
        """Run agent_memory functions in a separate process, with this test's environment."""
        prelude = f"import sys\nsys.path.insert(0, {str(ROOT / 'scripts')!r})\nimport agent_memory as a\n"
        result = subprocess.run(["python3", "-c", prelude + script], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    # Finding 4: a note is filed under the tool note, not cursor.

    def test_note_files_under_the_note_tool(self):
        repo = self.make_repo()
        result = self.note(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.inbox(), ["note/my-note/bundle.json"])
        bundle = self.bundle("note")
        self.assertEqual(bundle["tool"], "note")
        self.assertEqual(bundle["event"], "note")
        self.assertTrue(bundle["pinned"])
        self.assertEqual(bundle["land"]["remote_repo"], "github.com/example/proj")

    def test_approved_note_is_stored_with_the_note_tool(self):
        repo = self.make_repo()
        self.assertEqual(self.note(repo).returncode, 0)
        offered = self.python(
            "import json\n"
            "items = a.offered(False)\n"
            "print(json.dumps([[i['tool'], i['session_id']] for i in items]))\n"
            "page = a.render_page(items, False)\n"
            "print('name=\"tool\" value=\"note\"' in page)\n"
            "bundle = a.find_bundle('my-note', 'note')\n"
            "a.approve(bundle, 'learning', 'code', bundle['draft']['text'])\n"
        )
        listed, hidden_field = offered.splitlines()
        self.assertEqual(json.loads(listed), [["note", "my-note"]])
        self.assertEqual(hidden_field, "True")
        rows = list(self.server.rows.values())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "note")
        self.assertEqual(rows[0]["repo"], "github.com/example/proj")
        self.assertEqual(rows[0]["session_id"], "my-note")
        self.assertEqual(self.inbox(), [])

    def test_keep_and_drop_take_the_note_tool(self):
        repo = self.make_repo()
        self.assertEqual(self.note(repo).returncode, 0)
        kept = self.cli(["keep", "my-note", "--tool", "note"], repo)
        self.assertEqual(kept.returncode, 0, kept.stderr)
        dropped = self.cli(["drop", "my-note", "--tool", "note"], repo)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        self.assertEqual(self.inbox(), [])

    def test_pinning_again_replaces_a_note_filed_under_cursor(self):
        repo = self.make_repo()
        legacy = self.store / "inbox" / "cursor" / "my-note"
        legacy.mkdir(parents=True)
        (legacy / "bundle.json").write_text(
            json.dumps({"tool": "cursor", "session_id": "my-note", "event": "note", "started_at": "2026-11-01T00:00:00+00:00"}),
            encoding="utf-8",
        )
        self.assertEqual(self.note(repo).returncode, 0)
        self.assertEqual(self.inbox(), ["note/my-note/bundle.json"])
        self.assertEqual(self.bundle("note")["started_at"], "2026-11-01T00:00:00+00:00")

    def test_a_failed_write_keeps_the_note_filed_under_cursor(self):
        repo = self.make_repo()
        legacy = self.store / "inbox" / "cursor" / "my-note"
        legacy.mkdir(parents=True)
        (legacy / "bundle.json").write_text(
            json.dumps({"tool": "cursor", "session_id": "my-note", "event": "note"}), encoding="utf-8"
        )
        # A file where the note folder goes, so the new bundle can't be written.
        (self.store / "inbox" / "note").write_text("", encoding="utf-8")
        self.assertNotEqual(self.note(repo).returncode, 0)
        self.assertTrue((legacy / "bundle.json").is_file())

    def test_pinning_skips_an_unreadable_cursor_bundle_with_the_same_id(self):
        repo = self.make_repo()
        session = self.store / "inbox" / "cursor" / "my-note"
        session.mkdir(parents=True)
        (session / "bundle.json").write_text('{"tool": "cursor", "sess', encoding="utf-8")
        result = self.note(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((session / "bundle.json").is_file())
        self.assertEqual(self.bundle("note")["tool"], "note")

    def test_pinning_keeps_a_cursor_session_with_the_same_id(self):
        repo = self.make_repo()
        session = self.store / "inbox" / "cursor" / "my-note"
        session.mkdir(parents=True)
        (session / "bundle.json").write_text(
            json.dumps({"tool": "cursor", "session_id": "my-note", "event": "stop"}), encoding="utf-8"
        )
        self.assertEqual(self.note(repo).returncode, 0)
        self.assertEqual(self.inbox(), ["cursor/my-note/bundle.json", "note/my-note/bundle.json"])

    # Finding 5: no remote repo id, no note.

    def test_note_outside_a_git_repo_fails_and_writes_nothing(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        result = self.note(plain)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote repo id", result.stderr)
        self.assertIn(".agent-memory.toml", result.stderr)
        self.assertNotIn("pinned", result.stdout)
        self.assertEqual(self.inbox() if (self.store / "inbox").is_dir() else [], [])

    def test_note_in_a_repo_without_origin_fails(self):
        repo = self.make_repo(origin=None)
        result = self.note(repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("remote repo id", result.stderr)
        self.assertEqual(self.inbox() if (self.store / "inbox").is_dir() else [], [])

    def test_note_with_a_local_path_origin_fails_and_names_the_url_shape(self):
        repo = self.make_repo(origin="/srv/git/proj.git")
        result = self.note(repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("https://github.com/owner/name", result.stderr)
        self.assertIn("repo=", result.stderr)
        self.assertEqual(self.inbox() if (self.store / "inbox").is_dir() else [], [])

    def test_note_takes_the_repo_id_from_agent_memory_toml(self):
        repo = self.make_repo(origin=None)
        (repo / ".agent-memory.toml").write_text('repo = "example.com/team/proj"\n', encoding="utf-8")
        result = self.note(repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bundle("note")["land"]["remote_repo"], "example.com/team/proj")

    def test_note_from_a_subdirectory_uses_the_repo_root(self):
        repo = self.make_repo()
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        result = self.note(sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = self.bundle("note")
        self.assertEqual(Path(bundle["repo"]).resolve(), repo.resolve())
        self.assertEqual(bundle["land"]["remote_repo"], "github.com/example/proj")


if __name__ == "__main__":
    unittest.main()
