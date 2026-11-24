#!/usr/bin/env python3
"""The laptop's old SQLite store, which migrate reads: metadata queries and commits. No Docker."""

import importlib.util
import json
import os
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_store():
    spec = importlib.util.spec_from_file_location(
        "memory_store_under_test",
        ROOT / "scripts" / "memory_store.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_embed():
    spec = importlib.util.spec_from_file_location(
        "embed_url_under_test",
        ROOT / "scripts" / "embed.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MemoryStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.store = Path(self.tmp.name) / "store"
        self.home.mkdir()
        self.store.mkdir()
        cfg = self.home / ".config" / "agent-memory"
        cfg.mkdir(parents=True)
        self._previous = {
            "HOME": os.environ.get("HOME"),
            "AGENT_MEMORY_DIR": os.environ.get("AGENT_MEMORY_DIR"),
            "AGENT_MEMORY_EMBEDDER": os.environ.get("AGENT_MEMORY_EMBEDDER"),
            "AGENT_MEMORY_EMBED_MODEL": os.environ.get("AGENT_MEMORY_EMBED_MODEL"),
        }
        os.environ.pop("AGENT_MEMORY_EMBEDDER", None)
        os.environ.pop("AGENT_MEMORY_EMBED_MODEL", None)
        os.environ["HOME"] = str(self.home)
        os.environ["AGENT_MEMORY_DIR"] = str(self.store)
        self.db = load_store()

    def tearDown(self):
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _doc(self, suffix, team, org, repo, occurred, text="note"):
        return {
            "id": f"person-learning-{suffix}",
            "kind": "learning",
            "domain": "code",
            "repo": repo,
            "tool": "cursor",
            "session_id": suffix,
            "text": text,
            "org": org,
            "team": team,
            "person": "person",
            "occurred_at": occurred,
        }

    def _rows(self, path):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute("SELECT * FROM memory")]
        finally:
            connection.close()

    def test_default_qdrant_url_is_loopback(self):
        self.assertEqual(self.db.qdrant_url(), "http://127.0.0.1:6333")

    def test_configured_qdrant_url_is_not_hardcoded(self):
        (self.home / ".config" / "agent-memory" / "database").write_text(
            "mode = shared\nqdrant_url = http://qdrant.internal:6333\n",
            encoding="utf-8",
        )
        self.assertEqual(self.db.qdrant_url(), "http://qdrant.internal:6333")

    def test_queries_use_team_org_and_repo_indexes(self):
        self.db.save_approved(
            self._doc("a", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00", "nov")
        )
        self.db.save_approved(
            self._doc("b", "Platform", "Other", "github.com/example/one", "2026-10-01T00:00:00+00:00", "oct")
        )
        self.db.save_approved(
            self._doc("c", "Other", "Acme", "github.com/example/two", "2026-11-03T00:00:00+00:00", "other")
        )
        since = "2026-11-01T00:00:00+00:00"
        team_hits = self.db.query_memories(team="Platform", since=since)
        self.assertEqual([hit["document_id"] for hit in team_hits], ["person-learning-a"])
        org_hits = self.db.query_memories(org="Acme", since=since)
        self.assertEqual(
            [hit["document_id"] for hit in org_hits],
            ["person-learning-c", "person-learning-a"],
        )
        repo_hits = self.db.query_memories(repo="github.com/example/one", since=since)
        self.assertEqual([hit["document_id"] for hit in repo_hits], ["person-learning-a"])
        both = self.db.query_memories(team="Platform", org="Acme", since=since)
        self.assertEqual([hit["document_id"] for hit in both], ["person-learning-a"])
        path = self.store / "data" / "memories.sqlite"
        connection = sqlite3.connect(path)
        try:
            plan = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT id FROM memory
                WHERE team = ? AND occurred_at >= ?
                ORDER BY occurred_at DESC, id DESC
                """,
                ("Platform", since),
            ).fetchall()
        finally:
            connection.close()
        self.assertIn("memory_team_occurred", " ".join(str(row) for row in plan))
        names = {row["name"] for row in self._rows(path)[:0]}
        connection = sqlite3.connect(path)
        try:
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(
            {"memory_team_occurred", "memory_org_occurred", "memory_repo_occurred"} <= indexes
        )

    def test_blank_filter_is_rejected(self):
        with self.assertRaises(ValueError):
            self.db.query_memories(team="  ")

    def test_missing_occurred_at_does_not_match_since(self):
        document = self._doc("bare", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00")
        del document["occurred_at"]
        self.db.save_approved(document)
        hits = self.db.query_memories(team="Platform", since="2020-01-01T00:00:00+00:00")
        self.assertEqual(hits, [])

    def test_supersedes_patches_the_old_row(self):
        self.db.save_approved(
            self._doc("old", "Platform", "Acme", "github.com/example/one", "2026-11-01T00:00:00+00:00")
        )
        newer = self._doc("new", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00")
        newer["supersedes"] = "person-learning-old"
        self.db.save_approved(newer)
        rows = {row["id"]: row for row in self._rows(self.store / "data" / "memories.sqlite")}
        self.assertEqual(rows["person-learning-old"]["superseded_by"], "person-learning-new")

    def test_per_repo_is_one_file_per_remote(self):
        config = self.home / ".config" / "agent-memory"
        (config / "database").write_text("mode = per-repo\n", encoding="utf-8")
        (config / "databases").write_text(
            "github.com/example/one = http://qdrant.internal:6340\n"
            "github.com/example/two = http://qdrant.internal:6341\n",
            encoding="utf-8",
        )
        self.db.save_approved(
            self._doc("a", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00")
        )
        self.db.save_approved(
            self._doc("b", "Other", "Acme", "github.com/example/two", "2026-11-03T00:00:00+00:00")
        )
        one = self.db.local_db_path("github.com/example/one")
        two = self.db.local_db_path("github.com/example/two")
        self.assertNotEqual(one, two)
        self.assertEqual(len(self._rows(one)), 1)
        self.assertEqual(len(self._rows(two)), 1)
        hits = self.db.query_memories(remote_id="github.com/example/one", team="Platform")
        self.assertEqual([hit["document_id"] for hit in hits], ["person-learning-a"])
        self.assertEqual(self.db.qdrant_url("github.com/example/one"), "http://qdrant.internal:6340")

    def _change(self, suffix, commits, pull_request=None):
        document = self._doc(suffix, "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00")
        document["id"] = f"person-change-{suffix}"
        document["kind"] = "change"
        document["commit"] = commits[-1]
        document["commits"] = commits
        if pull_request is not None:
            document["pull_request"] = pull_request
        return document

    def _commit_rows(self, path):
        connection = sqlite3.connect(path)
        try:
            return connection.execute(
                "SELECT memory_id, sha, position FROM memory_commit ORDER BY memory_id, position"
            ).fetchall()
        finally:
            connection.close()

    def test_change_stores_every_commit_and_the_pull_request(self):
        a, b, c = "a" * 40, "b" * 40, "c" * 40
        self.db.save_approved(self._change("pr", [a, b, c], pull_request=5))
        for sha in (a, b[:7], c.upper()):
            self.assertTrue(self.db.memory_exists_for_commit(sha), sha)
        self.assertFalse(self.db.memory_exists_for_commit("d" * 40))
        self.assertFalse(self.db.memory_exists_for_commit("a" * 6))
        hits = self.db.query_memories(kind="change")
        self.assertEqual(hits[0]["commits"], [a, b, c])
        self.assertEqual(hits[0]["pull_request"], 5)
        self.assertEqual(hits[0]["commit"], c)
        documents = self.db.pending_documents()
        self.assertEqual(documents[0]["commits"], [a, b, c])
        self.assertEqual(documents[0]["pull_request"], 5)

        # Approving the same id again replaces its commits instead of adding to them.
        self.db.save_approved(self._change("pr", [c]))
        path = self.store / "data" / "memories.sqlite"
        self.assertEqual(self._commit_rows(path), [("person-change-pr", c, 0)])
        self.assertFalse(self.db.memory_exists_for_commit(a))

    def test_abbreviated_stored_commit_matches_the_full_id(self):
        full = "1234567" + "e" * 33
        document = self._change("short", [full[:10]])
        del document["commits"]
        self.db.save_approved(document)
        self.assertTrue(self.db.memory_exists_for_commit(full))
        self.assertFalse(self.db.memory_exists_for_commit("1234567" + "f" * 33))

    def test_old_file_is_migrated_and_backfilled(self):
        path = self.store / "data" / "memories.sqlite"
        path.parent.mkdir(parents=True)
        old_schema = self.db.SCHEMA.split("CREATE TABLE IF NOT EXISTS memory_commit", 1)[0]
        old_schema = old_schema.replace("  pull_request INTEGER,\n", "")
        connection = sqlite3.connect(path)
        connection.executescript(old_schema)
        connection.execute(
            """
            INSERT INTO memory (id, kind, domain, repo, tool, session_id, text, commit_sha,
                                org, team, person, updated_at)
            VALUES ('old', 'change', 'code', 'github.com/example/one', 'git', 's', 'old note',
                    'ABCDEF1234', 'Acme', 'Platform', 'person', '2026-11-01T00:00:00+00:00')
            """
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()

        self.assertTrue(self.db.memory_exists_for_commit("abcdef1234" + "0" * 30))
        hits = self.db.query_memories()
        self.assertEqual(hits[0]["commits"], ["abcdef1234"])
        self.assertIsNone(hits[0]["pull_request"])
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(memory)")}
            self.assertIn("written_by", columns)
        finally:
            connection.close()

    def test_embed_payload_carries_the_pointer(self):
        a, b, c = "a" * 40, "b" * 40, "c" * 40
        self.db.save_approved(self._change("pr", [a, b, c], pull_request=5))
        self.db.save_approved(self._doc("note", "Platform", "Acme", "github.com/example/one", None))
        embed = load_embed()
        payloads = {doc["id"]: embed.payload_for(doc) for doc in self.db.pending_documents()}
        self.assertEqual(payloads["person-change-pr"]["commits"], [a, b, c])
        self.assertEqual(payloads["person-change-pr"]["pull_request"], 5)
        self.assertEqual(payloads["person-learning-note"]["commits"], [])
        self.assertNotIn("pull_request", payloads["person-learning-note"])

    def test_embed_ready_check_uses_the_configured_url(self):
        (self.home / ".config" / "agent-memory" / "database").write_text(
            "mode = shared\nqdrant_url = http://127.0.0.1:9\n",
            encoding="utf-8",
        )
        embed = load_embed()
        with self.assertRaises(SystemExit) as raised:
            embed.main([])
        self.assertIn("http://127.0.0.1:9", str(raised.exception.code))


if __name__ == "__main__":
    unittest.main()
