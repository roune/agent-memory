#!/usr/bin/env python3
"""The CLI's server client: config files, HTTP calls, connect, migrate, and list.

Standard library only. A stub server on 127.0.0.1 plays the agent-memory
server, so the client's real urllib requests are what the tests check. One
test at the end runs migrate against the real app, and needs the server extra.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import logging
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory
import memory_migrate
import server_client
import store_core
from stub_server import StubServer

HAVE_STARLETTE = all(importlib.util.find_spec(name) for name in ("starlette", "httpx"))

REPO = "github.com/example/proj"
OTHER = "github.com/example/other"


def sha(number):
    return hashlib.sha1(str(number).encode()).hexdigest()


def fixture_rows(count=86, repo=REPO, people=("miguel", "ana")):
    """count rows in the shape save_approved stores, with their own updated_at and occurred_at."""
    rows = []
    for index in range(count):
        kind = ("change", "learning", "decision")[index % 3]
        document = {
            "id": f"{people[index % len(people)]}-{kind}-s{index}",
            "kind": kind,
            "domain": ("code", "architecture", "operations")[index % 3],
            "repo": repo,
            "tool": "git" if kind == "change" else "claude-code",
            "session_id": f"s{index}",
            "text": f"memory number {index}",
            "org": "Org",
            "team": "Team",
            "person": people[index % len(people)],
            "occurred_at": f"2026-11-{1 + index % 28:02d}T10:{index % 60:02d}:00+00:00",
        }
        if kind == "change":
            document["commit"] = sha(index)
            document["commits"] = [sha(index + 1000), sha(index)]
            document["pull_request"] = index
        if index == 4:
            document["superseded_by"] = "ana-learning-s7"
        rows.append((document, f"2026-11-{1 + index % 28:02d}T12:00:{index % 60:02d}+00:00"))
    return rows


def write_store(path, rows, rejections=()):
    connection = store_core.connect(Path(path))
    try:
        for document, updated_at in rows:
            row = store_core.normalize_document(document, updated_at)
            store_core._upsert_shared(connection, row, row["commits"])
        for repo, commit in rejections:
            connection.execute(
                "INSERT INTO rejected_commit (repo, sha, rejected_at) VALUES (?, ?, ?)",
                (repo, commit, "2026-11-18T12:59:52+00:00"),
            )
    finally:
        connection.close()


class HomeCase(unittest.TestCase):
    """A temporary HOME and AGENT_MEMORY_DIR, so nothing touches the real ~/.config or data/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.state = Path(self.tmp.name) / "state"
        self.home.mkdir()
        self.state.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home), "AGENT_MEMORY_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = self.home / ".config" / "agent-memory"

    def write_config(self, name, text):
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / name).write_text(text, encoding="utf-8")

    def stub(self, **kwargs):
        server = StubServer(**kwargs)
        self.addCleanup(server.close)
        return server


def mode_bits(path):
    return stat.S_IMODE(path.stat().st_mode)


class ConfigTest(HomeCase):
    def test_no_server_file_means_no_server(self):
        self.assertIsNone(server_client.read_config())
        self.assertIsNone(server_client.target(REPO))

    def test_server_file_with_comments_quotes_and_mode(self):
        self.write_config("server", "# mine\nurl = 'http://127.0.0.1:8765/'\ntoken = am_abc  # read and write\nmode = Per_Repo\n")
        self.assertEqual(
            server_client.read_config(), {"url": "http://127.0.0.1:8765", "token": "am_abc", "mode": "per-repo"}
        )

    def test_a_bad_mode_or_url_names_the_file(self):
        self.write_config("server", "url = http://127.0.0.1:8765\nmode = everywhere\n")
        with self.assertRaisesRegex(server_client.ConfigError, "server has mode 'everywhere'"):
            server_client.read_config()
        self.write_config("server", "url = 127.0.0.1:8765\n")
        with self.assertRaisesRegex(server_client.ConfigError, "http:// or https://"):
            server_client.read_config()

    def test_shared_mode_sends_every_repo_to_one_server(self):
        self.write_config("server", "url = http://a.test\ntoken = am_a\n")
        self.write_config("servers", f"{REPO} = http://b.test\n")
        self.assertEqual(server_client.target(REPO), ("http://a.test", "am_a"))

    def test_per_repo_mapping(self):
        self.write_config("server", "url = http://a.test\ntoken = am_a\nmode = per-repo\n")
        self.write_config("servers", f"{REPO} = http://b.test/\n{OTHER} = http://a.test\n")
        self.write_config("tokens", "http://b.test = am_b\n")
        self.assertEqual(server_client.target(REPO), ("http://b.test", "am_b"))
        self.assertEqual(server_client.target(OTHER), ("http://a.test", "am_a"), "the default url keeps its token")
        self.assertEqual(server_client.target("github.com/x/unmapped"), ("http://a.test", "am_a"))
        self.write_config("tokens", "")
        self.assertEqual(server_client.target(REPO), ("http://b.test", None), "another server never gets the default token")

    def test_legacy_mode_comes_from_the_database_file(self):
        self.assertEqual(server_client.legacy_mode(), "shared")
        self.write_config("database", "mode = per-repo\nqdrant_url = http://127.0.0.1:6333\n")
        self.assertEqual(server_client.legacy_mode(), "per-repo")

    def test_written_files_holding_tokens_are_0600(self):
        self.write_config("server", "url = http://old.test\n")
        os.chmod(self.config / "server", 0o644)
        path = server_client.write_config("http://a.test/", "am_a", "per-repo")
        self.assertEqual(mode_bits(path), 0o600)
        self.assertEqual(server_client.read_config(), {"url": "http://a.test", "token": "am_a", "mode": "per-repo"})
        self.write_config("servers", f"{OTHER} = http://c.test\n")
        servers, tokens = server_client.map_repo(REPO, "http://b.test", "am_b")
        self.assertEqual(mode_bits(tokens), 0o600)
        self.assertEqual(server_client.read_servers(), {OTHER: "http://c.test", REPO: "http://b.test"})
        server_client.map_repo(REPO, "http://d.test")
        self.assertEqual(server_client.read_servers()[REPO], "http://d.test", "a remote is mapped once")
        self.assertEqual(server_client.read_tokens(), {"http://b.test": "am_b"})


class ClientTest(HomeCase):
    def test_every_request_sends_json_and_no_origin(self):
        stub = self.stub(token="am_t")
        client = server_client.Client(stub.url, "am_t")
        client.whoami()
        client.commits_status(REPO, [sha(1)])
        for request in stub.requests:
            headers = {key.lower(): value for key, value in request["headers"].items()}
            self.assertEqual(headers["content-type"], "application/json")
            self.assertNotIn("origin", headers)
            self.assertEqual(headers["authorization"], "Bearer am_t")
            self.assertEqual(headers["user-agent"], "agent-memory-cli")
        self.assertEqual(stub.requests[1]["body"], {"repo": REPO, "shas": [sha(1)]})

    def test_no_token_sends_no_authorization(self):
        stub = self.stub()
        server_client.Client(stub.url).whoami()
        self.assertNotIn("authorization", {key.lower() for key in stub.requests[0]["headers"]})

    def test_errors(self):
        stub = self.stub(token="am_t")
        with self.assertRaises(server_client.ServerError) as caught:
            server_client.Client(stub.url, "am_wrong").whoami()
        self.assertEqual(caught.exception.status, 401)
        self.assertIn("connect again", server_client.explain(caught.exception))
        self.assertIsNone(server_client.Client(stub.url, "am_t").get_memory("no/such id"))
        self.assertEqual(stub.requests[-1]["path"], "/v1/memories/no%2Fsuch%20id", "the id is quoted, slash included")
        stub.close()
        with self.assertRaisesRegex(server_client.ServerUnreachable, "did not answer"):
            server_client.Client(stub.url, "am_t", timeout=2).whoami()

    def test_a_redirect_is_never_followed(self):
        # urllib would send the token to the Location host, and turn the POST into a GET.
        elsewhere = self.stub()
        stub = self.stub(token="am_t")
        stub.redirect_to = elsewhere.url + "/steal"
        client = server_client.Client(stub.url, "am_t")
        for call in (client.whoami, lambda: client.import_rows([], [])):
            with self.assertRaises(server_client.ServerError) as caught:
                call()
            self.assertEqual(caught.exception.status, 302)
            self.assertIn("never follows a redirect", str(caught.exception))
            self.assertIn(elsewhere.url + "/steal", str(caught.exception))
        self.assertEqual(elsewhere.requests, [], "nothing reached the other host")
        self.assertEqual([request["method"] for request in stub.requests], ["GET", "POST"])

    def test_a_page_that_is_not_json_is_not_the_server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HtmlHandler)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.handle_request, daemon=True).start()
        with self.assertRaisesRegex(server_client.ServerUnreachable, "is that the agent-memory server"):
            server_client.Client(f"http://127.0.0.1:{server.server_port}").health()


class _HtmlHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html>not this</html>")


def run_cli(function, stdin="", **values):
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
        code = function(argparse.Namespace(**values))
    return code, out.getvalue()


def connect(url, stdin="", token_stdin=False, mode=None, repo=""):
    return run_cli(agent_memory.connect_cmd, stdin, url=url, token_stdin=token_stdin, mode=mode, repo=repo)


class ConnectTest(HomeCase):
    def test_connect_with_a_token_writes_the_server_file_0600(self):
        stub = self.stub(token="am_t")
        code, out = connect(stub.url + "/", stdin="am_t\n", token_stdin=True)
        self.assertEqual(code, 0)
        self.assertEqual(server_client.read_config(), {"url": stub.url, "token": "am_t", "mode": "shared"})
        self.assertEqual(mode_bits(self.config / "server"), 0o600)
        self.assertIn("as tester in Org/Team, scopes: read, write", out)

    def test_connect_with_auth_off(self):
        stub = self.stub()
        code, out = connect(stub.url)
        self.assertEqual(code, 0)
        self.assertNotIn("token", (self.config / "server").read_text())
        self.assertIn("auth is off", out)

    def test_nothing_is_written_when_the_check_fails(self):
        stub = self.stub(token="am_t")
        with self.assertRaisesRegex(SystemExit, "connect again with --token-stdin"):
            connect(stub.url)
        with self.assertRaisesRegex(SystemExit, "unknown or revoked"):
            connect(stub.url, stdin="am_wrong\n", token_stdin=True)
        with self.assertRaisesRegex(SystemExit, "empty line"):
            connect(stub.url, stdin="\n", token_stdin=True)
        stub.close()
        with self.assertRaisesRegex(SystemExit, "did not answer"):
            connect(stub.url)
        self.assertFalse((self.config / "server").exists())

    def test_the_legacy_mode_carries_over_and_repo_maps_a_remote(self):
        self.write_config("database", "mode = per-repo\n")
        default, team = self.stub(), self.stub(token="am_team")
        with self.assertRaisesRegex(SystemExit, "per-repo mode"):
            connect(team.url, stdin="am_team\n", token_stdin=True, repo=REPO)
        connect(default.url)
        self.assertEqual(server_client.read_config()["mode"], "per-repo")
        code, out = connect(team.url, stdin="am_team\n", token_stdin=True, repo=REPO)
        self.assertEqual(code, 0)
        self.assertEqual(server_client.target(REPO), (team.url, "am_team"))
        self.assertEqual(server_client.target(OTHER), (default.url, None))
        self.assertEqual(server_client.read_config()["url"], default.url, "--repo leaves the server file alone")


def migrate(dry_run=False, client_for=None):
    out = io.StringIO()
    code = memory_migrate.migrate(dry_run=dry_run, out=out, client_for=client_for)
    return code, out.getvalue()


class MigrateTest(HomeCase):
    def setUp(self):
        super().setUp()
        self.rows = fixture_rows()
        self.local = self.state / "data" / "memories.sqlite"
        write_store(self.local, self.rows, [(REPO, sha(1)), (REPO, sha(2))])
        self.stub_server = self.stub(token="am_admin")
        self.write_config("server", f"url = {self.stub_server.url}\ntoken = am_admin\n")

    def imports(self):
        return [request for request in self.stub_server.requests if request["path"] == "/v1/admin/import"]

    def test_dry_run_counts_and_sends_nothing(self):
        code, out = migrate(dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("memories: 86", out)
        self.assertIn("rejected commits: 2", out)
        self.assertIn(f"repos: {REPO}", out)
        self.assertIn("people: ana, miguel", out)
        self.assertEqual(self.stub_server.requests, [])

    def test_dry_run_without_a_server_still_counts(self):
        (self.config / "server").unlink()
        code, out = migrate(dry_run=True)
        self.assertIn("(no server configured)", out)
        self.assertIn("memories: 86", out)
        with self.assertRaisesRegex(server_client.ConfigError, "connect --url"):
            migrate()

    def test_rows_arrive_with_ids_people_and_timestamps_intact(self):
        before = self.local.read_bytes()
        code, out = migrate()
        self.assertEqual(code, 0, out)
        rows = self.stub_server.rows
        self.assertEqual(len(rows), 86)
        for document, updated_at in self.rows:
            sent = rows[document["id"]]
            self.assertEqual(sent["person"], document["person"])
            self.assertEqual(sent["updated_at"], updated_at)
            self.assertEqual(sent["occurred_at"], document["occurred_at"])
            self.assertEqual(sent["text"], document["text"])
            self.assertEqual(sent.get("commits", []), document.get("commits", []))
        self.assertEqual(rows["miguel-learning-s4"]["superseded_by"], "ana-learning-s7")
        self.assertEqual(self.stub_server.rejections, {(REPO, sha(1)), (REPO, sha(2))})
        self.assertEqual([len(request["body"]["memories"]) for request in self.imports()], [50, 36, 0])
        self.assertEqual(len(self.imports()[-1]["body"]["rejections"]), 2)
        self.assertIn("sent: 86 written, 0 already there or newer, 2 rejections new", out)
        self.assertIn("verified: all 86 memories and 2 rejections are there", out)
        self.assertIn("the server holds 86 memories in all", out)
        self.assertEqual(self.local.read_bytes(), before, "migrate never writes the laptop's file")

    def test_a_rerun_is_a_no_op(self):
        migrate()
        held = json.dumps(self.stub_server.rows, sort_keys=True)
        code, out = migrate()
        self.assertEqual(code, 0)
        self.assertIn("sent: 0 written, 86 already there or newer, 0 rejections new", out)
        self.assertEqual(json.dumps(self.stub_server.rows, sort_keys=True), held)

    def test_a_missing_or_changed_row_is_reported(self):
        original = StubServer.route

        def drops_one(stub, method, path, query, body):
            status, payload = original(stub, method, path, query, body)
            if path == "/v1/admin/import":
                stub.rows.pop("ana-learning-s1", None)
                if "miguel-change-s0" in stub.rows:
                    stub.rows["miguel-change-s0"] = {**stub.rows["miguel-change-s0"], "text": "edited on the server"}
            return status, payload

        with mock.patch.object(StubServer, "route", drops_one):
            code, out = migrate()
        self.assertEqual(code, 1)
        self.assertIn("missing on the server: ana-learning-s1", out)
        self.assertIn("differs on the server: miguel-change-s0", out)

    def test_a_refused_import_stops_with_its_error(self):
        self.stub_server.fail_next = (403, {"error": "this token lacks the admin scope"})
        with self.assertRaises(server_client.ServerError) as caught:
            migrate()
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.stub_server.rows, {})
        with self.assertRaisesRegex(SystemExit, "lacks the admin scope"):
            self.stub_server.fail_next = (403, {"error": "this token lacks the admin scope"})
            with contextlib.redirect_stdout(io.StringIO()):
                agent_memory.migrate_cmd(argparse.Namespace(dry_run=False))

    def test_per_repo_files_go_to_the_server_their_remote_maps_to(self):
        self.local.unlink()
        self.write_config("database", "mode = per-repo\n")
        team = self.stub(token="am_team")
        self.write_config("server", f"url = {self.stub_server.url}\ntoken = am_admin\nmode = per-repo\n")
        self.write_config("servers", f"{OTHER} = {team.url}\n")
        self.write_config("tokens", f"{team.url} = am_team\n")
        base = self.home / ".local" / "share" / "agent-memory" / "databases"
        write_store(base / "github.com--example--proj" / "memories.sqlite", fixture_rows(3), [(REPO, sha(9))])
        write_store(base / "github.com--example--other" / "memories.sqlite", fixture_rows(2, repo=OTHER, people=("zoe",)))
        code, out = migrate()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.stub_server.rows), ["ana-learning-s1", "miguel-change-s0", "miguel-decision-s2"])
        self.assertEqual(sorted(team.rows), ["zoe-change-s0", "zoe-learning-s1"])
        self.assertEqual(self.stub_server.rejections, {(REPO, sha(9))})
        self.assertEqual(team.rejections, set())

    def test_a_row_superseded_on_the_server_is_kept_and_passes(self):
        migrate()
        held = self.stub_server.rows["ana-learning-s1"]
        self.stub_server.rows["ana-learning-s1"] = {**held, "superseded_by": "ana-learning-s9", "updated_at": "2026-12-01T00:00:00+00:00"}
        code, out = migrate()
        self.assertEqual(code, 0, out)
        self.assertIn("newer on the server, kept there: ana-learning-s1", out)
        self.assertNotIn("differs", out)
        self.assertEqual(self.stub_server.rows["ana-learning-s1"]["superseded_by"], "ana-learning-s9")

    def test_the_token_needs_admin_and_read_before_anything_is_sent(self):
        for scopes, missing in ((("admin",), "lacks read"), (("read", "write"), "lacks admin")):
            self.stub_server.scopes = list(scopes)
            with self.subTest(scopes=scopes):
                with self.assertRaisesRegex(server_client.ConfigError, f"admin and read scopes.*{missing}. Nothing was sent"):
                    migrate()
        self.assertEqual(self.imports(), [])
        self.assertEqual(self.stub_server.rows, {})

    def test_the_local_files_come_from_the_legacy_mode_not_the_server_file(self):
        # data/memories.sqlite is a shared store: no database file says otherwise.
        self.write_config("server", f"url = {self.stub_server.url}\ntoken = am_admin\nmode = per-repo\n")
        code, out = migrate()
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.stub_server.rows), 86)

    def test_files_in_the_other_mode_are_not_silently_skipped(self):
        self.write_config("database", "mode = per-repo\n")
        code, out = migrate()
        self.assertEqual(code, 1)
        self.assertIn("is a shared store; set mode = shared in ~/.config/agent-memory/database", out)
        self.assertEqual(self.stub_server.requests, [])

    def test_an_id_in_two_local_files_is_refused(self):
        self.local.unlink()
        self.write_config("database", "mode = per-repo\n")
        self.write_config("server", f"url = {self.stub_server.url}\nmode = per-repo\n")
        base = self.home / ".local" / "share" / "agent-memory" / "databases"
        write_store(base / "a" / "memories.sqlite", fixture_rows(1))
        write_store(base / "b" / "memories.sqlite", fixture_rows(1))
        with self.assertRaisesRegex(store_core.StoreError, "more than one local file"):
            migrate()


class ListTest(HomeCase):
    def list_args(self, **changes):
        values = {name: None for name in agent_memory.LIST_FILTERS}
        values.update(limit=20, offset=0, database=None)
        values.update(changes)
        return values

    def test_list_goes_through_the_server_when_one_is_connected(self):
        stub = self.stub(token="am_t")
        self.write_config("server", f"url = {stub.url}\ntoken = am_t\n")
        stub.rows = {document["id"]: {**document, "updated_at": stamp} for document, stamp in fixture_rows(3)}
        code, out = run_cli(agent_memory.list_memories, **self.list_args(repo=REPO, since="2026-11-01"))
        self.assertEqual(code, 0)
        self.assertEqual([hit["document_id"] for hit in json.loads(out)], ["miguel-change-s0", "ana-learning-s1", "miguel-decision-s2"])
        query = stub.requests[-1]["query"]
        self.assertEqual(query["repo"], [REPO])
        self.assertEqual(query["since"], ["2026-11-01"])
        self.assertEqual(query["limit"], ["20"])
        self.assertNotIn("team", query)

    def test_a_refused_filter_is_the_servers_message(self):
        stub = self.stub()
        self.write_config("server", f"url = {stub.url}\n")
        with self.assertRaisesRegex(SystemExit, "since must be an ISO-8601 time"):
            run_cli(agent_memory.list_memories, **self.list_args(since="yesterday"))

    def test_per_repo_list_asks_the_server_for_that_remote(self):
        default, team = self.stub(), self.stub()
        self.write_config("server", f"url = {default.url}\nmode = per-repo\n")
        self.write_config("servers", f"{OTHER} = {team.url}\n")
        run_cli(agent_memory.list_memories, **self.list_args(database=OTHER))
        self.assertEqual(len(team.requests), 1)
        self.assertEqual(default.requests, [])
        self.write_config("server", f"url = {default.url}\n")
        with self.assertRaisesRegex(SystemExit, "only used in per-repo mode"):
            run_cli(agent_memory.list_memories, **self.list_args(database=OTHER))

    def test_without_a_server_list_says_how_to_connect_and_never_reads_the_local_file(self):
        write_store(self.state / "data" / "memories.sqlite", fixture_rows(2))
        with self.assertRaisesRegex(SystemExit, "agent-memory connect --url"):
            run_cli(agent_memory.list_memories, **self.list_args())


@unittest.skipUnless(HAVE_STARLETTE, "needs the server extra (scripts/test-server)")
class MigrateIntoTheRealServerTest(HomeCase):
    """migrate against the real app, through Starlette's TestClient in place of urllib."""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient

        from agent_memory_server.app import create_app
        from agent_memory_server.embedding import Embedding
        from agent_memory_server.qdrant import Qdrant
        from agent_memory_server.settings import Settings
        from agent_memory_server.store import ServerStore
        from server_doubles import FakeQdrant, HashEmbedder

        quiet = logging.getLogger("httpx")
        self.addCleanup(quiet.setLevel, quiet.level)
        quiet.setLevel(logging.WARNING)
        data = Path(self.tmp.name) / "server"
        settings = Settings(data_dir=data, models_dir=data / "models", auth="off")
        self.server_store = ServerStore(settings.db_path)
        embedding = Embedding(self.server_store, HashEmbedder(), Qdrant("http://qdrant:6333", transport=FakeQdrant()))
        self.http = TestClient(create_app(settings, self.server_store, embedding, retry_seconds=0), base_url="http://127.0.0.1:8765")
        self.addCleanup(self.http.close)
        self.rows = fixture_rows()
        # A row kept before the review flow converted occurred_at to UTC.
        offset, stamp = self.rows[5]
        self.rows[5] = ({**offset, "occurred_at": "2026-11-20T11:00:00+01:00"}, stamp)
        no_time, stamp = self.rows[6]
        self.rows[6] = ({key: value for key, value in no_time.items() if key != "occurred_at"}, stamp)
        write_store(self.state / "data" / "memories.sqlite", self.rows, [(REPO, sha(1))])
        self.write_config("server", "url = http://127.0.0.1:8765\n")

    def opener(self, request, timeout):
        from urllib.error import HTTPError

        response = self.http.request(
            request.get_method(), request.full_url, content=request.data, headers=dict(request.header_items())
        )
        if response.status_code >= 400:
            raise HTTPError(request.full_url, response.status_code, response.reason_phrase, response.headers, io.BytesIO(response.content))
        return contextlib.closing(io.BytesIO(response.content))

    def client_for(self, url, token):
        return server_client.Client(url, token, opener=self.opener)

    def test_migrate_verifies_against_the_real_server(self):
        code, out = migrate(client_for=self.client_for)
        self.assertEqual(code, 0, out)
        self.assertIn("verified: all 86 memories and 1 rejections are there", out)
        self.assertIn("the server holds 86 memories in all", out)
        self.assertEqual(self.server_store.get_memory("ana-decision-s5")["occurred_at"], "2026-11-20T10:00:00+00:00")
        self.assertIsNone(self.server_store.get_memory("miguel-change-s6")["occurred_at"])
        code, out = migrate(client_for=self.client_for)
        self.assertEqual(code, 0, out)
        self.assertIn("sent: 0 written, 86 already there or newer, 0 rejections new", out)
        # A newer note supersedes a migrated one on the server; a rerun keeps it and passes.
        newer = {
            "kind": "learning", "domain": "code", "text": "replaces s1", "repo": REPO, "tool": "git", "session_id": "late",
            "org": "Org", "team": "Team", "person": "ana", "occurred_at": "2026-11-21T10:00:00+00:00", "supersedes": "ana-learning-s1",
        }
        self.assertEqual(self.http.post("/v1/memories", json=newer).status_code, 201)
        self.assertEqual(self.server_store.get_memory("ana-learning-s1")["superseded_by"], "ana-learning-late")
        code, out = migrate(client_for=self.client_for)
        self.assertEqual(code, 0, out)
        self.assertIn("newer on the server, kept there: ana-learning-s1", out)


if __name__ == "__main__":
    unittest.main()
