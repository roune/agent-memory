"""Test doubles for the CLI's side: an agent-memory server on 127.0.0.1, in memory.

Not shipped in the package, and not a test module itself. StubServer answers
the /v1 routes the CLI calls, with the write rules the real app applies
(agent_memory_server.rules, which is standard library only), so the CLI's real
urllib requests are what the tests check. SilentServer accepts connections and
never answers, like a server that hangs.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import memory_migrate
from agent_memory_server import rules


def _same_commit(left, right):
    left, right = left.lower(), right.lower()
    return left.startswith(right) or right.startswith(left)


class StubServer:
    """The /v1 routes, in memory. Newer updated_at wins on import; POST /v1/memories is insert, 200 or 409."""

    def __init__(self, token=None, person="tester", scopes=("read", "write", "admin"), org="Org", team="Team"):
        self.token = token
        self.person = person
        self.org = org
        self.team = team
        self.scopes = list(scopes)
        self.rows = {}
        self.rejections = set()
        self.requests = []
        self.fail_next = None
        # What /v1/health reports as "version"; None leaves the field out, as a server older than 0.2.0 does.
        self.version = None
        # path -> (status, payload), answered every time until removed.
        self.fail_path = {}
        self.redirect_to = None
        # POST /v1/tokens: label -> the active child secret, and every secret it replaced.
        self.children = {}
        self.revoked = []
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, status, payload):
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                body = json.loads(raw) if raw else None
                with stub.lock:
                    stub.requests.append(
                        {"method": self.command, "path": parts.path, "query": parse_qs(parts.query), "headers": dict(self.headers), "body": body}
                    )
                    if stub.fail_next:
                        status, payload = stub.fail_next
                        stub.fail_next = None
                        return self._answer(status, payload)
                    if parts.path in stub.fail_path:
                        return self._answer(*stub.fail_path[parts.path])
                    if parts.path == "/v1/health":
                        health = {"status": "ok", "memories": len(stub.rows)}
                        if stub.version:
                            health["version"] = stub.version
                        return self._answer(200, health)
                    if stub.token and self.headers.get("Authorization") != f"Bearer {stub.token}":
                        return self._answer(401, {"error": "unknown or revoked token"})
                    return self._answer(*stub.route(self.command, parts.path, parse_qs(parts.query), body))

            do_GET = do_POST = _handle

            def handle_one_request(self):
                if stub.redirect_to:
                    self.raw_requestline = self.rfile.readline(65537)
                    if not self.parse_request():
                        return
                    stub.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers)})
                    self.send_response(302)
                    self.send_header("Location", stub.redirect_to + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                super().handle_one_request()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def hit(self, document):
        return memory_migrate.expected_hit(document)

    def posts(self, path):
        return [request["body"] for request in self.requests if request["method"] == "POST" and request["path"] == path]

    def _pin(self, body):
        """The real app's identity rule, for one token with no allowed lists."""
        if not self.token or not isinstance(body, dict):
            return body
        if body.get("person") not in (None, "") and body["person"] != self.person:
            raise PermissionError(f"this token writes as {self.person}, not {body['person']!r}")
        for field, own in (("org", self.org), ("team", self.team)):
            if body.get(field) not in (None, "", own):
                raise PermissionError(f"this token can't write {field} {body[field]!r} (allowed: {own})")
        return {**body, "person": self.person, "org": body.get("org") or self.org, "team": body.get("team") or self.team}

    def post_memory(self, body):
        try:
            document = rules.memory_from_body(self._pin(body))
        except PermissionError as error:
            return 403, {"error": str(error), "code": "identity"}
        except ValueError as error:
            return 400, {"error": str(error)}
        current = self.rows.get(document["id"])
        if current is not None:
            # As the real app: superseded_by is set by a later write, so a resend still matches.
            kept = {key: value for key, value in current.items() if key not in ("updated_at", "superseded_by")}
            if kept != {key: value for key, value in document.items() if key != "superseded_by"}:
                return 409, {
                    "error": f"{document['id']} is already kept with different content; a newer memory replaces it through supersedes",
                    "id": document["id"],
                    "updated_at": current["updated_at"],
                    "stored": {"text": current["text"]},
                }
            return 200, {"id": document["id"], "updated_at": current["updated_at"], "embedded": True}
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        self.rows[document["id"]] = {**document, "updated_at": stamp}
        if document.get("supersedes") in self.rows:
            self.rows[document["supersedes"]]["superseded_by"] = document["id"]
        return 201, {"id": document["id"], "updated_at": stamp, "embedded": True}

    def kept(self, sha):
        for row in self.rows.values():
            listed = row.get("commits") or [row.get("commit")]
            if any(item and _same_commit(sha, item) for item in listed):
                return True
        return False

    def rejected(self, repo, sha):
        return any(repo == kept_repo and _same_commit(sha, kept_sha) for kept_repo, kept_sha in self.rejections)

    def route(self, method, path, query, body):
        if path == "/v1/whoami":
            if self.token:
                return 200, {
                    "auth": "on",
                    "person": self.person,
                    "org": self.org,
                    "team": self.team,
                    "allowed_orgs": [self.org],
                    "allowed_teams": [self.team],
                    "scopes": self.scopes,
                    "label": None,
                }
            return 200, {"auth": "off", "person": "", "org": "", "team": "", "scopes": ["read", "write", "admin"]}
        if path == "/v1/tokens" and method == "POST":
            if not self.token:
                return 400, {"error": "auth is off, so editors need no token"}
            if not {"read", "write"} <= set(self.scopes):
                return 403, {"error": "this token lacks the write scope"}
            if not isinstance(body, dict) or set(body) != {"label"}:
                return 400, {"error": 'send {"label": "<what the token is for>"}'}
            number = len(self.children) + len(self.revoked) + 1
            secret = f"am_child{number}_{body['label']}"
            if body["label"] in self.children:
                self.revoked.append(self.children[body["label"]])
            self.children[body["label"]] = secret
            return 201, {"id": f"t_child{number}", "token": secret, "label": body["label"], "person": self.person, "scopes": ["read"]}
        if path == "/v1/memories" and method == "POST":
            return self.post_memory(body)
        if path == "/v1/rejections" and method == "POST":
            try:
                repo, shas = rules.commits_from_body(body)
            except ValueError as error:
                return 400, {"error": str(error)}
            before = len(self.rejections)
            self.rejections.update((repo, sha) for sha in shas)
            return 200, {"rejected": len(self.rejections) - before}
        if path == "/v1/admin/import" and method == "POST":
            written = 0
            newer = []
            for document in body["memories"]:
                current = self.rows.get(document["id"])
                if current is None or document["updated_at"] > current["updated_at"]:
                    self.rows[document["id"]] = document
                    written += 1
                elif current["updated_at"] > document["updated_at"]:
                    newer.append(document["id"])
            added = 0
            for rejection in body["rejections"]:
                key = (rejection["repo"], rejection["sha"])
                if key not in self.rejections:
                    self.rejections.add(key)
                    added += 1
            return 200, {"written": written, "skipped": len(body["memories"]) - written, "rejections": added, "newer": newer}
        if path == "/v1/memories" and method == "GET":
            if "since" in query and query["since"][0] == "yesterday":
                return 400, {"error": "since must be an ISO-8601 time, e.g. 2026-11-01T00:00:00+00:00"}
            hits = [self.hit(row) for row in self.rows.values()]
            if "repo" in query:
                hits = [hit for hit in hits if hit["repo"] == query["repo"][0]]
            return 200, {"memories": hits[: int(query.get("limit", ["20"])[0])]}
        if path.startswith("/v1/memories/") and method == "GET":
            found = self.rows.get(unquote(path[len("/v1/memories/") :]))
            return (200, self.hit(found)) if found else (404, {"error": "no memory with that id"})
        if path == "/v1/commits/status" and method == "POST":
            try:
                repo, shas = rules.commits_from_body(body)
            except ValueError as error:
                return 400, {"error": str(error)}
            return 200, {"commits": {sha: {"kept": self.kept(sha), "rejected": self.rejected(repo, sha)} for sha in shas}}
        return 404, {"error": "not found"}


class SilentServer:
    """Accepts TCP connections and never answers. accepted records (time, what the callback saw)."""

    def __init__(self, on_accept=None):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.on_accept = on_accept
        self.accepted = []
        self.held = []
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.1)
        while not self.closed.is_set():
            try:
                connection, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.accepted.append((time.monotonic(), self.on_accept() if self.on_accept else None))
            self.held.append(connection)

    def close(self):
        self.closed.set()
        self.thread.join(timeout=2)
        for connection in self.held:
            connection.close()
        self.sock.close()
