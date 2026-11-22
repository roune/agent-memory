#!/usr/bin/env python3
"""The shared approval rules, and that approve() builds the same document with them."""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import agent_memory_server
import memory_rules

A = "a" * 40
B = "b" * 40


def fields(**changes):
    base = {
        "kind": "learning",
        "domain": "code",
        "text": "a note",
        "repo": "github.com/example/proj",
        "tool": "git",
        "session_id": "s1",
        "org": "Org",
        "team": "Team",
        "person": "tester",
        "occurred_at": "2026-11-20T10:00:00+00:00",
    }
    base.update(changes)
    return base


class RulesTest(unittest.TestCase):
    def rejects(self, message, **changes):
        with self.assertRaises(ValueError) as raised:
            memory_rules.build_document(**fields(**changes))
        self.assertEqual(str(raised.exception), message)

    def test_kind_domain_and_text(self):
        self.rejects("kind, domain, and text are required", kind="note")
        self.rejects("kind, domain, and text are required", domain="sales")
        self.rejects("kind, domain, and text are required", text=" \r\n ")

    def test_repo_tool_and_session(self):
        self.rejects("repo is required", repo=" ")
        self.rejects("tool is required", tool="")
        self.rejects("session id is required", session_id=None)

    def test_identity(self):
        self.rejects("org, team, and person are required (identity file or form)", team="")
        self.rejects("person must be a single path segment", person="a/b")
        self.rejects("person must be a single path segment", person="..")

    def test_occurred_at_and_record(self):
        self.rejects("occurred_at is required", occurred_at="")
        self.rejects("record must be empty, explanation, history, or runbook", record="diary")

    def test_a_change_needs_its_commit(self):
        self.rejects("a change needs the commit it explains", kind="change", commit="abc12")
        self.rejects("a change needs the commit it explains", kind="change", commit="zzzzzzzz")

    def test_text_is_lf_and_trimmed(self):
        document = memory_rules.build_document(**fields(text="  one\r\n\r\ntwo\rthree\r\n"))
        self.assertEqual(document["text"], "one\n\ntwo\nthree")

    def test_id_and_optional_links(self):
        document = memory_rules.build_document(
            **fields(record="runbook", subject=" queue ", look_at="", supersedes="tester-learning-s0")
        )
        self.assertEqual(document["id"], "tester-learning-s1")
        self.assertEqual(document["record"], "runbook")
        self.assertEqual(document["subject"], "queue")
        self.assertEqual(document["supersedes"], "tester-learning-s0")
        self.assertNotIn("look_at", document)
        self.assertNotIn("commit", document)

    def test_commits_count_only_for_a_change(self):
        learning = memory_rules.build_document(**fields(commit=A, commits=[A], pull_request=3))
        self.assertNotIn("commits", learning)
        self.assertNotIn("pull_request", learning)
        change = memory_rules.build_document(**fields(kind="change", commit=f" {B} ", commits=[A, B], pull_request=3))
        self.assertEqual((change["commit"], change["commits"], change["pull_request"]), (B, [A, B], 3))

    def test_a_change_without_commits_stores_its_commit(self):
        change = memory_rules.build_document(**fields(kind="change", commit=A))
        self.assertEqual(change["commits"], [A])

    def test_commits_are_checked_and_deduplicated(self):
        change = memory_rules.build_document(**fields(kind="change", commit=B, commits=[f" {A} ", A.upper(), B]))
        self.assertEqual(change["commits"], [A, B])
        message = "commits must be a list of commit hashes"
        self.rejects(message, kind="change", commit=B, commits=["nothex!!", B])
        self.rejects(message, kind="change", commit=B, commits=B)
        self.rejects(message, kind="change", commit=B, commits=[7, B])
        self.rejects("commits must include the commit the change explains", kind="change", commit=A, commits=[B])

    def test_pull_request_is_a_positive_number(self):
        for value in ("seven", "7", 0, -1, True, 7.0):
            self.rejects("pull_request must be a positive number", kind="change", commit=A, pull_request=value)

    def test_text_fields_must_be_strings(self):
        self.rejects("text must be text", text=["x"])
        self.rejects("occurred_at must be text", occurred_at=5)
        self.rejects("session id must be text", session_id=1)
        self.rejects("look_at must be text", look_at={"path": "x"})
        self.rejects("commit must be text", kind="change", commit=12345678)


class ApproveUsesTheRulesTest(unittest.TestCase):
    """approve() fills defaults from the bundle and identity, then builds with the shared rules."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        names = ("HOME", "AGENT_MEMORY_DIR")
        self.saved = {name: os.environ.get(name) for name in names}
        home = Path(self.tmp.name) / "home"
        config = home / ".config" / "agent-memory"
        config.mkdir(parents=True)
        (config / "identity").write_text("org = Org\nteam = Team\nperson = tester\n", encoding="utf-8")
        os.environ["HOME"] = str(home)
        os.environ["AGENT_MEMORY_DIR"] = str(Path(self.tmp.name) / "store")
        spec = importlib.util.spec_from_file_location("agent_memory_rules_under_test", SCRIPTS / "agent_memory.py")
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def bundle(self, session_id="s1"):
        self.cli.session_dir("git", session_id).mkdir(parents=True)
        return {
            "repo": str(Path(self.tmp.name) / "clone"),
            "tool": "git",
            "session_id": session_id,
            "ended_at": "2026-11-20T10:00:00+00:00",
            "land": {
                "remote_repo": "github.com/example/proj",
                "commits": [{"sha": A}, {"sha": B}],
                "pull_request": 4,
            },
        }

    def test_a_change_matches_build_document(self):
        approved = self.cli.approve(self.bundle(), "change", "code", "why\r\nit changed", commit=B, subject="x")
        expected = memory_rules.build_document(
            **fields(kind="change", text="why\nit changed", commit=B, commits=[A, B], pull_request=4, subject="x")
        )
        self.assertEqual(approved, expected)
        self.assertFalse(self.cli.session_dir("git", "s1").exists())

    def test_a_learning_matches_build_document(self):
        approved = self.cli.approve(self.bundle(), "learning", "operations", "watch the queue", record="runbook")
        expected = memory_rules.build_document(
            **fields(domain="operations", text="watch the queue", record="runbook")
        )
        self.assertEqual(approved, expected)

    def test_approve_reports_the_shared_messages(self):
        bundle = self.bundle()
        for args, kwargs, message in (
            (("note", "code", "x"), {}, "kind, domain, and text are required"),
            (("learning", "code", "x"), {"person": "a/b"}, "person must be a single path segment"),
            (("learning", "code", "x"), {"record": "diary"}, "record must be empty, explanation, history, or runbook"),
            (("change", "code", "x"), {"commit": "abc"}, "a change needs the commit it explains"),
        ):
            with self.assertRaises(ValueError) as raised:
                self.cli.approve(bundle, *args, **kwargs)
            self.assertEqual(str(raised.exception), message)
        self.assertTrue(self.cli.session_dir("git", "s1").exists())

    def test_cli_lists_are_the_shared_lists(self):
        self.assertIs(self.cli.KINDS, memory_rules.KINDS)
        self.assertIs(self.cli.DOMAINS, memory_rules.DOMAINS)
        self.assertIs(self.cli.RECORDS, memory_rules.RECORDS)


class ServerCommandTest(unittest.TestCase):
    def test_without_the_extra_it_prints_the_install_hint(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = agent_memory_server.main([], find_spec=lambda name: None)
        self.assertEqual(code, 2)
        self.assertIn("mcp, fastembed not installed", err.getvalue())
        self.assertIn("agent-memory[server] @ git+https://github.com/roune/agent-memory", err.getvalue())

    def test_with_the_extra_it_says_the_server_is_not_built_yet(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = agent_memory_server.main([], find_spec=lambda name: object())
        self.assertEqual(code, 1)
        self.assertIn("PR 2b", err.getvalue())


if __name__ == "__main__":
    unittest.main()
