"""The HTTP app: /v1 routes for the CLI, behind the browser rules. Needs the server extra.

With auth on (the default), every route but /v1/health needs a bearer token
with the route's scope, and a write takes its person from the token (auth.py).
With AGENT_MEMORY_AUTH=off (local compose only), every caller has every scope
and identity comes from the request body. Either way, the browser rules keep
web pages on this machine out:

- a Host not in AGENT_MEMORY_ALLOWED_HOSTS gets 421 (DNS rebinding)
- an Origin not in AGENT_MEMORY_ALLOWED_ORIGINS gets 403 (any web page)
- a POST, PUT or PATCH without Content-Type: application/json gets 415
  (a cross-site form or text/plain POST needs no preflight; this one would).
  DELETE is left out: it has no body here (an MCP client ends its session
  with one, often without a Content-Type), and a browser preflights it.

They wrap the whole app, /mcp included, and transport_security() passes the
same hosts and origins to the MCP SDK as well.

/mcp is the MCP SDK's streamable HTTP transport with the read-only tools in
mcp_tools.py. It needs the read scope, checked on every request before the
SDK sees it. Sessions are the SDK's, kept in memory: after a restart a client
gets 404 for its old session and starts a new one.
"""

import asyncio
import contextlib
import json
import logging

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from store_core import StoreError

from agent_memory_server import auth, mcp_tools, rules

log = logging.getLogger("agent_memory_server")

# Methods with a body. A cross-site DELETE always needs a preflight, which the server never answers.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})


def transport_security(settings):
    """The MCP SDK's own Host and Origin checks, for /mcp. The SDK turns them on by
    itself only when it binds a loopback host, and the server binds 0.0.0.0."""
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


def mcp_user(who):
    """The caller as the SDK's AuthenticatedUser, keyed on the token id (never the secret)."""
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    return AuthenticatedUser(AccessToken(token=who.token_id, client_id=who.token_id, scopes=list(who.scopes)))


async def _refuse(send, status, message):
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BrowserRules:
    """ASGI middleware: Host, Origin, then Content-Type, on every HTTP request."""

    def __init__(self, app, allowed_hosts, allowed_origins):
        self.app = app
        self.allowed_hosts = frozenset(host.lower() for host in allowed_hosts)
        self.allowed_origins = frozenset(origin.lower() for origin in allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        host = headers.get("host", "").lower()
        if host not in self.allowed_hosts:
            log.warning("refused Host %r", host)
            return await _refuse(send, 421, f"unknown Host {host!r}; see AGENT_MEMORY_ALLOWED_HOSTS")
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.allowed_origins:
            log.warning("refused Origin %r", origin)
            return await _refuse(send, 403, "requests from web pages are refused")
        if scope["method"] in WRITE_METHODS:
            kind = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind != "application/json":
                return await _refuse(send, 415, "send Content-Type: application/json")
        return await self.app(scope, receive, send)


class ASGIEndpoint:
    """Marks an ASGI callable for Route, which treats a plain function as a request handler."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)


class BadRequest(Exception):
    pass


async def _body(request):
    raw = await request.body()
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BadRequest("the body must be JSON") from None


def create_app(settings, store, embedding, retry_seconds=None):
    """The ASGI app. retry_seconds=None uses settings.retry_seconds; 0 turns the retry loop off."""
    retry = settings.retry_seconds if retry_seconds is None else retry_seconds

    async def caller(request, scope):
        """Who is calling. Raises AuthError: 401 without a valid token, 403 without the scope."""
        if settings.auth_off:
            return auth.Caller.local()
        secret = auth.bearer(request.headers.get("authorization"))
        if secret is None:
            raise auth.AuthError(401, "send Authorization: Bearer <token>")
        row = await run_in_threadpool(store.token_for_secret, secret)
        if row is None:
            raise auth.AuthError(401, "unknown or revoked token")
        found = auth.Caller.from_row(row)
        return found.require(scope) if scope else found

    async def embed_now():
        return await run_in_threadpool(embedding.embed_pending)

    async def retry_loop():
        while True:
            try:
                await embed_now()
            except Exception:  # embed_pending doesn't raise; keep the loop alive if it ever does
                log.exception("embed retry failed")
            await asyncio.sleep(retry)

    mcp_server = mcp_tools.build_server(store, embedding)
    # Builds the SDK's session manager. Its own Starlette app is not used: /mcp
    # is a route of this app, so there is one path, one lifespan, and the auth
    # check below runs first.
    mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        transport_security=transport_security(settings),
    )
    sessions = mcp_server.session_manager

    async def mcp_endpoint(scope, receive, send):
        who = await caller(Request(scope, receive), "read")
        if who.auth:
            # The SDK ties a session to the user that opened it, and answers 404
            # to any other. Without this every session's owner is None, so any
            # read token holding a session id could use or end it.
            scope = {**scope, "user": mcp_user(who)}
        await sessions.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(retry_loop()) if retry else None
        try:
            async with sessions.run():
                yield
        finally:
            # The retry task may be inside an embed in a threadpool thread, which
            # cancel() can't interrupt; stop() ends it after its current batch.
            embedding.stop()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def health(request):
        qdrant_ready = await run_in_threadpool(embedding.qdrant.ready)
        memories = await run_in_threadpool(store.count_memories)
        pending = await run_in_threadpool(store.count_pending, embedding.collection)
        return JSONResponse(
            {
                "status": "ok",
                "auth": "off" if settings.auth_off else "on",
                "qdrant": {"ready": qdrant_ready},
                "embedder": embedding.status(),
                "memories": memories,
                "pending": pending,
            }
        )

    async def whoami(request):
        who = await caller(request, None)
        return JSONResponse({**who.describe(), "embedder": embedding.status()})

    async def list_memories(request):
        await caller(request, "read")
        filters = rules.query_from_params(dict(request.query_params))
        hits = await run_in_threadpool(lambda: store.query_memories(**filters))
        return JSONResponse({"memories": hits})

    async def get_memory(request):
        await caller(request, "read")
        found = await run_in_threadpool(store.get_memory, request.path_params["memory_id"])
        if found is None:
            return JSONResponse({"error": "no memory with that id"}, status_code=404)
        return JSONResponse(found)

    async def post_memory(request):
        who = await caller(request, "write")
        document = rules.memory_from_body(auth.pin_identity(who, await _body(request)))
        outcome, stored = await run_in_threadpool(store.write_memory, document, who.token_id)
        if outcome == "conflict":
            return JSONResponse(
                {
                    "error": f"{stored['document_id']} is already kept with different content; "
                    "a newer memory replaces it through supersedes",
                    "id": stored["document_id"],
                    "updated_at": stored["updated_at"],
                    "stored": {"text": stored["text"]},
                },
                status_code=409,
            )
        await embed_now()
        embedded = await run_in_threadpool(store.is_embedded, stored["document_id"], embedding.collection)
        return JSONResponse(
            {"id": stored["document_id"], "updated_at": stored["updated_at"], "embedded": embedded},
            status_code=201 if outcome == "created" else 200,
        )

    async def post_rejections(request):
        await caller(request, "write")
        repo, shas = rules.commits_from_body(await _body(request))
        count = await run_in_threadpool(store.reject_commits, repo, shas)
        return JSONResponse({"rejected": count})

    async def commits_status(request):
        await caller(request, "read")
        repo, shas = rules.commits_from_body(await _body(request))

        def look():
            return {
                sha: {"kept": store.memory_exists_for_commit(sha), "rejected": store.commit_rejected(sha, repo)}
                for sha in shas
            }

        return JSONResponse({"commits": await run_in_threadpool(look)})

    async def admin_import(request):
        who = await caller(request, "admin")
        memories, rejections = rules.import_from_body(await _body(request))
        counts = await run_in_threadpool(store.import_rows, memories, rejections, who.token_id)
        # Embed after the reply is sent. A batch of 50 on a cold model can take
        # longer than the CLI's 10-second timeout, and migrate needs only the
        # rows: what is still pending shows in /v1/health. If another embed is
        # running, this one returns at once rather than park a threadpool
        # thread behind it; that embed or the retry loop takes these rows.
        return JSONResponse(counts, background=BackgroundTask(embedding.embed_pending_if_idle))

    async def post_tokens(request):
        """A read-only child of the caller's token, for an editor. The same label replaces the earlier one.

        It needs read as well as write: the child reads, and scopes don't imply
        each other, so a write-only token must not be able to mint read access.
        """
        who = await caller(request, "write")
        if not who.auth:
            raise BadRequest("auth is off, so editors need no token")
        who.require("read")
        body = await _body(request)
        if not isinstance(body, dict) or set(body) - {"label"}:
            raise BadRequest("send {\"label\": \"<what the token is for>\"}")
        label = auth.clean_label(body.get("label"), required=True)
        token_id, secret = await run_in_threadpool(
            lambda: store.create_token(
                who.person,
                who.org,
                who.team,
                ("read",),
                allowed_orgs=who.allowed_orgs,
                allowed_teams=who.allowed_teams,
                label=label,
                parent_id=who.token_id,
            )
        )
        return JSONResponse(
            {"id": token_id, "token": secret, "label": label, "person": who.person, "scopes": ["read"]},
            status_code=201,
        )

    async def refused(request, error):
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        body = {"error": str(error)}
        if error.code:
            body["code"] = error.code
        return JSONResponse(body, status_code=error.status, headers=headers)

    async def bad_request(request, error):
        return JSONResponse({"error": str(error)}, status_code=400)

    async def store_failed(request, error):
        log.error("store error: %s", error)
        return JSONResponse({"error": str(error)}, status_code=500)

    async def http_error(request, error):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    app = Starlette(
        routes=[
            Route("/v1/health", health, methods=["GET"]),
            Route("/v1/whoami", whoami, methods=["GET"]),
            Route("/v1/memories", list_memories, methods=["GET"]),
            Route("/v1/memories", post_memory, methods=["POST"]),
            Route("/v1/memories/{memory_id:str}", get_memory, methods=["GET"]),
            Route("/v1/rejections", post_rejections, methods=["POST"]),
            Route("/v1/commits/status", commits_status, methods=["POST"]),
            Route("/v1/admin/import", admin_import, methods=["POST"]),
            Route("/v1/tokens", post_tokens, methods=["POST"]),
            Route("/mcp", ASGIEndpoint(mcp_endpoint)),
        ],
        exception_handlers={
            auth.AuthError: refused,
            BadRequest: bad_request,
            ValueError: bad_request,
            StoreError: store_failed,
            HTTPException: http_error,
        },
        lifespan=lifespan,
    )
    return BrowserRules(app, settings.allowed_hosts, settings.allowed_origins)

