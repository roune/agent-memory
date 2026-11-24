#!/usr/bin/env python3
"""MCP list paging and its echoed filter, and search ordering across a supersede.

Battle test v0.1.0, findings 10 and 8. The functions run on the plain python3
suite; the /mcp half needs the server extra (scripts/test-server). No test
needs Docker or a model: FakeQdrant and a scripted embedder stand in.
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
from agent_memory_server.store import ServerStore
from server_doubles import FakeQdrant, HashEmbedder

if HAVE_SERVER:
    from starlette.testclient import TestClient

    from agent_memory_server.app import create_app
    from agent_memory_server.settings import Settings

REPO = "github.com/example/proj"
BASE = "http://127.0.0.1:8765"
PROTOCOL = "2025-06-18"

# The query sits on the x axis; each text's angle from it sets its score.
QUERY = "how does this tool work end to end"
VECTORS = {
    "query: " + QUERY: [1.0, 0.0],
    "v1: the laptop store": [1.0, 0.10],  # closest: the stale note scores highest
    "v2: the server": [1.0, 0.30],
    "v3: the server, with tokens": [1.0, 0.50],
    "no auto retrieval": [1.0, 0.20],
    "unrelated": [0.0, 1.0],
}


class ScriptedEmbedder(HashEmbedder):
    """Fixed two-dimensional vectors for known texts, so the scores are chosen by the test."""

    def __init__(self):
        super().__init__(dimension=2, model="test/scripted")

    def _vector(self, text):
        return VECTORS.get(text) or super()._vector(text)


def memory(**changes):
    base = {
        "kind": "decision",
        "domain": "architecture",
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


def ids(hits):
    return [hit["document_id"] for hit in hits]


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        quiet = logging.NullHandler()
        logging.getLogger("agent_memory_server").addHandler(quiet)
        self.addCleanup(logging.getLogger("agent_memory_server").removeHandler, quiet)
        self.data = Path(self.tmp.name)
        self.store = ServerStore(self.data / "memories.sqlite")
        self.embedding = Embedding(self.store, ScriptedEmbedder(), Qdrant("http://qdrant:6333", transport=FakeQdrant()))

    def keep(self, **changes):
        outcome, stored = self.store.write_memory(memory(**changes), "test")
        self.assertEqual(outcome, "created")
        self.embedding.embed_pending()
        return stored["document_id"]

    def keep_many(self, count):
        """count memories, one minute apart, newest last."""
        for index in range(count):
            self.keep(
                session_id=f"n{index:03d}",
                occurred_at=f"2026-11-{1 + index // 1440:02d}T{index // 60 % 24:02d}:{index % 60:02d}:00+00:00",
            )


class ListFilterEchoTest(StoreCase):
    def test_filter_echoes_since_and_until_as_utc_bounds(self):
        self.keep(session_id="s1")
        listed = mcp_tools.list_memories(self.store, repo=REPO, kind="decision", since="2026-11-20")
        self.assertEqual(
            listed["filter"], {"repo": REPO, "kind": "decision", "since": "2026-11-20T00:00:00+00:00"}
        )
        both = mcp_tools.list_memories(self.store, since="2026-11-20T11:00:00+01:00", until="2026-11-21T00:00:00Z")
        self.assertEqual(both["filter"], {"since": "2026-11-20T10:00:00+00:00", "until": "2026-11-21T00:00:00+00:00"})

    def test_blank_bounds_are_not_echoed(self):
        self.assertEqual(mcp_tools.list_memories(self.store, since="", until=None, repo=" ")["filter"], {})


class ListPagingTest(StoreCase):
    def test_offset_pages_past_the_newest_hundred(self):
        self.keep_many(130)
        seen = []
        offset = 0
        pages = 0
        while offset is not None:
            page = mcp_tools.list_memories(self.store, repo=REPO, limit=100, offset=offset)
            self.assertEqual(page["offset"], offset)
            seen += ids(page["memories"])
            offset = page["next_offset"]
            pages += 1
        self.assertEqual(pages, 2)
        self.assertEqual(len(seen), 130)
        self.assertEqual(len(set(seen)), 130)
        self.assertEqual(seen[0], "miguel-decision-n129")
        self.assertEqual(seen[-1], "miguel-decision-n000")

    def test_next_offset_is_null_on_the_last_page_even_when_it_is_full(self):
        self.keep_many(4)
        first = mcp_tools.list_memories(self.store, limit=2)
        self.assertEqual((first["offset"], first["next_offset"]), (0, 2))
        last = mcp_tools.list_memories(self.store, limit=2, offset=2)
        self.assertEqual(ids(last["memories"]), ["miguel-decision-n001", "miguel-decision-n000"])
        self.assertIsNone(last["next_offset"])
        self.assertEqual(mcp_tools.list_memories(self.store, offset=10)["memories"], [])
        self.assertEqual(mcp_tools.list_memories(self.store, limit=3)["next_offset"], 3)
        self.assertIsNone(mcp_tools.list_memories(self.store, limit=5)["next_offset"])

    def test_paging_keeps_the_filters(self):
        self.keep_many(3)
        self.keep(session_id="other", repo="github.com/example/other")
        page = mcp_tools.list_memories(self.store, repo=REPO, limit=2, offset=2)
        self.assertEqual(ids(page["memories"]), ["miguel-decision-n000"])
        self.assertIsNone(page["next_offset"])

    def test_bad_offsets_are_tool_errors(self):
        self.assertEqual(mcp_tools.list_memories(self.store, offset=None)["offset"], 0)
        self.assertEqual(mcp_tools.list_memories(self.store, offset="3")["offset"], 3)
        for bad in (-1, "many", True, [1]):
            with self.subTest(bad=bad), self.assertRaisesRegex(QueryError, "offset"):
                mcp_tools.list_memories(self.store, offset=bad)


class SupersedesFirstTest(unittest.TestCase):
    """The ordering on its own, with hits as search builds them."""

    @staticmethod
    def hit(document_id, score, superseded_by=None):
        found = {"document_id": document_id, "score": score}
        if superseded_by:
            found["superseded_by"] = superseded_by
        return found

    def test_a_superseded_hit_moves_to_just_after_its_newer_hit(self):
        results = [self.hit("x", 0.9), self.hit("v1", 0.8, "v2"), self.hit("y", 0.7), self.hit("v2", 0.6)]
        self.assertEqual(ids(mcp_tools.supersedes_first(results)), ["x", "y", "v2", "v1"])

    def test_scores_and_hits_are_unchanged(self):
        results = [self.hit("v1", 0.8, "v2"), self.hit("v2", 0.6)]
        ordered = mcp_tools.supersedes_first(results)
        self.assertEqual(sorted(ordered, key=lambda hit: hit["document_id"]), results)

    def test_without_its_newer_hit_a_superseded_hit_keeps_its_place(self):
        results = [self.hit("v1", 0.8, "elsewhere"), self.hit("x", 0.7)]
        self.assertEqual(ids(mcp_tools.supersedes_first(results)), ["v1", "x"])

    def test_a_chain_comes_out_newest_first_at_the_newest_place(self):
        results = [self.hit("v1", 0.9, "v2"), self.hit("x", 0.8), self.hit("v2", 0.7, "v3"), self.hit("v3", 0.6)]
        self.assertEqual(ids(mcp_tools.supersedes_first(results)), ["x", "v3", "v2", "v1"])

    def test_two_old_hits_of_one_newer_keep_their_score_order(self):
        results = [self.hit("a", 0.9, "new"), self.hit("b", 0.8, "new"), self.hit("new", 0.5)]
        self.assertEqual(ids(mcp_tools.supersedes_first(results)), ["new", "a", "b"])

    def test_links_that_loop_keep_score_order(self):
        results = [self.hit("a", 0.9, "b"), self.hit("x", 0.8), self.hit("b", 0.7, "a"), self.hit("s", 0.6, "s")]
        self.assertEqual(ids(mcp_tools.supersedes_first(results)), ["a", "x", "b", "s"])

    def test_a_hit_that_leads_into_a_loop_keeps_its_place(self):
        results = [self.hit("c", 0.9, "a"), self.hit("a", 0.8, "b"), self.hit("b", 0.7, "a")]
        self.assertEqual(ids(mcp_tools.supersedes_first(results)), ["c", "a", "b"])


class SearchSupersedeTest(StoreCase):
    def setUp(self):
        super().setUp()
        self.keep(session_id="v1", text="v1: the laptop store")
        self.keep(session_id="v2", text="v2: the server", supersedes="miguel-decision-v1")
        self.keep(session_id="no-auto", text="no auto retrieval")
        self.keep(session_id="other", text="unrelated")

    def test_the_superseded_v1_comes_after_v2_though_it_scores_higher(self):
        found = mcp_tools.search(self.store, self.embedding, QUERY, repo=REPO)
        order = ids(found["results"])
        scores = {hit["document_id"]: hit["score"] for hit in found["results"]}
        self.assertGreater(scores["miguel-decision-v1"], scores["miguel-decision-v2"])
        self.assertLess(order.index("miguel-decision-v2"), order.index("miguel-decision-v1"))
        self.assertEqual(order.index("miguel-decision-v1"), order.index("miguel-decision-v2") + 1)
        # Nothing is hidden, and both links are there.
        self.assertEqual(len(order), 4)
        by_id = {hit["document_id"]: hit for hit in found["results"]}
        self.assertEqual(by_id["miguel-decision-v1"]["superseded_by"], "miguel-decision-v2")
        self.assertEqual(by_id["miguel-decision-v2"]["supersedes"], "miguel-decision-v1")
        # A hit that supersedes nothing keeps its score place ahead of v2.
        self.assertEqual(order[0], "miguel-decision-no-auto")

    def test_a_chain_of_three_reads_newest_first(self):
        self.keep(session_id="v3", text="v3: the server, with tokens", supersedes="miguel-decision-v2")
        order = ids(mcp_tools.search(self.store, self.embedding, QUERY)["results"])
        start = order.index("miguel-decision-v3")
        self.assertEqual(order[start : start + 3], ["miguel-decision-v3", "miguel-decision-v2", "miguel-decision-v1"])

    def test_when_the_newer_one_is_cut_by_the_limit_the_old_one_still_shows(self):
        order = ids(mcp_tools.search(self.store, self.embedding, QUERY, limit=1)["results"])
        self.assertEqual(order, ["miguel-decision-v1"])


@unittest.skipUnless(HAVE_SERVER, "needs the server extra (scripts/test-server)")
class McpHttpTest(StoreCase):
    def setUp(self):
        super().setUp()
        settings = Settings(data_dir=self.data, models_dir=self.data / "models", auth="off")
        self.client = TestClient(create_app(settings, self.store, self.embedding, retry_seconds=0), base_url=BASE)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.ids = iter(range(1, 1000))
        self.session = self.open_session()

    def rpc(self, method, params=None, session=None, notify=False):
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if session:
            headers["Mcp-Session-Id"] = session
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            message["id"] = next(self.ids)
        return self.client.post("/mcp", json=message, headers=headers)

    def open_session(self):
        started = self.rpc(
            "initialize",
            {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
        )
        self.assertEqual(started.status_code, 200, started.text)
        session = started.headers["mcp-session-id"]
        self.rpc("notifications/initialized", session=session, notify=True)
        return session

    def call(self, name, **arguments):
        response = self.rpc("tools/call", {"name": name, "arguments": arguments}, session=self.session)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def test_list_tool_takes_offset_and_echoes_since(self):
        self.keep_many(3)
        tools = {tool["name"]: tool for tool in self.rpc("tools/list", session=self.session).json()["result"]["tools"]}
        self.assertIn("offset", tools["list"]["inputSchema"]["properties"])
        self.assertIn("next_offset", tools["list"]["description"])
        self.assertIn("superseded one comes right after", tools["search"]["description"])
        first = self.call("list", repo=REPO, since="2026-11-01", limit=2)["structuredContent"]
        self.assertEqual(first["filter"], {"repo": REPO, "since": "2026-11-01T00:00:00+00:00"})
        self.assertEqual(first["next_offset"], 2)
        second = self.call("list", repo=REPO, since="2026-11-01", limit=2, offset=first["next_offset"])
        self.assertEqual(ids(second["structuredContent"]["memories"]), ["miguel-decision-n000"])
        self.assertIsNone(second["structuredContent"]["next_offset"])
        refused = self.call("list", offset=-1)
        self.assertTrue(refused["isError"])
        self.assertIn("offset", refused["content"][0]["text"])

    def test_search_tool_puts_v2_before_v1(self):
        self.keep(session_id="v1", text="v1: the laptop store")
        self.keep(session_id="v2", text="v2: the server", supersedes="miguel-decision-v1")
        order = ids(self.call("search", query=QUERY)["structuredContent"]["results"])
        self.assertEqual(order, ["miguel-decision-v2", "miguel-decision-v1"])


if __name__ == "__main__":
    unittest.main()
