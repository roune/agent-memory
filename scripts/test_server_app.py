#!/usr/bin/env python3
"""The server's HTTP routes and browser rules. Needs the server extra: scripts/test-server runs it.

Under the plain python3 these tests are skipped.
"""

import importlib.util
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

HAVE_STARLETTE = all(importlib.util.find_spec(name) for name in ("starlette", "httpx"))
HAVE_MCP = importlib.util.find_spec("mcp") is not None

from agent_memory_server.embedding import Embedding
from agent_memory_server.qdrant import Qdrant
from agent_memory_server.settings import Settings
from agent_memory_server.store import ServerStore
from server_doubles import FakeQdrant, HashEmbedder

if HAVE_STARLETTE:
    from starlette.testclient import TestClient

    from agent_memory_server.app import create_app, transport_security

A = "a" * 40
B = "b" * 40
REPO = "github.com/example/proj"
BASE = "http://127.0.0.1:8765"


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


@unittest.skipUnless(HAVE_STARLETTE, "needs the server extra (scripts/test-server)")
class AppCase(unittest.TestCase):
    origins = ()
    auth_mode = "off"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        quiet = logging.NullHandler()
        logging.getLogger("agent_memory_server").addHandler(quiet)
        self.addCleanup(logging.getLogger("agent_memory_server").removeHandler, quiet)
        data = Path(self.tmp.name)
        self.settings = Settings(data_dir=data, models_dir=data / "models", auth=self.auth_mode, allowed_origins=self.origins)
        self.store = ServerStore(self.settings.db_path)
        self.fake = FakeQdrant()
        self.embedding = Embedding(self.store, HashEmbedder(), Qdrant("http://qdrant:6333", transport=self.fake))
        self.client = self.client_for(retry_seconds=0)

    def client_for(self, retry_seconds):
        app = create_app(self.settings, self.store, self.embedding, retry_seconds=retry_seconds)
        return TestClient(app, base_url=BASE)

    def post(self, path, payload, **headers):
        return self.client.post(path, json=payload, headers=headers)


class BrowserRulesTest(AppCase):
    def test_each_rule_on_v1_and_on_mcp(self):
        for path in ("/v1/memories", "/v1/admin/import", "/mcp"):
            with self.subTest(path=path):
                plain = self.client.post(path, content=b"{}", headers={"Content-Type": "text/plain"})
                self.assertEqual(plain.status_code, 415)
                form = self.client.post(path, data={"text": "x"})
                self.assertEqual(form.status_code, 415)
                missing = self.client.post(path, content=b"{}")
                self.assertEqual(missing.status_code, 415)
                origin = self.post(path, body(), Origin="https://evil.example")
                self.assertEqual(origin.status_code, 403)
                null_origin = self.post(path, body(), Origin="null")
                self.assertEqual(null_origin.status_code, 403)
                host = self.post(path, body(), Host="evil.example:8765")
                self.assertEqual(host.status_code, 421)
        self.assertEqual(self.store.count_memories(), 0)

    def test_reads_get_the_host_and_origin_rules_too(self):
        self.assertEqual(self.client.get("/v1/health", headers={"Host": "attacker.test"}).status_code, 421)
        self.assertEqual(self.client.get("/v1/memories", headers={"Origin": "http://127.0.0.1:4173"}).status_code, 403)
        self.assertEqual(self.client.get("/v1/health", headers={"Host": "localhost:8765"}).status_code, 200)

    def test_json_with_a_charset_is_json(self):
        response = self.client.post(
            "/v1/memories",
            content=b'{"kind": "learning"}',
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(response.status_code, 400)


class AllowedOriginTest(AppCase):
    origins = ("https://memory.example",)

    def test_a_listed_origin_is_answered(self):
        self.assertEqual(self.client.get("/v1/health", headers={"Origin": "https://memory.example"}).status_code, 200)
        self.assertEqual(self.client.get("/v1/health", headers={"Origin": "https://other.example"}).status_code, 403)


@unittest.skipUnless(HAVE_STARLETTE and HAVE_MCP, "needs the server extra (scripts/test-server)")
class TransportSecurityTest(unittest.TestCase):
    def test_the_sdk_gets_the_same_hosts_and_origins(self):
        settings = Settings.from_env({"AGENT_MEMORY_ALLOWED_ORIGINS": "https://memory.example"})
        security = transport_security(settings)
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertEqual(security.allowed_hosts, ["127.0.0.1:8765", "localhost:8765"])
        self.assertEqual(security.allowed_origins, ["https://memory.example"])


class WriteRouteTest(AppCase):
    def test_new_identical_and_different(self):
        created = self.post("/v1/memories", body())
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["id"], "tester-learning-s1")
        self.assertTrue(created.json()["embedded"])
        again = self.post("/v1/memories", {**body(), "id": "tester-learning-s1"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["updated_at"], created.json()["updated_at"])
        clash = self.post("/v1/memories", body(text="different"))
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(clash.json()["stored"], {"text": "a note"})
        self.assertEqual(self.client.get("/v1/memories/tester-learning-s1").json()["text"], "a note")

    def test_bad_bodies_are_400_and_store_nothing(self):
        for payload in (body(transcript_path="/tmp/t.jsonl"), body(kind="note"), body(raw="x"), ["x"]):
            with self.subTest(payload=payload):
                response = self.post("/v1/memories", payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())
        broken = self.client.post("/v1/memories", content=b"{not json", headers={"Content-Type": "application/json"})
        self.assertEqual(broken.status_code, 400)
        self.assertEqual(self.store.count_memories(), 0)

    def test_superseding_updates_and_embeds_the_old_row(self):
        self.post("/v1/memories", body(session_id="s1"))
        response = self.post("/v1/memories", body(session_id="s2", supersedes="tester-learning-s1"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.client.get("/v1/memories/tester-learning-s1").json()["superseded_by"], "tester-learning-s2")
        payloads = self.fake.payloads(self.embedding.collection)
        self.assertEqual(payloads["tester-learning-s1"]["superseded_by"], "tester-learning-s2")

    def test_qdrant_down_stores_the_row_and_the_retry_loop_embeds_it(self):
        self.fake.down = True
        response = self.post("/v1/memories", body())
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["embedded"])
        self.assertEqual(self.client.get("/v1/memories/tester-learning-s1").status_code, 200)
        self.fake.down = False
        with self.client_for(retry_seconds=0.05):
            deadline = time.monotonic() + 5
            while not self.store.is_embedded("tester-learning-s1", self.embedding.collection):
                self.assertLess(time.monotonic(), deadline, "the retry loop did not embed the row")
                time.sleep(0.02)


class ReadRouteTest(AppCase):
    def test_list_get_and_missing(self):
        self.post("/v1/memories", body(session_id="s1", team="Platform"))
        self.post("/v1/memories", body(session_id="s2", team="Infra"))
        listed = self.client.get("/v1/memories", params={"team": "Infra", "limit": "5"})
        self.assertEqual([hit["document_id"] for hit in listed.json()["memories"]], ["tester-learning-s2"])
        self.assertEqual(self.client.get("/v1/memories", params={"q": "x"}).status_code, 400)
        self.assertEqual(self.client.get("/v1/memories/nobody-learning-x").status_code, 404)

    def test_commit_status_matches_abbreviated_hashes_both_ways(self):
        self.post("/v1/memories", body(kind="change", commit=A))
        self.post("/v1/rejections", {"repo": REPO, "shas": [B[:8]]})
        status = self.post("/v1/commits/status", {"repo": REPO, "shas": [A[:7], B]}).json()["commits"]
        self.assertEqual(status[A[:7]], {"kept": True, "rejected": False})
        self.assertEqual(status[B], {"kept": False, "rejected": True})
        other = self.post("/v1/commits/status", {"repo": "github.com/example/fork", "shas": [B]}).json()["commits"]
        self.assertFalse(other[B]["rejected"])
        self.assertEqual(self.post("/v1/rejections", {"repo": REPO, "shas": ["nope"]}).status_code, 400)

    def test_health_and_whoami(self):
        self.post("/v1/memories", body())
        health = self.client.get("/v1/health").json()
        self.assertEqual((health["status"], health["auth"], health["qdrant"]), ("ok", "off", {"ready": True}))
        self.assertEqual((health["memories"], health["pending"]), (1, 0))
        self.assertEqual(health["embedder"]["state"], "ready")
        self.assertEqual(health["embedder"]["collection"], self.embedding.collection)
        whoami = self.client.get("/v1/whoami").json()
        self.assertEqual((whoami["auth"], whoami["person"], whoami["scopes"]), ("off", None, ["read", "write", "admin"]))

    def test_unknown_routes_and_methods_answer_json(self):
        missing = self.client.get("/v1/nothing")
        self.assertEqual((missing.status_code, missing.json()), (404, {"error": "Not Found"}))
        self.assertEqual(self.client.put("/v1/memories", json={}).status_code, 405)


class ImportRouteTest(AppCase):
    def row(self, text, updated_at):
        return {**body(text=text), "id": "miguel-learning-old", "person": "miguel", "updated_at": updated_at, "commits": []}

    def test_newer_updated_at_wins_and_rows_are_embedded(self):
        first = self.post(
            "/v1/admin/import",
            {"memories": [self.row("v1", "2026-11-01T00:00:00+00:00")], "rejections": [{"repo": REPO, "sha": A}]},
        )
        self.assertEqual(first.json(), {"written": 1, "skipped": 0, "rejections": 1, "embedded": True})
        newer = self.post("/v1/admin/import", {"memories": [self.row("v2", "2026-11-02T00:00:00+00:00")]})
        older = self.post("/v1/admin/import", {"memories": [self.row("v0", "2026-10-01T00:00:00+00:00")]})
        self.assertEqual((newer.json()["written"], older.json()["skipped"]), (1, 1))
        kept = self.client.get("/v1/memories/miguel-learning-old").json()
        self.assertEqual((kept["text"], kept["person"]), ("v2", "miguel"))
        self.assertEqual(self.fake.payloads(self.embedding.collection)["miguel-learning-old"]["text"], "v2")
        bad = self.post("/v1/admin/import", {"memories": [{**self.row("x", "2026-11-03T00:00:00+00:00"), "transcript_path": "t"}]})
        self.assertEqual(bad.status_code, 400)


class AuthOnTest(AppCase):
    """Auth on: tokens, scopes, identity pinning, child tokens and whoami over HTTP."""

    auth_mode = "on"

    def setUp(self):
        super().setUp()
        self.writer_id, self.writer = self.store.create_token(
            "miguel", "Acme", "Platform", ("read", "write"), allowed_teams=("Infra",)
        )
        _, self.reader = self.store.create_token("ana", "Acme", "Platform", ("read",))
        _, self.admin = self.store.create_token("ops", "Acme", "Platform", ("read", "write", "admin"))

    def as_(self, secret):
        return {"Authorization": f"Bearer {secret}"}

    def anonymous_body(self, **changes):
        document = body(**changes)
        for field in ("person", "org", "team"):
            if field not in changes:
                document.pop(field)
        return document

    def test_health_needs_no_token_and_everything_else_does(self):
        self.assertEqual(self.client.get("/v1/health").json()["auth"], "on")
        for method, path in (
            ("get", "/v1/whoami"),
            ("get", "/v1/memories"),
            ("get", "/v1/memories/x-learning-y"),
            ("post", "/v1/memories"),
            ("post", "/v1/rejections"),
            ("post", "/v1/commits/status"),
            ("post", "/v1/admin/import"),
            ("post", "/v1/tokens"),
        ):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, **({"json": {}} if method == "post" else {}))
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        bogus = self.client.get("/v1/memories", headers=self.as_("am_not-a-real-token"))
        self.assertEqual(bogus.status_code, 401)

    def test_read_cant_write_and_write_cant_import(self):
        self.assertEqual(self.client.get("/v1/memories", headers=self.as_(self.reader)).status_code, 200)
        denied = self.client.post("/v1/memories", json=self.anonymous_body(), headers=self.as_(self.reader))
        self.assertEqual(denied.status_code, 403)
        self.assertIn("write scope", denied.json()["error"])
        self.assertEqual(self.client.post("/v1/rejections", json={"repo": REPO, "shas": [A]}, headers=self.as_(self.reader)).status_code, 403)
        imported = self.client.post("/v1/admin/import", json={"memories": []}, headers=self.as_(self.writer))
        self.assertEqual(imported.status_code, 403)
        self.assertEqual(self.client.post("/v1/admin/import", json={"memories": []}, headers=self.as_(self.admin)).status_code, 200)
        self.assertEqual(self.store.count_memories(), 0)

    def test_the_token_pins_person_and_records_who_wrote_it(self):
        created = self.client.post("/v1/memories", json=self.anonymous_body(), headers=self.as_(self.writer))
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["id"], "miguel-learning-s1")
        kept = self.client.get("/v1/memories/miguel-learning-s1", headers=self.as_(self.reader)).json()
        self.assertEqual((kept["person"], kept["org"], kept["team"]), ("miguel", "Acme", "Platform"))
        connection = __import__("sqlite3").connect(self.store.path)
        try:
            written_by = connection.execute("SELECT written_by FROM memory").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(written_by, self.writer_id)
        as_ana = self.client.post("/v1/memories", json=body(person="ana"), headers=self.as_(self.writer))
        self.assertEqual(as_ana.status_code, 403)
        self.assertIn("writes as miguel", as_ana.json()["error"])

    def test_org_and_team_only_from_the_tokens_lists(self):
        infra = self.client.post(
            "/v1/memories", json=self.anonymous_body(session_id="s2", team="Infra"), headers=self.as_(self.writer)
        )
        self.assertEqual(infra.status_code, 201)
        sales = self.client.post(
            "/v1/memories", json=self.anonymous_body(session_id="s3", team="Sales"), headers=self.as_(self.writer)
        )
        self.assertEqual(sales.status_code, 403)
        other_org = self.client.post(
            "/v1/memories", json=self.anonymous_body(session_id="s4", org="Other"), headers=self.as_(self.writer)
        )
        self.assertEqual(other_org.status_code, 403)

    def test_whoami_reports_the_tokens_identity(self):
        who = self.client.get("/v1/whoami", headers=self.as_(self.writer)).json()
        self.assertEqual((who["auth"], who["token"], who["person"]), ("on", self.writer_id, "miguel"))
        self.assertEqual((who["allowed_orgs"], who["allowed_teams"]), (["Acme"], ["Platform", "Infra"]))
        self.assertEqual(who["scopes"], ["read", "write"])

    def test_child_tokens_for_editors(self):
        minted = self.client.post("/v1/tokens", json={"label": "claude-code"}, headers=self.as_(self.writer))
        self.assertEqual(minted.status_code, 201, minted.text)
        child = minted.json()
        self.assertEqual((child["person"], child["scopes"], child["label"]), ("miguel", ["read"], "claude-code"))
        self.assertEqual(self.client.get("/v1/memories", headers=self.as_(child["token"])).status_code, 200)
        self.assertEqual(
            self.client.post("/v1/memories", json=self.anonymous_body(), headers=self.as_(child["token"])).status_code, 403
        )
        self.assertEqual(self.client.post("/v1/tokens", json={"label": "x"}, headers=self.as_(child["token"])).status_code, 403)
        self.assertEqual(self.client.post("/v1/tokens", json={"label": "x"}, headers=self.as_(self.reader)).status_code, 403)
        again = self.client.post("/v1/tokens", json={"label": "claude-code"}, headers=self.as_(self.writer)).json()
        self.assertEqual(self.client.get("/v1/memories", headers=self.as_(child["token"])).status_code, 401)
        self.assertEqual(self.client.get("/v1/memories", headers=self.as_(again["token"])).status_code, 200)
        for bad in ({}, {"label": ""}, {"label": "x" * 65}, {"label": "a\nb"}, {"label": "a\tb"}, {"label": "x", "scopes": ["write"]}):
            with self.subTest(body=bad):
                self.assertEqual(self.client.post("/v1/tokens", json=bad, headers=self.as_(self.writer)).status_code, 400)

    def test_a_write_only_token_cant_mint_read_access(self):
        _, write_only = self.store.create_token("bot", "Acme", "Platform", ("write",))
        refused = self.client.post("/v1/tokens", json={"label": "sneaky"}, headers=self.as_(write_only))
        self.assertEqual(refused.status_code, 403)
        self.assertIn("read scope", refused.json()["error"])
        self.assertEqual(len([row for row in self.store.list_tokens() if row["parent_id"]]), 0)

    def test_revoking_the_parent_revokes_its_children(self):
        child = self.client.post("/v1/tokens", json={"label": "cursor"}, headers=self.as_(self.writer)).json()
        self.store.revoke_token(self.writer_id)
        for secret in (self.writer, child["token"]):
            self.assertEqual(self.client.get("/v1/memories", headers=self.as_(secret)).status_code, 401)

    def test_browser_rules_still_come_first(self):
        response = self.client.post(
            "/v1/memories", content=b"{}", headers={**self.as_(self.writer), "Content-Type": "text/plain"}
        )
        self.assertEqual(response.status_code, 415)


class AuthOffTokensTest(AppCase):
    def test_minting_a_token_with_auth_off_is_refused(self):
        refused = self.client.post("/v1/tokens", json={"label": "x"})
        self.assertEqual(refused.status_code, 400)
        self.assertIn("auth is off", refused.json()["error"])
        self.assertEqual(self.client.get("/v1/whoami").json()["identity"], "from the request body")


if __name__ == "__main__":
    unittest.main()
