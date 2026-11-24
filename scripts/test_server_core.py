#!/usr/bin/env python3
"""The server's standard-library parts: store, request rules, embedding, reindex, lock, CLI.

No Starlette, no Docker, no model: HashEmbedder and FakeQdrant stand in.
test_server_app.py covers the HTTP routes when the server extra is installed.
"""

import io
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import memory_rules
import store_core
from agent_memory_server import cli, rules
from agent_memory_server.embedding import Embedding, EmbeddingMismatch, collection_name, describe, points_for
from agent_memory_server.lock import ServerLock
from agent_memory_server.qdrant import Qdrant
from agent_memory_server.reindex import ReindexError, reindex
from agent_memory_server.settings import Settings
from agent_memory_server.store import ServerStore
from server_doubles import FakeQdrant, HashEmbedder

A = "a" * 40
B = "b" * 40
REPO = "github.com/example/proj"


def body(**changes):
    base = {
        "kind": "learning",
        "domain": "code",
        "text": "a note",
        "repo": REPO,
        "tool": "git",
        "session_id": "s1",
        "org": "Org",
        "team": "Team",
        "person": "tester",
        "occurred_at": "2026-11-20T10:00:00+00:00",
    }
    base.update(changes)
    return base


def document(**changes):
    return rules.memory_from_body(body(**changes))


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved = {name: os.environ.get(name) for name in ("HOME", "AGENT_MEMORY_DIR")}
        os.environ["HOME"] = str(Path(self.tmp.name) / "no-such-home")
        os.environ.pop("AGENT_MEMORY_DIR", None)
        self.addCleanup(self._restore)
        self.data = Path(self.tmp.name) / "data"
        self.store = ServerStore(self.data / "memories.sqlite")
        self.fake = FakeQdrant()
        self.qdrant = Qdrant("http://qdrant:6333", transport=self.fake)
        self.model = HashEmbedder()
        quiet = logging.NullHandler()
        logging.getLogger("agent_memory_server").addHandler(quiet)
        self.addCleanup(logging.getLogger("agent_memory_server").removeHandler, quiet)

    def _restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def embedding(self, model=None):
        return Embedding(self.store, model or self.model, self.qdrant)


class CollectionNameTest(unittest.TestCase):
    def test_the_default_name(self):
        self.assertEqual(
            collection_name("fastembed", "BAAI/bge-small-en-v1.5", "aa8f8b060edb00e03bfdd08813a2949946c8ba55", 384),
            "memories-fastembed-384-2d705fdd",
        )

    def test_dimensions_and_revisions_give_distinct_names(self):
        names = {
            collection_name("openai", "text-embedding-3-small", "", 512),
            collection_name("openai", "text-embedding-3-small", "", 1536),
            collection_name("bedrock", "amazon.titan-embed-text-v2", "amazon.titan-embed-text-v2:0", 1024),
            collection_name("bedrock", "amazon.titan-embed-text-v2", "amazon.titan-embed-text-v2:1", 1024),
        }
        self.assertEqual(len(names), 4)
        for name in names:
            self.assertRegex(name, r"^memories-[a-z]+-\d+-[0-9a-f]{8}$")


class SchemaTest(ServerCase):
    def test_a_v2_file_gets_written_by_and_the_new_tables(self):
        path = self.data / "old.sqlite"
        path.parent.mkdir(parents=True)
        connection = sqlite3.connect(path)
        connection.executescript(store_core.SCHEMA.replace(",\n  written_by TEXT NOT NULL DEFAULT ''", ""))
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        connection.close()
        store = ServerStore(path)
        self.assertEqual(store.write_memory(document(), "local")[0], "created")
        connection = sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT written_by FROM memory").fetchone()[0], "local")
        finally:
            connection.close()


class RulesTest(unittest.TestCase):
    def test_unknown_keys_and_transcript_path_are_refused(self):
        with self.assertRaisesRegex(ValueError, "^unknown field: transcript_path"):
            rules.memory_from_body(body(transcript_path="/Users/me/.claude/x.jsonl"))
        with self.assertRaisesRegex(ValueError, "^unknown field: embedded_at, raw$"):
            rules.memory_from_body(body(raw="x", embedded_at="now"))
        with self.assertRaisesRegex(ValueError, "JSON object"):
            rules.memory_from_body(["not", "an", "object"])

    def test_the_document_matches_approve(self):
        built = rules.memory_from_body(body(kind="change", commit=B, commits=[A, B], pull_request=4))
        expected = memory_rules.build_document(**{**body(kind="change"), "commit": B, "commits": [A, B], "pull_request": 4})
        self.assertEqual(built, expected)

    def test_an_id_must_be_the_one_the_fields_give(self):
        self.assertEqual(rules.memory_from_body(body(id="tester-learning-s1"))["id"], "tester-learning-s1")
        with self.assertRaisesRegex(ValueError, "id must be tester-learning-s1"):
            rules.memory_from_body(body(id="someone-else"))

    def test_the_shared_rules_apply(self):
        for changes, message in (
            ({"kind": "note"}, "kind, domain, and text are required"),
            ({"text": ["x"]}, "text must be text"),
            ({"person": "a/b"}, "person must be a single path segment"),
            ({"kind": "change", "commit": A, "commits": [B]}, "commits must include the commit the change explains"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                rules.memory_from_body(body(**changes))

    def test_commit_bodies(self):
        self.assertEqual(rules.commits_from_body({"repo": f" {REPO} ", "shas": [A[:7]]}), (REPO, [A[:7]]))
        for bad in ({"shas": [A]}, {"repo": REPO, "shas": []}, {"repo": REPO, "shas": ["zz"]}, {"repo": REPO, "shas": [A], "x": 1}):
            with self.assertRaises(ValueError):
                rules.commits_from_body(bad)

    def test_import_bodies(self):
        row = {**body(), "id": "tester-learning-s1", "updated_at": "2026-11-20T10:00:00+00:00", "commits": []}
        memories, rejections = rules.import_from_body({"memories": [row], "rejections": [{"repo": REPO, "sha": A}]})
        self.assertEqual(memories[0]["id"], "tester-learning-s1")
        self.assertEqual(rejections, [{"repo": REPO, "sha": A, "rejected_at": ""}])
        for bad in (
            {"memories": [{**row, "transcript_path": "x"}]},
            {"memories": [{**row, "updated_at": ""}]},
            {"memories": [{**row, "commits": ["nothex!!"]}]},
            {"memories": [row] * 501},
            {"other": []},
        ):
            with self.assertRaises(ValueError):
                rules.import_from_body(bad)

    def test_import_timestamps_are_checked_and_written_as_utc(self):
        row = {**body(), "id": "tester-learning-s1", "commits": []}
        memories, rejections = rules.import_from_body(
            {
                "memories": [{**row, "updated_at": "2026-11-01T01:30:00Z"}],
                "rejections": [{"repo": REPO, "sha": A, "rejected_at": "2026-11-01T02:00:00+01:00"}],
            }
        )
        self.assertEqual(memories[0]["updated_at"], "2026-11-01T01:30:00+00:00")
        self.assertEqual(rejections[0]["rejected_at"], "2026-11-01T01:00:00+00:00")
        offsets = rules.import_from_body(
            {
                "memories": [
                    {**row, "updated_at": "2026-11-01T01:30:00Z", "occurred_at": "2026-11-01T02:00:00+01:00"},
                    {**row, "id": "tester-learning-s2", "session_id": "s2", "updated_at": "2026-11-01T01:30:00Z", "occurred_at": None},
                ]
            }
        )[0]
        self.assertEqual([item["occurred_at"] for item in offsets], ["2026-11-01T01:00:00+00:00", None])
        with self.assertRaisesRegex(ValueError, "tester-learning-s1: occurred_at needs a time zone"):
            rules.import_from_body({"memories": [{**row, "updated_at": "2026-11-01T01:30:00Z", "occurred_at": "2026-11-01T02:00:00"}]})
        for bad, message in (
            ("not-a-time", "tester-learning-s1: updated_at must be an ISO-8601 time"),
            ("2026-11-01T00:00:00", "tester-learning-s1: updated_at needs a time zone"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                rules.import_from_body({"memories": [{**row, "updated_at": bad}]})
        with self.assertRaisesRegex(ValueError, "rejected_at must be an ISO-8601 time"):
            rules.import_from_body({"rejections": [{"repo": REPO, "sha": A, "rejected_at": "yesterday"}]})

    def test_query_params(self):
        self.assertEqual(rules.query_from_params({"team": "T", "limit": "5"}), {"team": "T", "limit": 5})
        with self.assertRaisesRegex(ValueError, "unknown parameter: q"):
            rules.query_from_params({"q": "x"})
        with self.assertRaisesRegex(ValueError, "limit must be a number"):
            rules.query_from_params({"limit": "many"})


class WriteTest(ServerCase):
    def test_new_then_identical_then_different(self):
        outcome, stored = self.store.write_memory(document(), "local")
        self.assertEqual((outcome, stored["document_id"]), ("created", "tester-learning-s1"))
        again, same = self.store.write_memory(document(), "local")
        self.assertEqual((again, same["updated_at"]), ("unchanged", stored["updated_at"]))
        clash, kept = self.store.write_memory(document(text="other words"), "local")
        self.assertEqual((clash, kept["text"]), ("conflict", "a note"))
        self.assertEqual(self.store.get_memory("tester-learning-s1")["text"], "a note")

    def test_different_commits_are_a_conflict(self):
        self.store.write_memory(document(kind="change", commit=A), "local")
        self.assertEqual(self.store.write_memory(document(kind="change", commit=B), "local")[0], "conflict")

    def test_a_resend_after_being_superseded_is_still_identical(self):
        self.store.write_memory(document(session_id="s1"), "local")
        self.store.write_memory(document(session_id="s2", supersedes="tester-learning-s1"), "local")
        self.assertEqual(self.store.get_memory("tester-learning-s1")["superseded_by"], "tester-learning-s2")
        self.assertEqual(self.store.write_memory(document(session_id="s1"), "local")[0], "unchanged")

    def test_import_keeps_ids_and_the_newer_row_wins(self):
        old = {**body(text="old"), "id": "someone-learning-x", "person": "someone", "updated_at": "2026-11-01T00:00:00+00:00"}
        result = self.store.import_rows([old], [{"repo": REPO, "sha": A, "rejected_at": ""}], "local")
        self.assertEqual(result, {"written": 1, "skipped": 0, "rejections": 1, "newer": []})
        newer = {**old, "text": "newer", "updated_at": "2026-11-02T00:00:00+00:00"}
        older = {**old, "text": "older", "updated_at": "2026-10-01T00:00:00+00:00"}
        self.assertEqual(self.store.import_rows([newer], [], "local")["written"], 1)
        self.assertEqual(
            self.store.import_rows([older, newer], [], "local"),
            {"written": 0, "skipped": 2, "rejections": 0, "newer": ["someone-learning-x"]},
            "the older one is reported as newer on the server; the same updated_at is not",
        )
        self.assertEqual(self.store.import_rows([newer], [], "local")["newer"], [])
        kept = self.store.get_memory("someone-learning-x")
        self.assertEqual((kept["text"], kept["person"]), ("newer", "someone"))
        self.assertTrue(self.store.commit_rejected(A[:9], REPO))


class EmbeddingTest(ServerCase):
    def test_prepare_creates_the_collection_with_metadata_and_records_the_state(self):
        embedding = self.embedding()
        embedding.prepare()
        created = self.fake.collections[embedding.collection]
        self.assertEqual(created["vectors"], {"size": 8, "distance": "Cosine"})
        self.assertEqual(created["metadata"]["model"], "test/hash")
        self.assertEqual(created["indexes"]["pull_request"], "integer")
        self.assertEqual(self.store.embedding_state()["collection"], embedding.collection)

    def test_writes_are_embedded_with_the_provider_in_the_payload(self):
        embedding = self.embedding()
        self.store.write_memory(document(kind="change", commit=A, pull_request=3), "local")
        self.assertTrue(embedding.embed_pending())
        payload = self.fake.payloads(embedding.collection)["tester-change-s1"]
        self.assertEqual(payload["embedding_provider"], "hash")
        self.assertEqual((payload["commits"], payload["pull_request"]), ([A], 3))
        self.assertTrue(self.store.is_embedded("tester-change-s1", embedding.collection))
        self.assertEqual(self.store.count_pending(embedding.collection), 0)

    def test_superseding_embeds_the_old_row_again(self):
        embedding = self.embedding()
        self.store.write_memory(document(session_id="s1"), "local")
        embedding.embed_pending()
        self.store.write_memory(document(session_id="s2", supersedes="tester-learning-s1"), "local")
        self.assertEqual(self.store.count_pending(embedding.collection), 2)
        embedding.embed_pending()
        payloads = self.fake.payloads(embedding.collection)
        self.assertEqual(payloads["tester-learning-s1"]["superseded_by"], "tester-learning-s2")

    def test_a_same_second_supersede_during_an_embed_is_embedded_again(self):
        embedding = self.embedding()
        embedding.prepare()
        self.store.write_memory(document(session_id="s1"), "local")
        read_before = self.store.pending_for(embedding.collection)
        self.store.write_memory(document(session_id="s2", supersedes="tester-learning-s1"), "local")
        # The embed that read s1 before the supersede finishes now, with s1's old payload.
        vectors = self.model.embed_documents([doc["text"] for doc in read_before])
        self.qdrant.upsert(embedding.collection, points_for(read_before, vectors, embedding.description))
        self.store.mark_embedded_in(embedding.collection, [(doc["id"], doc["updated_at"]) for doc in read_before])
        self.assertEqual(self.store.count_pending(embedding.collection), 2)
        embedding.embed_pending()
        payload = self.fake.payloads(embedding.collection)["tester-learning-s1"]
        self.assertEqual(payload["superseded_by"], "tester-learning-s2")

    def test_qdrant_down_keeps_the_row_and_a_later_try_embeds_it(self):
        embedding = self.embedding()
        self.fake.down = True
        self.assertEqual(self.store.write_memory(document(), "local")[0], "created")
        with self.assertLogs("agent_memory_server", "WARNING"):
            self.assertFalse(embedding.embed_pending())
        self.assertEqual((embedding.state, self.store.count_pending(embedding.collection)), ("pending", 1))
        self.assertIn("not reachable", embedding.status()["error"])
        self.fake.down = False
        self.assertTrue(embedding.embed_pending())
        self.assertEqual((embedding.state, embedding.status()["error"]), ("ready", ""))
        self.assertTrue(self.store.is_embedded("tester-learning-s1", embedding.collection))

    def test_a_model_error_does_not_raise(self):
        broken = HashEmbedder()
        broken.embed_documents = lambda texts: (_ for _ in ()).throw(RuntimeError("onnx went away"))
        embedding = self.embedding(broken)
        self.store.write_memory(document(), "local")
        with self.assertLogs("agent_memory_server", "ERROR"):
            self.assertFalse(embedding.embed_pending())
        self.assertIn("onnx went away", embedding.error)

    def test_a_changed_model_or_dimension_is_a_mismatch(self):
        self.embedding().prepare()
        for other in (HashEmbedder(dimension=16), HashEmbedder(model="test/other")):
            with self.assertRaisesRegex(EmbeddingMismatch, "agent-memory-server reindex"):
                self.embedding(other).prepare()

    def test_a_collection_of_another_size_is_a_mismatch(self):
        name = describe(self.model)["collection"]
        self.fake.collections[name] = {"vectors": {"size": 3, "distance": "Cosine"}, "metadata": {}, "indexes": {}, "points": {}}
        with self.assertRaisesRegex(EmbeddingMismatch, "size 3.*reindex --rebuild"):
            self.embedding().prepare()

    def test_a_missing_active_collection_is_a_mismatch(self):
        embedding = self.embedding()
        embedding.prepare()
        del self.fake.collections[embedding.collection]
        with self.assertRaisesRegex(EmbeddingMismatch, "missing from Qdrant.*--rebuild"):
            self.embedding().prepare()


class ReindexTest(ServerCase):
    def fill(self, count):
        for index in range(count):
            self.store.write_memory(document(session_id=f"s{index}"), "local")

    def test_builds_the_collection_checks_the_count_and_switches(self):
        self.fill(3)
        self.embedding().prepare()
        bigger = HashEmbedder(dimension=16)
        lines = []
        reindex(self.store, bigger, self.qdrant, out=lines.append)
        name = describe(bigger)["collection"]
        self.assertEqual(len(self.fake.collections[name]["points"]), 3)
        self.assertEqual(self.store.embedding_state()["collection"], name)
        self.assertIn(f"active collection: {name}", lines[-1])
        self.embedding(bigger).prepare()

    def test_switching_back_embeds_only_what_the_old_collection_lacks(self):
        self.fill(3)
        small = self.embedding()
        small.embed_pending()
        bigger = HashEmbedder(dimension=16)
        reindex(self.store, bigger, self.qdrant, out=lambda line: None)
        self.store.write_memory(document(session_id="new"), "local")
        Embedding(self.store, bigger, self.qdrant).embed_pending()
        back = HashEmbedder()
        reindex(self.store, back, self.qdrant, out=lambda line: None)
        self.assertEqual(back.embedded, ["a note"])
        self.assertEqual(self.store.embedding_state()["collection"], small.collection)

    def test_a_count_mismatch_does_not_switch(self):
        self.fill(2)
        name = describe(self.model)["collection"]
        reindex(self.store, self.model, self.qdrant, out=lambda line: None)
        self.fake.collections[name]["points"]["stray"] = {"id": "stray", "vector": [0.0] * 8, "payload": {}}
        bigger = HashEmbedder(dimension=16)
        reindex(self.store, bigger, self.qdrant, out=lambda line: None)
        with self.assertRaisesRegex(ReindexError, "holds 3 points but memories.sqlite has 2"):
            reindex(self.store, self.model, self.qdrant, out=lambda line: None)
        self.assertEqual(self.store.embedding_state()["collection"], describe(bigger)["collection"])
        reindex(self.store, self.model, self.qdrant, rebuild=True, out=lambda line: None)
        self.assertEqual(self.store.embedding_state()["collection"], name)

    def test_prune_lists_without_yes_and_keeps_the_active_collection(self):
        self.fill(1)
        self.fake.collections["memories"] = {"vectors": {"size": 384, "distance": "Cosine"}, "metadata": {}, "indexes": {}, "points": {}}
        self.fake.collections["unrelated"] = {"vectors": {"size": 4, "distance": "Cosine"}, "metadata": {}, "indexes": {}, "points": {}}
        self.fake.collections["memories-old-8-00000000"] = {"vectors": {"size": 8, "distance": "Cosine"}, "metadata": {}, "indexes": {}, "points": {}}
        lines = []
        reindex(self.store, self.model, self.qdrant, prune=True, out=lines.append)
        self.assertIn("would delete: memories-old-8-00000000", lines)
        self.assertTrue(any(line.startswith("keeping memories:") for line in lines))
        reindex(self.store, self.model, self.qdrant, prune=True, yes=True, out=lines.append)
        active = describe(self.model)["collection"]
        self.assertEqual(sorted(self.fake.collections), sorted([active, "memories", "unrelated"]))
        reindex(self.store, self.model, self.qdrant, prune=True, yes=True, include_legacy=True, out=lines.append)
        self.assertEqual(sorted(self.fake.collections), sorted([active, "unrelated"]))


class LaterThanTest(unittest.TestCase):
    def test_a_stamp_in_the_same_second_moves_past_the_old_one(self):
        from agent_memory_server.store import later_than

        same = "2026-11-22T10:00:00+00:00"
        bumped = later_than(same, same)
        self.assertEqual(bumped, "2026-11-22T10:00:00.000001+00:00")
        self.assertGreater(bumped, same)
        self.assertLess(bumped, "2026-11-22T10:00:01+00:00")
        self.assertEqual(later_than("2026-11-22T10:00:01+00:00", same), "2026-11-22T10:00:01+00:00")
        self.assertEqual(later_than(same, bumped), "2026-11-22T10:00:00.000002+00:00")


class LockTest(ServerCase):
    def test_a_second_holder_is_refused_until_release(self):
        first, second = ServerLock(self.data / "server.lock"), ServerLock(self.data / "server.lock")
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()


class CommandTest(ServerCase):
    def environ(self, **extra):
        return {"AGENT_MEMORY_SERVER_DATA": str(self.data), "AGENT_MEMORY_AUTH": "off", **extra}

    def run_cli(self, argv, **extra):
        err, started, out = io.StringIO(), [], []
        code = cli.run(
            argv,
            environ=self.environ(**extra),
            model=extra.pop("model", None) or self.model,
            transport=self.fake,
            start=lambda app, host, port: started.append((host, port)),
            build=lambda settings, store, embedding: object(),
            out=out.append,
            err=err,
        )
        return code, err.getvalue(), started, out

    def test_serve_starts_with_auth_off(self):
        code, err, started, _ = self.run_cli(["serve"])
        self.assertEqual((code, started), (0, [("0.0.0.0", 8765)]))
        self.assertEqual(self.model.loads, 1)
        self.assertIsNotNone(self.store.embedding_state())

    def test_serve_starts_with_auth_on_and_says_how_to_make_the_first_token(self):
        with self.assertLogs("agent_memory_server", "WARNING") as logged:
            code, err, started, _ = self.run_cli(["serve"], AGENT_MEMORY_AUTH="")
        self.assertEqual((code, len(started)), (0, 1))
        self.assertIn("no tokens yet", "\n".join(logged.output))
        self.assertNotIn("auth is off", "\n".join(logged.output))

    def test_serve_refuses_a_changed_embedder(self):
        self.embedding().prepare()
        self.model = HashEmbedder(dimension=16)
        code, err, started, _ = self.run_cli(["serve"])
        self.assertEqual((code, started), (1, []))
        self.assertIn("agent-memory-server reindex", err)

    def test_serve_starts_while_qdrant_is_down(self):
        self.fake.down = True
        with self.assertLogs("agent_memory_server", "WARNING") as logged:
            code, _, started, _ = self.run_cli(["serve"])
        self.assertIn("embedded once it answers", logged.output[0])
        self.assertEqual((code, len(started)), (0, 1))

    def test_reindex_refuses_while_the_server_holds_the_lock(self):
        held = ServerLock(self.data / "server.lock")
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        code, err, _, _ = self.run_cli(["reindex"])
        self.assertEqual(code, 1)
        self.assertIn("docker compose stop server", err)
        code, err, started, _ = self.run_cli(["serve"])
        self.assertEqual((code, started), (1, []))

    def test_reindex_runs_and_yes_needs_prune(self):
        self.store.write_memory(document(), "local")
        code, _, _, out = self.run_cli(["reindex"])
        self.assertEqual(code, 0)
        self.assertIn(f"embedded 1 memory into {describe(self.model)['collection']}", out)
        self.assertEqual(self.run_cli(["reindex", "--yes"])[0], 2)
        self.assertEqual(self.run_cli(["reindex", "--include-legacy"])[0], 2)

    def test_an_unknown_embedder_stops_the_command(self):
        err = io.StringIO()
        code = cli.run(["serve"], environ=self.environ(AGENT_MEMORY_EMBEDDER="openai"), transport=self.fake, err=err)
        self.assertEqual(code, 1)
        self.assertIn("'openai' is not implemented", err.getvalue())


class SettingsTest(unittest.TestCase):
    def test_defaults_and_overrides(self):
        default = Settings.from_env({})
        self.assertEqual((default.db_path, default.lock_path), (Path("/data/memories.sqlite"), Path("/data/server.lock")))
        self.assertEqual(default.allowed_hosts, ("127.0.0.1:8765", "localhost:8765"))
        self.assertEqual((default.allowed_origins, default.auth_off), ((), False))
        custom = Settings.from_env(
            {
                "AGENT_MEMORY_AUTH": "OFF",
                "AGENT_MEMORY_ALLOWED_HOSTS": "Memory.Example:443, 127.0.0.1:8765",
                "AGENT_MEMORY_MODELS_DIR": "/opt/agent-memory/models",
            }
        )
        self.assertTrue(custom.auth_off)
        self.assertEqual(custom.allowed_hosts, ("memory.example:443", "127.0.0.1:8765"))
        self.assertEqual(custom.models_dir, Path("/opt/agent-memory/models"))


if __name__ == "__main__":
    unittest.main()
