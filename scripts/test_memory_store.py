#!/usr/bin/env python3
"""SQLite store, metadata queries, import, and flush. No Docker."""

import importlib.util
import json
import os
import sqlite3
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/readyz":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        return


def start_ready():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReadyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


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
        }
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
                ORDER BY occurred_at DESC
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

    def test_import_moves_json_once_and_does_not_overwrite(self):
        folder = self.store / "memories"
        folder.mkdir()
        document = self._doc("old", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00")
        (folder / "person-learning-old.json").write_text(json.dumps(document), encoding="utf-8")
        (folder / "bad.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.db.import_json(), 0)
        self.assertTrue(folder.is_dir())
        self.assertTrue((folder / "bad.json").is_file())
        rows = self._rows(self.store / "data" / "memories.sqlite")
        self.assertEqual(len(rows), 1)
        (folder / "bad.json").unlink()
        self.assertEqual(self.db.import_json(), 0)
        self.assertFalse(folder.exists())
        self.assertTrue((self.store / "memories.imported").is_dir())
        connection = sqlite3.connect(self.store / "data" / "memories.sqlite")
        connection.execute("UPDATE memory SET text = 'kept' WHERE id = 'person-learning-old'")
        connection.commit()
        connection.close()
        again = self.store / "memories"
        again.mkdir()
        document["text"] = "replaced"
        (again / "person-learning-old.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.db.import_json(), 0)
        rows = self._rows(self.store / "data" / "memories.sqlite")
        self.assertEqual(rows[0]["text"], "kept")
        self.assertEqual(self.db.import_json(), 0)

    def test_missing_occurred_at_does_not_match_since(self):
        folder = self.store / "memories"
        folder.mkdir()
        document = self._doc("bare", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00")
        del document["occurred_at"]
        (folder / "person-learning-bare.json").write_text(json.dumps(document), encoding="utf-8")
        self.db.import_json()
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

    def test_flush_leaves_the_laptop_unchanged_when_the_container_is_down(self):
        (self.home / ".config" / "agent-memory" / "database").write_text(
            "mode = shared\nqdrant_url = http://127.0.0.1:9\n",
            encoding="utf-8",
        )
        self.db.save_approved(
            self._doc("a", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00")
        )
        self.assertEqual(self.db.flush(), 0)
        rows = self._rows(self.store / "data" / "memories.sqlite")
        self.assertIsNone(rows[0]["flushed_at"])
        self.assertFalse((self.store / "data" / "shared" / "memories.sqlite").exists())

    def test_flush_copies_and_resolves_conflicts(self):
        server, url = start_ready()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        (self.home / ".config" / "agent-memory" / "database").write_text(
            f"mode = shared\nqdrant_url = {url}\n",
            encoding="utf-8",
        )
        self.db.save_approved(
            self._doc("a", "Platform", "Acme", "github.com/example/one", "2026-11-02T00:00:00+00:00", "local")
        )
        self.assertEqual(self.db.flush(), 0)
        shared = self.store / "data" / "shared" / "memories.sqlite"
        shared_rows = self._rows(shared)
        self.assertEqual(shared_rows[0]["text"], "local")
        local_rows = self._rows(self.store / "data" / "memories.sqlite")
        self.assertEqual(local_rows[0]["flushed_at"], local_rows[0]["updated_at"])
        self.assertEqual(self.db.flush(), 0)

        connection = sqlite3.connect(shared)
        connection.execute(
            "UPDATE memory SET text = 'shared-newer', updated_at = '2999-01-01T00:00:00+00:00' WHERE id = 'person-learning-a'"
        )
        connection.commit()
        connection.close()
        local = sqlite3.connect(self.store / "data" / "memories.sqlite")
        local.execute("UPDATE memory SET flushed_at = NULL WHERE id = 'person-learning-a'")
        local.commit()
        local.close()
        self.assertEqual(self.db.flush(), 0)
        self.assertEqual(self._rows(shared)[0]["text"], "shared-newer")

        stamped = self._rows(self.store / "data" / "memories.sqlite")[0]["updated_at"]
        connection = sqlite3.connect(shared)
        connection.execute(
            "UPDATE memory SET text = 'clash', updated_at = ? WHERE id = 'person-learning-a'",
            (stamped,),
        )
        connection.commit()
        connection.close()
        local = sqlite3.connect(self.store / "data" / "memories.sqlite")
        local.execute("UPDATE memory SET flushed_at = NULL WHERE id = 'person-learning-a'")
        local.commit()
        local.close()
        self.assertEqual(self.db.flush(), 0)
        self.assertEqual(self._rows(shared)[0]["text"], "clash")
        self.assertIsNone(self._rows(self.store / "data" / "memories.sqlite")[0]["flushed_at"])

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
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            connection.close()

    def test_flush_copies_commit_rows(self):
        server, url = start_ready()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        (self.home / ".config" / "agent-memory" / "database").write_text(
            f"mode = shared\nqdrant_url = {url}\n",
            encoding="utf-8",
        )
        a, b = "a" * 40, "b" * 40
        self.db.save_approved(self._change("pr", [a, b], pull_request=9))
        self.assertEqual(self.db.flush(), 0)
        shared = self.store / "data" / "shared" / "memories.sqlite"
        self.assertEqual(
            self._commit_rows(shared),
            [("person-change-pr", a, 0), ("person-change-pr", b, 1)],
        )
        self.assertEqual(self._rows(shared)[0]["pull_request"], 9)

    def test_import_writes_commit_rows(self):
        folder = self.store / "memories"
        folder.mkdir()
        document = self._change("imported", ["f" * 40])
        del document["commits"]
        (folder / f"{document['id']}.json").write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.db.import_json(), 0)
        self.assertTrue(self.db.memory_exists_for_commit("f" * 40))

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
