#!/usr/bin/env python3
"""store_core.Store on a plain path, and the laptop wrappers that resolve one."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import store_core

A = "a" * 40
B = "b" * 40
REPO = "github.com/example/proj"


def load_memory_store():
    spec = importlib.util.spec_from_file_location("memory_store_core_under_test", SCRIPTS / "memory_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document(session_id="1", kind="learning", text="a note", **extra):
    doc = {
        "id": f"tester-{kind}-{session_id}",
        "kind": kind,
        "domain": "code",
        "repo": REPO,
        "tool": "git",
        "session_id": session_id,
        "text": text,
        "org": "Org",
        "team": "Team",
        "person": "tester",
        "occurred_at": "2026-11-20T10:00:00+00:00",
    }
    doc.update(extra)
    return doc


class StoreOnAPathTest(unittest.TestCase):
    """No HOME, no AGENT_MEMORY_DIR, no ~/.config: only the path given."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {name: os.environ.get(name) for name in ("HOME", "AGENT_MEMORY_DIR")}
        os.environ["HOME"] = str(Path(self.tmp.name) / "no-such-home")
        os.environ.pop("AGENT_MEMORY_DIR", None)
        self.path = Path(self.tmp.name) / "server" / "memories.sqlite"
        self.store = store_core.Store(self.path)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_reads_of_a_missing_file_return_nothing_and_create_nothing(self):
        self.assertEqual(self.store.query_memories(), [])
        self.assertIsNone(self.store.get_memory("tester-learning-1"))
        self.assertEqual(self.store.pending_documents(), [])
        self.assertFalse(self.store.memory_exists_for_commit(A))
        self.assertFalse(self.store.commit_rejected(A, REPO))
        self.assertFalse(self.path.exists())
        self.assertFalse((Path(self.tmp.name) / "no-such-home").exists())

    def test_save_then_read_back_by_query_and_id(self):
        self.store.save_approved(document())
        self.store.save_approved(document("2", kind="change", text="the change", commits=[A, B], pull_request=7))
        hits = self.store.query_memories(repo=REPO)
        self.assertEqual({hit["document_id"] for hit in hits}, {"tester-learning-1", "tester-change-2"})
        change = self.store.get_memory("tester-change-2")
        self.assertEqual(change["commits"], [A, B])
        self.assertEqual(change["pull_request"], 7)
        self.assertEqual(change["text"], "the change")
        self.assertTrue(self.path.is_file())

    def test_get_memory_needs_an_id(self):
        with self.assertRaises(ValueError):
            self.store.get_memory("  ")

    def test_commit_lookup_matches_abbreviated_hashes_both_ways(self):
        self.store.save_approved(document("2", kind="change", commits=[A[:10]]))
        self.assertTrue(self.store.memory_exists_for_commit(A))
        self.assertTrue(self.store.memory_exists_for_commit(A[:7]))
        self.assertFalse(self.store.memory_exists_for_commit(B))

    def test_rejections_are_kept_per_repo(self):
        self.assertEqual(self.store.reject_commits(REPO, [A, "not-a-sha"]), 1)
        self.assertTrue(self.store.commit_rejected(A[:8], REPO))
        self.assertFalse(self.store.commit_rejected(A, "github.com/example/fork"))

    def test_pending_rows_and_mark_embedded(self):
        self.store.save_approved(document())
        pending = self.store.pending_documents()
        self.assertEqual([doc["id"] for doc in pending], ["tester-learning-1"])
        self.store.mark_embedded([(doc["id"], doc["updated_at"]) for doc in pending])
        self.assertEqual(self.store.pending_documents(), [])
        self.assertEqual(len(self.store.pending_documents(rebuild=True)), 1)

    def test_supersedes_stamps_the_old_row(self):
        self.store.save_approved(document("1"))
        self.store.save_approved(document("2", supersedes="tester-learning-1"))
        self.assertEqual(self.store.get_memory("tester-learning-1")["superseded_by"], "tester-learning-2")


class LaptopWrapperTest(unittest.TestCase):
    """memory_store resolves the laptop file and hands it to Store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {name: os.environ.get(name) for name in ("HOME", "AGENT_MEMORY_DIR")}
        home = Path(self.tmp.name) / "home"
        (home / ".config" / "agent-memory").mkdir(parents=True)
        os.environ["HOME"] = str(home)
        os.environ["AGENT_MEMORY_DIR"] = str(Path(self.tmp.name) / "store")
        self.db = load_memory_store()

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_wrappers_use_the_store_under_agent_memory_dir(self):
        self.db.save_approved(document())
        expected = Path(self.tmp.name) / "store" / "data" / "memories.sqlite"
        self.assertEqual(self.db.local_store().path, expected)
        self.assertTrue(expected.is_file())
        self.assertEqual(self.db.get_memory("tester-learning-1")["text"], "a note")
        self.assertIsNone(self.db.get_memory("tester-learning-9"))

    def test_store_error_is_the_core_class(self):
        self.assertIs(self.db.StoreError, store_core.StoreError)

    def test_per_repo_mode_without_an_id_is_still_false_for_lookups(self):
        (Path(os.environ["HOME"]) / ".config" / "agent-memory" / "database").write_text(
            "mode = per-repo\n", encoding="utf-8"
        )
        self.assertFalse(self.db.memory_exists_for_commit(A))
        self.assertFalse(self.db.commit_rejected(A))
        with self.assertRaises(self.db.StoreError):
            self.db.save_approved(document(repo=""))


if __name__ == "__main__":
    unittest.main()
