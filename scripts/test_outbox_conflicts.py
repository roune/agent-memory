#!/usr/bin/env python3
"""A queued memory that got a 409 has a way out: sync lists both texts, and --resolve settles it.

Standard library only. A stub server on 127.0.0.1 plays the agent-memory
server (stub_server.py), with a temporary HOME and AGENT_MEMORY_DIR, so nothing
touches the real ~/.config, the real outbox, data/ or Qdrant.
"""

import argparse
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory
import outbox
from test_cutover import CLI, REPO, Case, ReviewPageCase, dead_url

STORED_ID = "tester-learning-s1"


class ConflictCase(Case):
    """One memory kept online as s1, then a different text for s1 approved offline and refused with 409."""

    def conflict(self, queued_text="the newer text", stored_text="the stored text"):
        agent_memory.approve(self.bundle(), "learning", "code", stored_text)
        self.queue(queued_text, session_id="s1")
        outbox.sync(self.state, agent_memory.client_for)
        [(path, item)] = outbox.failed(self.state)
        self.assertEqual(item["status"], 409)
        self.server.requests.clear()
        return path.stem

    def run_sync(self, retry=False, resolve=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = agent_memory.sync_cmd(argparse.Namespace(retry_failed=retry, quiet=False, resolve=resolve))
        return code, out.getvalue(), err.getvalue()


class ListTest(ConflictCase):
    def test_sync_shows_the_queued_and_the_stored_text_and_both_choices(self):
        name = self.conflict()
        code, out, _ = self.run_sync()
        self.assertEqual(code, 0, "nothing is waiting, and nothing failed in this run")
        self.assertIn("the newer text", out)
        self.assertIn("the stored text", out)
        self.assertIn(STORED_ID, out)
        self.assertIn(f"agent-memory sync --resolve {name} supersede", out)
        self.assertIn(f"agent-memory sync --resolve {name} drop", out)

    def test_the_summary_line_names_resolve_and_not_only_retry(self):
        self.conflict()
        code, out, _ = self.run_sync(retry=True)
        self.assertEqual(code, 1, "the retry got the 409 again")
        self.assertNotIn("fix them, then run", out)
        self.assertIn("--resolve <item> supersede|drop", out)

    def test_another_4xx_is_listed_with_retry_and_drop(self):
        path = self.queue("bad one")
        self.server.fail_next = (400, {"error": "occurred_at needs a time zone"})
        code, out, _ = self.run_sync()
        self.assertEqual(code, 1)
        self.assertIn("occurred_at needs a time zone", out)
        self.assertIn("bad one", out)
        self.assertIn(f"agent-memory sync --resolve {path.stem} drop", out)
        self.assertNotIn("supersede", out.split("In ", 1)[1])

    def test_quiet_sync_prints_nothing(self):
        self.conflict()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            agent_memory.sync_cmd(argparse.Namespace(retry_failed=False, quiet=True, resolve=None))
        self.assertEqual(out.getvalue(), "")


class SupersedeTest(ConflictCase):
    def test_supersede_sends_the_queued_text_as_a_new_memory_that_supersedes_the_stored_one(self):
        name = self.conflict()
        code, out, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 0, err)
        new = self.server.rows[f"{STORED_ID}-2"]
        self.assertEqual((new["text"], new["supersedes"], new["session_id"]), ("the newer text", STORED_ID, "s1-2"))
        old = self.server.rows[STORED_ID]
        self.assertEqual((old["text"], old["superseded_by"]), ("the stored text", f"{STORED_ID}-2"))
        self.assertEqual(self.failed(), [])
        self.assertIn(f"sent as {STORED_ID}-2, which supersedes {STORED_ID}", out)

    def test_the_item_name_may_carry_json(self):
        name = self.conflict()
        code, _, err = self.run_sync(resolve=[name + ".json", "supersede"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"{STORED_ID}-2", self.server.rows)

    def test_supersede_names_the_newest_memory_of_the_chain(self):
        name = self.conflict()
        # Since the 409, a note replaced the stored memory with s1-2.
        path = self.queue("a later fix", session_id="s1-2")
        item = outbox._read(path)
        item["body"]["supersedes"] = STORED_ID
        outbox.write_private(path, item)
        outbox.sync(self.state, agent_memory.client_for)
        self.assertEqual(self.server.rows[STORED_ID]["superseded_by"], f"{STORED_ID}-2")
        code, out, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 0, err)
        new = self.server.rows[f"{STORED_ID}-3"]
        self.assertEqual((new["text"], new["supersedes"]), ("the newer text", f"{STORED_ID}-2"))
        self.assertEqual(self.server.rows[STORED_ID]["superseded_by"], f"{STORED_ID}-2", "the chain stays in one line")
        self.assertEqual(self.server.rows[f"{STORED_ID}-2"]["superseded_by"], f"{STORED_ID}-3")

    def test_a_resolve_that_already_got_through_sends_nothing_new(self):
        name = self.conflict()
        path = outbox.failed_root(self.state) / f"{name}.json"
        kept = path.read_bytes()
        self.assertEqual(self.run_sync(resolve=[name, "supersede"])[0], 0)
        # As if the answer was lost and the item stayed: put it back and resolve again.
        path.write_bytes(kept)
        self.server.requests.clear()
        code, out, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.posts("/v1/memories"), [])
        self.assertNotIn(f"{STORED_ID}-3", self.server.rows)
        self.assertEqual(self.failed(), [])
        self.assertIn(f"{STORED_ID}-2 already keeps this text", out)

    def test_a_taken_suffix_is_skipped(self):
        name = self.conflict()
        # s1-2 holds unrelated text and is not part of s1's chain.
        self.queue("unrelated", session_id="s1-2")
        outbox.sync(self.state, agent_memory.client_for)
        code, _, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.rows[f"{STORED_ID}-3"]["supersedes"], STORED_ID)
        self.assertEqual(self.server.rows[f"{STORED_ID}-2"]["text"], "unrelated")

    def test_a_token_without_read_falls_back_to_the_stored_id(self):
        name = self.conflict()
        self.server.fail_path[f"/v1/memories/{STORED_ID}"] = (403, {"error": "this token lacks the read scope"})
        code, _, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.server.rows[f"{STORED_ID}-2"]["supersedes"], STORED_ID)

    def test_a_link_the_queued_memory_had_is_reported(self):
        agent_memory.approve(self.bundle(), "learning", "code", "the stored text")
        body = {
            "kind": "learning", "domain": "code", "text": "the newer text", "repo": REPO, "tool": "git",
            "session_id": "s1", "org": "Org", "team": "Team", "person": "tester",
            "occurred_at": "2026-11-20T10:00:00+00:00", "supersedes": "tester-learning-older",
        }
        outbox.add(self.state, self.server.url, "memory", body)
        outbox.sync(self.state, agent_memory.client_for)
        [(path, _)] = outbox.failed(self.state)
        code, out, err = self.run_sync(resolve=[path.stem, "supersede"])
        self.assertEqual(code, 0, err)
        self.assertIn("tester-learning-older", out)

    def test_while_the_server_is_down_the_item_and_its_text_stay(self):
        name = self.conflict()
        path = outbox.failed_root(self.state) / f"{name}.json"
        # The item keeps the url it was queued for: point it at one that doesn't answer.
        item = outbox._read(path)
        item["server_url"] = dead_url()
        outbox.write_private(path, item)
        before = path.read_bytes()
        code, _, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 1)
        self.assertIn("did not answer", err)
        self.assertIn("stays", err)
        self.assertEqual(path.read_bytes(), before)

    def test_another_refusal_keeps_the_item(self):
        name = self.conflict()
        self.server.fail_path["/v1/memories"] = (400, {"error": "occurred_at needs a time zone"})
        code, _, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 1)
        self.assertIn("occurred_at needs a time zone", err)
        self.assertEqual(len(self.failed()), 1)

    def test_supersede_is_refused_for_an_item_that_is_not_a_409(self):
        path = self.queue("bad one")
        self.server.fail_next = (400, {"error": "bad"})
        outbox.sync(self.state, agent_memory.client_for)
        code, _, err = self.run_sync(resolve=[path.stem, "supersede"])
        self.assertEqual(code, 1)
        self.assertIn("supersede is for a memory that got a 409", err)
        self.assertEqual(len(self.failed()), 1)

    def test_a_stored_memory_the_server_lost_points_at_retry(self):
        name = self.conflict()
        del self.server.rows[STORED_ID]
        code, _, err = self.run_sync(resolve=[name, "supersede"])
        self.assertEqual(code, 1)
        self.assertIn("--retry-failed", err)
        self.assertEqual(len(self.failed()), 1)


class SupersedeWithTokenTest(SupersedeTest):
    """With auth on the body has no person: the server takes it from the token, and the id is the same."""

    token = "am_laptop"


class DropTest(ConflictCase):
    def test_drop_deletes_the_item_prints_its_text_and_leaves_the_server_alone(self):
        name = self.conflict()
        code, out, err = self.run_sync(resolve=[name, "drop"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.failed(), [])
        self.assertEqual(self.server.posts("/v1/memories"), [])
        self.assertEqual(self.server.rows[STORED_ID]["text"], "the stored text")
        self.assertIn(f"the server keeps {STORED_ID}", out)
        self.assertIn("the newer text", out)

    def test_drop_works_for_an_unreadable_item(self):
        folder = outbox.failed_root(self.state)
        folder.mkdir(parents=True)
        (folder / "20261124T000000Z-junk.json").write_text("not json", encoding="utf-8")
        code, _, err = self.run_sync(resolve=["20261124T000000Z-junk", "drop"])
        self.assertEqual(code, 0, err)
        self.assertFalse((folder / "20261124T000000Z-junk.json").exists())


class NamesTest(ConflictCase):
    def test_an_unknown_item_or_a_path_is_refused(self):
        self.conflict()
        for name in ("nope", "../outbox/x", "/etc/passwd", ".lock", ""):
            with self.subTest(name=name):
                code, _, err = self.run_sync(resolve=[name, "drop"])
                self.assertEqual(code, 1)
                self.assertIn("agent-memory sync lists them", err)
        self.assertEqual(len(self.failed()), 1)

    def test_an_unknown_choice_is_refused(self):
        name = self.conflict()
        code, _, err = self.run_sync(resolve=[name, "keep"])
        self.assertEqual(code, 1)
        self.assertIn("supersede or drop", err)
        self.assertEqual(len(self.failed()), 1)


class CommandLineTest(ConflictCase):
    def cli(self, *args):
        return subprocess.run(CLI + list(args), env=self.env(), text=True, capture_output=True, timeout=60)

    def test_sync_resolve_from_the_command_line(self):
        name = self.conflict()
        result = self.cli("sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--resolve {name} supersede", result.stdout)
        result = self.cli("sync", "--resolve", name, "supersede")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{STORED_ID}-2", self.server.rows)

    def test_resolve_and_retry_failed_are_one_or_the_other(self):
        name = self.conflict()
        result = self.cli("sync", "--retry-failed", "--resolve", name, "drop")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(self.failed()), 1)


class ReviewPageTest(ReviewPageCase):
    def test_the_page_names_both_choices_for_a_409(self):
        agent_memory.approve(self.bundle(), "learning", "code", "kept text")
        path = self.queue("clashing text", session_id="s1")
        outbox.sync(self.state, agent_memory.client_for)
        page = self.get()
        self.assertIn(f"agent-memory sync --resolve {path.stem} supersede", page)
        self.assertIn(f"agent-memory sync --resolve {path.stem} drop", page)
        self.assertNotIn("A newer note replaces it through Supersedes", page)


if __name__ == "__main__":
    unittest.main()
