"""The HTTP app: /v1 routes for the CLI, behind the browser rules. Needs the server extra.

Auth is off in this version (docs/server-plan.md, PR 2c adds tokens), so every
caller has every scope and identity comes from the request body. The browser
rules are what keep web pages on this machine out:

- a Host not in AGENT_MEMORY_ALLOWED_HOSTS gets 421 (DNS rebinding)
- an Origin not in AGENT_MEMORY_ALLOWED_ORIGINS gets 403 (any web page)
- a POST, PUT, PATCH or DELETE without Content-Type: application/json gets 415
  (a cross-site form or text/plain POST needs no preflight; this one would)

They wrap the whole app, so /mcp gets them too when PR 3 mounts it there, with
transport_security() passed to the SDK as well.
"""

import asyncio
import contextlib
import json
import logging

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Route

from store_core import StoreError

from agent_memory_server import rules

log = logging.getLogger("agent_memory_server")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SCOPES_WITH_AUTH_OFF = ["read", "write", "admin"]
WRITTEN_BY_WITH_AUTH_OFF = "local"


def transport_security(settings):
    """The MCP SDK's own Host and Origin checks, for /mcp. The SDK turns them on by
    itself only when it binds a loopback host, and the server binds 0.0.0.0."""
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


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

    async def embed_now():
        return await run_in_threadpool(embedding.embed_pending)

    async def retry_loop():
        while True:
            try:
                await embed_now()
            except Exception:  # embed_pending doesn't raise; keep the loop alive if it ever does
                log.exception("embed retry failed")
            await asyncio.sleep(retry)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(retry_loop()) if retry else None
        try:
            yield
        finally:
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
        return JSONResponse(
            {
                "auth": "off",
                "person": None,
                "org": None,
                "team": None,
                "allowed_orgs": [],
                "allowed_teams": [],
                "scopes": SCOPES_WITH_AUTH_OFF,
                "identity": "from the request body",
                "embedder": embedding.status(),
            }
        )

    async def list_memories(request):
        filters = rules.query_from_params(dict(request.query_params))
        hits = await run_in_threadpool(lambda: store.query_memories(**filters))
        return JSONResponse({"memories": hits})

    async def get_memory(request):
        found = await run_in_threadpool(store.get_memory, request.path_params["memory_id"])
        if found is None:
            return JSONResponse({"error": "no memory with that id"}, status_code=404)
        return JSONResponse(found)

    async def post_memory(request):
        document = rules.memory_from_body(await _body(request))
        outcome, stored = await run_in_threadpool(store.write_memory, document, WRITTEN_BY_WITH_AUTH_OFF)
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
        repo, shas = rules.commits_from_body(await _body(request))
        count = await run_in_threadpool(store.reject_commits, repo, shas)
        return JSONResponse({"rejected": count})

    async def commits_status(request):
        repo, shas = rules.commits_from_body(await _body(request))

        def look():
            return {
                sha: {"kept": store.memory_exists_for_commit(sha), "rejected": store.commit_rejected(sha, repo)}
                for sha in shas
            }

        return JSONResponse({"commits": await run_in_threadpool(look)})

    async def admin_import(request):
        memories, rejections = rules.import_from_body(await _body(request))
        counts = await run_in_threadpool(store.import_rows, memories, rejections, WRITTEN_BY_WITH_AUTH_OFF)
        counts["embedded"] = await embed_now()
        return JSONResponse(counts)

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
        ],
        exception_handlers={
            BadRequest: bad_request,
            ValueError: bad_request,
            StoreError: store_failed,
            HTTPException: http_error,
        },
        lifespan=lifespan,
    )
    return BrowserRules(app, settings.allowed_hosts, settings.allowed_origins)

