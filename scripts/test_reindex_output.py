#!/usr/bin/env python3
"""What `agent-memory-server reindex` prints: one line for each step it took.

The battle test before v0.1.0 ran `reindex --rebuild` on an empty store and saw
nothing. These tests pin the lines: what was dropped, created or reused, how many
memories were embedded, the point count, the active collection, and what --prune
deletes or would delete. HashEmbedder and FakeQdrant stand in for the model and Qdrant.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_memory_server import cli
from agent_memory_server.embedding import describe
from agent_memory_server.reindex import ReindexError, reindex
from server_doubles import HashEmbedder
from test_server_core import ServerCase, document


def empty_collection(size=8):
    return {"vectors": {"size": size, "distance": "Cosine"}, "metadata": {}, "indexes": {}, "points": {}}


class ReindexOutputTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.name = describe(self.model)["collection"]
        self.created = f"created {self.name} (hash test/hash, 8 dimensions, Cosine)"

    def fill(self, count):
        for index in range(count):
            self.store.write_memory(document(session_id=f"s{index}"), "local")

    def lines(self, **options):
        printed = []
        self.assertEqual(reindex(self.store, self.model, self.qdrant, out=printed.append, **options), 0)
        return printed

    def test_rebuild_on_an_empty_store_says_every_step(self):
        self.assertEqual(
            self.lines(rebuild=True),
            [
                f"nothing to drop: {self.name} did not exist",
                self.created,
                f"embedded 0 memories into {self.name}",
                f"{self.name} holds 0 points, one for each of the 0 memories in memories.sqlite",
                f"active collection: {self.name} (was none)",
            ],
        )

    def test_rebuild_of_an_existing_collection_says_it_dropped_it(self):
        self.fill(2)
        self.lines()
        self.assertEqual(
            self.lines(rebuild=True),
            [
                f"dropped {self.name}",
                self.created,
                f"embedded 2 memories into {self.name}",
                f"{self.name} holds 2 points, one for each of the 2 memories in memories.sqlite",
                f"active collection: {self.name} (unchanged)",
            ],
        )

    def test_a_plain_reindex_reuses_the_collection_and_embeds_only_what_it_lacks(self):
        self.fill(1)
        self.embedding().embed_pending()
        self.store.write_memory(document(session_id="later"), "local")
        self.assertEqual(
            self.lines(),
            [
                f"using {self.name}, which already existed",
                f"embedded 1 memory into {self.name}",
                f"{self.name} holds 2 points, one for each of the 2 memories in memories.sqlite",
                f"active collection: {self.name} (unchanged)",
            ],
        )

    def test_switching_models_names_the_collection_it_replaces(self):
        self.fill(1)
        self.embedding().embed_pending()
        bigger = HashEmbedder(dimension=16)
        printed = []
        reindex(self.store, bigger, self.qdrant, out=printed.append)
        name = describe(bigger)["collection"]
        self.assertEqual(printed[0], f"created {name} (hash test/hash, 16 dimensions, Cosine)")
        self.assertEqual(printed[-1], f"active collection: {name} (was {self.name})")

    def test_prune_without_yes_names_what_it_would_delete_and_deletes_nothing(self):
        self.fake.collections["memories-old-8-00000000"] = empty_collection()
        self.fake.collections["memories-older-8-11111111"] = empty_collection()
        self.fake.collections["memories"] = empty_collection(384)
        printed = self.lines(prune=True)
        self.assertEqual(
            printed[-3:],
            [
                "keeping memories: the laptop's scripts/embed still writes it until the cutover "
                "(pass --include-legacy to delete it too)",
                "would delete: memories-old-8-00000000, memories-older-8-11111111",
                "run again with --prune --yes to delete them",
            ],
        )
        self.assertIn("memories-old-8-00000000", self.fake.collections)

    def test_prune_with_yes_names_each_collection_it_deleted(self):
        self.fake.collections["memories-old-8-00000000"] = empty_collection()
        self.fake.collections["memories"] = empty_collection(384)
        printed = self.lines(prune=True, yes=True, include_legacy=True)
        self.assertEqual(printed[-2:], ["deleted memories", "deleted memories-old-8-00000000"])
        self.assertEqual(sorted(self.fake.collections), [self.name])

    def test_prune_with_nothing_to_delete_says_so(self):
        self.fake.collections["unrelated"] = empty_collection()
        self.assertEqual(self.lines(prune=True)[-1], "nothing to prune: no other memories* collection")

    def test_a_count_mismatch_prints_the_steps_before_it_and_does_not_switch(self):
        self.fill(1)
        self.lines()
        self.fake.collections[self.name]["points"]["stray"] = {"id": "stray", "vector": [0.0] * 8, "payload": {}}
        printed = []
        with self.assertRaisesRegex(ReindexError, "holds 2 points but memories.sqlite has 1"):
            reindex(self.store, self.model, self.qdrant, out=printed.append)
        self.assertEqual(printed, [f"using {self.name}, which already existed", f"embedded 0 memories into {self.name}"])


class ReindexCommandOutputTest(ServerCase):
    """cli.run with its default out=print, so the lines reach stdout as they do in the container."""

    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.run(
                argv,
                environ={"AGENT_MEMORY_SERVER_DATA": str(self.data), "AGENT_MEMORY_AUTH": "off"},
                model=self.model,
                transport=self.fake,
                err=stderr,
            )
        return code, stdout.getvalue().splitlines(), stderr.getvalue()

    def test_rebuild_on_an_empty_store_prints_to_stdout(self):
        name = describe(self.model)["collection"]
        code, stdout, stderr = self.run_cli(["reindex", "--rebuild"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout[0], f"nothing to drop: {name} did not exist")
        self.assertIn(f"embedded 0 memories into {name}", stdout)
        self.assertEqual(stdout[-1], f"active collection: {name} (was none)")

    def test_prune_prints_what_it_would_delete(self):
        self.fake.collections["memories-old-8-00000000"] = empty_collection()
        code, stdout, _ = self.run_cli(["reindex", "--prune"])
        self.assertEqual(code, 0)
        self.assertIn("would delete: memories-old-8-00000000", stdout)


if __name__ == "__main__":
    unittest.main()
