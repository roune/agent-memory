#!/usr/bin/env python3
"""The read-only MCP tools: filters, search, get and list (standard library), then /mcp over HTTP.

The HTTP half needs the server extra (scripts/test-server). No test needs
Docker or a model: FakeQdrant and HashEmbedder stand in.
"""

import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HAVE_SERVER = all(importlib.util.find_spec(name) for name in ("starlette", "httpx", "mcp"))

from agent_memory_server import mcp_tools
from agent_memory_server.embedding import Embedding
from agent_memory_server.mcp_tools import QueryError
from agent_memory_server.qdrant import Qdrant
from agent_memory_server.settings import Settings
from agent_memory_server.store import ServerStore
from server_doubles import FakeQdrant, HashEmbedder

if HAVE_SERVER:
    from starlette.testclient import TestClient

    from agent_memory_server.app import create_app

REPO = "github.com/example/proj"
OTHER = "github.com/example/other"
A = "a" * 40
BASE = "http://127.0.0.1:8765"
PROTOCOL = "2025-06-18"
WRITE_WORDS = ("write", "save", "approve", "add", "create", "update", "delete", "remove", "reject", "import", "put")


def memory(**changes):
    base = {
        "kind": "learning",
        "domain": "code",
        "text": "a note",
        "repo": REPO,
        "tool": "git",
        "session_id": "s1",
        "org": "Acme",
        "team": "Platform",
        "person": "miguel",
        "occurred_at": "2026-11-20T10:00:00+00:00",
    }
    base.update(changes)
    base.setdefault("id", f"{base['person']}-{base['kind']}-{base['session_id']}")
    return base


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        quiet = logging.NullHandler()
        logging.getLogger("agent_memory_server").addHandler(quiet)
        self.addCleanup(logging.getLogger("agent_memory_server").removeHandler, quiet)
        self.data = Path(self.tmp.name)
        self.store = ServerStore(self.data / "memories.sqlite")
        self.fake = FakeQdrant()
        self.embedding = Embedding(self.store, self.make_embedder(), Qdrant("http://qdrant:6333", transport=self.fake))

    def make_embedder(self):
        return HashEmbedder()

    def keep(self, **changes):
        outcome, stored = self.store.write_memory(memory(**changes), "test")
        self.assertIn(outcome, ("created", "same"))
        self.embedding.embed_pending()
        return stored["document_id"]


class FilterTest(unittest.TestCase):
    def test_blank_filters_are_dropped_and_order_is_fixed(self):
        self.assertEqual(mcp_tools.filters_from(repo=" ", kind=None), {})
        self.assertEqual(
            mcp_tools.filters_from(team="Infra", repo=f" {REPO} ", kind="change"),
            {"repo": REPO, "kind": "change", "team": "Infra"},
        )
        with self.assertRaises(QueryError):
            mcp_tools.filters_from(tenant="x")

    def test_qdrant_filter_and_body(self):
        self.assertIsNone(mcp_tools.qdrant_filter({}))
        self.assertEqual(
            mcp_tools.search_body((0.1, 0.2), {}, 5),
            {"query": [0.1, 0.2], "limit": 5, "with_payload": ["document_id"], "with_vector": False},
        )
        self.assertEqual(
            mcp_tools.search_body([0.1], {"repo": REPO, "org": "Acme"}, 5)["filter"],
            {"must": [{"key": "repo", "match": {"value": REPO}}, {"key": "org", "match": {"value": "Acme"}}]},
        )

    def test_limits_are_clamped(self):
        self.assertEqual(mcp_tools.clamp_limit(None, mcp_tools.SEARCH_LIMIT), 10)
        self.assertEqual(mcp_tools.clamp_limit(0, mcp_tools.SEARCH_LIMIT), 1)
        self.assertEqual(mcp_tools.clamp_limit(500, mcp_tools.SEARCH_LIMIT), 50)
        self.assertEqual(mcp_tools.clamp_limit("3", mcp_tools.LIST_LIMIT), 3)
        self.assertEqual(mcp_tools.clamp_limit(500, mcp_tools.LIST_LIMIT), 100)
        for bad in ("many", True, [1]):
            with self.assertRaises(QueryError):
                mcp_tools.clamp_limit(bad, mcp_tools.SEARCH_LIMIT)


class SearchTest(StoreCase):
    def test_hits_come_back_in_gets_shape_with_a_score(self):
        self.keep(session_id="s1", kind="change", commit=A, pull_request=7, text="flock guards the server lock")
        self.keep(session_id="s2", text="something else")
        found = mcp_tools.search(self.store, self.embedding, "  server lock  ", limit=5)
        self.assertEqual(found["query"], "server lock")
        self.assertEqual(found["not_yet_searchable"], 0)
        self.assertEqual(len(found["results"]), 2)
        hit = next(item for item in found["results"] if item["document_id"] == "miguel-change-s1")
        self.assertEqual({**hit, "score": None}, {**self.store.get_memory("miguel-change-s1"), "score": None})
        self.assertIsInstance(hit["score"], float)
        self.assertEqual(hit["commits"], [A])
        self.assertEqual(hit["pull_request"], 7)
        scores = [item["score"] for item in found["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_filters_reach_qdrant(self):
        self.keep(session_id="s1")
        self.keep(session_id="s2", repo=OTHER, org="Other")
        found = mcp_tools.search(self.store, self.embedding, "note", repo=OTHER, org="", kind=None)
        self.assertEqual(found["filter"], {"repo": OTHER})
        self.assertEqual([hit["document_id"] for hit in found["results"]], ["miguel-learning-s2"])
        method, path, sent = self.fake.requests[-1]
        self.assertEqual((method, path), ("POST", f"/collections/{self.embedding.collection}/points/query"))
        self.assertEqual(sent["filter"], {"must": [{"key": "repo", "match": {"value": OTHER}}]})
        self.assertEqual(len(sent["query"]), self.embedding.description["dimension"])

    def test_a_superseded_memory_is_returned_with_superseded_by(self):
        self.keep(session_id="s1", text="use a pid file")
        self.keep(session_id="s2", person="ana", org="Other", team="Sales", text="use flock", supersedes="miguel-learning-s1")
        found = mcp_tools.search(self.store, self.embedding, "use", limit=10)
        by_id = {hit["document_id"]: hit for hit in found["results"]}
        self.assertEqual(set(by_id), {"miguel-learning-s1", "ana-learning-s2"})
        self.assertEqual(by_id["miguel-learning-s1"]["superseded_by"], "ana-learning-s2")
        self.assertEqual(by_id["ana-learning-s2"]["supersedes"], "miguel-learning-s1")

    def test_text_and_superseded_by_come_from_sqlite_not_the_point(self):
        self.keep(session_id="s1", text="use a pid file")
        self.fake.down = True
        self.store.write_memory(memory(session_id="s2", supersedes="miguel-learning-s1", text="use flock"), "test")
        self.fake.down = False
        # The new row isn't embedded yet, and the old point still lacks superseded_by.
        found = mcp_tools.search(self.store, self.embedding, "pid")
        self.assertEqual(found["not_yet_searchable"], 2)
        self.assertEqual([hit["document_id"] for hit in found["results"]], ["miguel-learning-s1"])
        self.assertEqual(found["results"][0]["superseded_by"], "miguel-learning-s2")

    def test_qdrant_down_missing_collection_and_mismatch_are_query_errors(self):
        self.keep()
        self.fake.down = True
        with self.assertRaisesRegex(QueryError, "get and list still work"):
            mcp_tools.search(self.store, self.embedding, "note")
        self.fake.down = False
        del self.fake.collections[self.embedding.collection]
        with self.assertRaisesRegex(QueryError, "not in Qdrant yet"):
            mcp_tools.search(self.store, self.embedding, "note")
        self.embedding.state, self.embedding.error = "mismatch", "run reindex"
        with self.assertRaisesRegex(QueryError, "run reindex"):
            mcp_tools.search(self.store, self.embedding, "note")

    def test_blank_and_long_queries(self):
        for query in ("", "   ", None, "x" * (mcp_tools.MAX_QUERY + 1)):
            with self.assertRaises(QueryError):
                mcp_tools.search(self.store, self.embedding, query)

    def test_a_point_without_a_row_is_skipped(self):
        self.keep()
        self.fake.collections[self.embedding.collection]["points"]["ghost"] = {
            "id": "ghost",
            "vector": [1.0] * self.embedding.description["dimension"],
            "payload": {"document_id": "nobody-learning-x"},
        }
        found = mcp_tools.search(self.store, self.embedding, "note")
        self.assertEqual([hit["document_id"] for hit in found["results"]], ["miguel-learning-s1"])


class GetAndListTest(StoreCase):
    def test_get(self):
        self.keep(kind="change", commit=A)
        self.assertEqual(mcp_tools.get(self.store, " miguel-change-s1 ")["commits"], [A])
        for missing in ("nope", "", None):
            with self.assertRaises(QueryError):
                mcp_tools.get(self.store, missing)

    def test_list_filters_since_and_until(self):
        self.keep(session_id="old", occurred_at="2026-07-01T00:00:00+00:00")
        self.keep(session_id="new")
        self.keep(session_id="other", repo=OTHER, kind="decision", team="Infra")
        listed = mcp_tools.list_memories(self.store, repo=REPO, since="2026-08-01T00:00:00+00:00")
        self.assertEqual([hit["document_id"] for hit in listed["memories"]], ["miguel-learning-new"])
        self.assertEqual(listed["filter"], {"repo": REPO, "since": "2026-08-01T00:00:00+00:00"})
        until = mcp_tools.list_memories(self.store, until="2026-08-01T00:00:00+00:00")
        self.assertEqual([hit["document_id"] for hit in until["memories"]], ["miguel-learning-old"])
        self.assertEqual(len(mcp_tools.list_memories(self.store, repo=" ", since="")["memories"]), 3)
        team = mcp_tools.list_memories(self.store, team="Infra", limit=1)
        self.assertEqual([hit["document_id"] for hit in team["memories"]], ["miguel-decision-other"])

    def test_since_and_until_are_times_compared_in_utc(self):
        self.keep(session_id="early", occurred_at="2026-11-20T09:30:00+00:00")
        self.keep(session_id="late", occurred_at="2026-11-20T10:30:00+00:00")
        # 11:00+01:00 is 10:00 UTC: only the 10:30 row is later. As text it sorted after both.
        since = mcp_tools.list_memories(self.store, since="2026-11-20T11:00:00+01:00")
        self.assertEqual([hit["document_id"] for hit in since["memories"]], ["miguel-learning-late"])
        until = mcp_tools.list_memories(self.store, until="2026-11-20T10:00:00Z")
        self.assertEqual([hit["document_id"] for hit in until["memories"]], ["miguel-learning-early"])
        day = mcp_tools.list_memories(self.store, since="2026-11-20", until="2026-11-21")
        self.assertEqual(len(day["memories"]), 2)
        self.assertEqual(mcp_tools.list_memories(self.store, since="2026-11-21")["memories"], [])
        for bad in ("yesterday", "2026-11-20T10:00:00", "2026-13-01", 7):
            with self.subTest(bad=bad), self.assertRaisesRegex(QueryError, "since"):
                mcp_tools.list_memories(self.store, since=bad)

    def test_memories_by_ids(self):
        self.keep(session_id="s1")
        self.assertEqual(set(self.store.memories_by_ids(["miguel-learning-s1", "nope", "", "miguel-learning-s1"])), {"miguel-learning-s1"})
        self.assertEqual(self.store.memories_by_ids([]), {})
        empty = ServerStore(self.data / "missing.sqlite")
        self.assertEqual(empty.memories_by_ids(["x"]), {})


@unittest.skipUnless(HAVE_SERVER, "needs the server extra (scripts/test-server)")
class McpCase(StoreCase):
    auth_mode = "off"

    def setUp(self):
        super().setUp()
        self.settings = Settings(data_dir=self.data, models_dir=self.data / "models", auth=self.auth_mode)
        app = create_app(self.settings, self.store, self.embedding, retry_seconds=0)
        self.client = TestClient(app, base_url=BASE)
        # The context manager runs the lifespan, which starts the SDK's session manager.
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.ids = iter(range(1, 1000))

    def rpc(self, method, params=None, session=None, token=None, notify=False):
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if session:
            headers["Mcp-Session-Id"] = session
        if token:
            headers["Authorization"] = f"Bearer {token}"
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            message["id"] = next(self.ids)
        return self.client.post("/mcp", json=message, headers=headers)

    def open_session(self, token=None):
        started = self.rpc(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            token=token,
        )
        self.assertEqual(started.status_code, 200, started.text)
        session = started.headers["mcp-session-id"]
        self.assertEqual(self.rpc("notifications/initialized", session=session, token=token, notify=True).status_code, 202)
        return session, started.json()["result"]

    def call(self, session, name, token=None, **arguments):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}, session=session, token=token)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]


class McpHttpTest(McpCase):
    def test_initialize_lists_only_the_three_read_tools(self):
        session, started = self.open_session()
        self.assertIn("superseded_by", started["instructions"])
        tools = self.rpc("tools/list", session=session).json()["result"]["tools"]
        names = sorted(tool["name"] for tool in tools)
        self.assertEqual(names, ["get", "list", "search"])
        for tool in tools:
            self.assertFalse(any(word in tool["name"].lower() for word in WRITE_WORDS), tool["name"])
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_search_get_and_list_over_http(self):
        self.keep(session_id="s1", text="flock guards the server lock")
        self.keep(session_id="s2", supersedes="miguel-learning-s1", text="flock, and reindex takes it too")
        session, _ = self.open_session()
        searched = self.call(session, "search", query="server lock", repo=REPO)
        self.assertFalse(searched.get("isError"))
        hits = searched["structuredContent"]["results"]
        self.assertEqual({hit["document_id"] for hit in hits}, {"miguel-learning-s1", "miguel-learning-s2"})
        got = self.call(session, "get", document_id="miguel-learning-s1")["structuredContent"]
        self.assertEqual(got["superseded_by"], "miguel-learning-s2")
        listed = self.call(session, "list", repo=REPO, limit=1)["structuredContent"]
        self.assertEqual(len(listed["memories"]), 1)

    def test_tool_failures_are_tool_errors_and_the_server_keeps_answering(self):
        self.keep()
        session, _ = self.open_session()
        self.fake.down = True
        failed = self.call(session, "search", query="note")
        self.assertTrue(failed["isError"])
        self.assertIn("get and list still work", failed["content"][0]["text"])
        self.assertEqual(self.call(session, "get", document_id="miguel-learning-s1")["structuredContent"]["text"], "a note")
        bad_time = self.call(session, "list", since="yesterday")
        self.assertTrue(bad_time["isError"])
        self.assertIn("ISO-8601", bad_time["content"][0]["text"])
        missing = self.call(session, "get", document_id="nope")
        self.assertTrue(missing["isError"])
        self.assertIn("no approved memory nope", missing["content"][0]["text"])
        unknown = self.rpc("tools/call", {"name": "save", "arguments": {}}, session=session).json()
        self.assertTrue(unknown.get("error") or unknown["result"]["isError"])

    def test_a_delete_without_content_type_ends_the_session(self):
        session, _ = self.open_session()
        ended = self.client.delete("/mcp", headers={"Mcp-Session-Id": session, "MCP-Protocol-Version": PROTOCOL})
        self.assertEqual(ended.status_code, 200, ended.text)
        self.assertEqual(self.rpc("tools/list", session=session).status_code, 404)

    def test_mcp_is_one_path(self):
        session, _ = self.open_session()
        headers = {"Accept": "application/json, text/event-stream", "Mcp-Session-Id": session}
        self.assertEqual(self.client.post("/mcp/mcp", json={}, headers=headers).status_code, 404)
        self.assertEqual(self.client.post("/v1/mcp", json={}, headers=headers).status_code, 404)

    def test_nothing_is_written(self):
        self.keep()
        before = self.store.path.read_bytes()
        seen = len(self.fake.requests)
        session, _ = self.open_session()
        self.call(session, "search", query="note")
        self.call(session, "list")
        self.call(session, "get", document_id="miguel-learning-s1")
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual([method for method, _, _ in self.fake.requests[seen:]], ["POST"])


class McpAuthTest(McpCase):
    auth_mode = "on"

    def setUp(self):
        super().setUp()
        self.reader_id, self.reader = self.store.create_token("ana", "Acme", "Platform", ("read",))
        _, self.writer = self.store.create_token("bot", "Acme", "Platform", ("write",))

    def test_no_token_is_401_with_www_authenticate(self):
        for token in (None, "am_wrong"):
            with self.subTest(token=token):
                response = self.rpc(
                    "initialize",
                    {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
                    token=token,
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_read_scope_is_required_on_every_request(self):
        refused = self.rpc("tools/list", token=self.writer)
        self.assertEqual(refused.status_code, 403)
        self.assertIn("read", refused.json()["error"])
        session, _ = self.open_session(token=self.reader)
        self.assertEqual(self.call(session, "list", token=self.reader)["structuredContent"], {"filter": {}, "limit": 20, "offset": 0, "memories": [], "next_offset": None})
        # A session doesn't outlive its token: each request is checked again.
        self.assertEqual(self.rpc("tools/list", session=session).status_code, 401)
        self.assertEqual(self.rpc("tools/list", session=session, token=self.writer).status_code, 403)
        self.store.revoke_token(self.reader_id)
        self.assertEqual(self.rpc("tools/list", session=session, token=self.reader).status_code, 401)

    def test_a_session_answers_only_the_token_that_opened_it(self):
        _, other = self.store.create_token("zed", "Acme", "Platform", ("read",))
        session, _ = self.open_session(token=self.reader)
        self.assertEqual(self.rpc("tools/list", session=session, token=other).status_code, 404)
        ended = self.client.delete("/mcp", headers={"Mcp-Session-Id": session, "Authorization": f"Bearer {other}"})
        self.assertEqual(ended.status_code, 404)
        self.assertEqual(self.rpc("tools/list", session=session, token=self.reader).status_code, 200)
        mine = self.client.delete("/mcp", headers={"Mcp-Session-Id": session, "Authorization": f"Bearer {self.reader}"})
        self.assertEqual(mine.status_code, 200)

    def test_browser_rules_come_before_auth(self):
        headers = {"Authorization": f"Bearer {self.reader}", "Accept": "application/json, text/event-stream"}
        self.assertEqual(self.client.post("/mcp", json={}, headers={**headers, "Host": "evil.example"}).status_code, 421)
        self.assertEqual(self.client.post("/mcp", json={}, headers={**headers, "Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/mcp", content=b"{}", headers={**headers, "Content-Type": "text/plain"}).status_code, 415)
        self.assertEqual(self.client.post("/mcp", json={}, headers={"Host": "evil.example"}).status_code, 421)


if __name__ == "__main__":
    unittest.main()
