"""The read-only MCP tools at /mcp: search, get and list. Reshaped from the stdio branch (16a1f2c).

search embeds the question with the server's embedder and queries the active
collection, then reads each hit back from memories.sqlite. get and list read
memories.sqlite only. None of them writes, and there is no write tool: writes
go through the /v1 API, which only the review flow calls.

Superseded memories are returned like any other, with superseded_by naming
the newer one. Nothing is hidden: a supersede may come from another org, so
the reader sees both notes and decides.

Everything above build_server() is standard library, so the plain python3
suite tests it. build_server() needs the server extra.
"""

import logging

from embedder import EmbedderError
from store_core import StoreError

from agent_memory_server import rules
from agent_memory_server.qdrant import QdrantError, QdrantUnavailable

log = logging.getLogger("agent_memory_server")

FILTER_KEYS = ("repo", "domain", "kind", "org", "team", "person")
SEARCH_LIMIT = (10, 50)
LIST_LIMIT = (20, 100)
MAX_QUERY = 2000

INSTRUCTIONS = """\
Approved memories from past coding sessions and git history: what changed and why
(kind change), what was learned (learning), and what was decided (decision), in the
domains code, architecture, and operations. Each memory names its repo as a remote id
such as github.com/owner/name, and the person, org, and team that approved it.

Use search for a question in words. Pass repo to stay inside one repository.
Use get to read one memory by document_id. Use list to browse by repo and time.

A memory with superseded_by has a newer one that replaces it; get that id too and
read both. The newer one may come from another org or team. A memory with supersedes
names the one it replaces.

A change memory points at its commits and pull request; read the diff with
`git show --stat <sha>` or `gh pr view <number>` in that repository. A commit can be
missing after a squash merge, so check with `git cat-file -e <sha>` first."""


class QueryError(Exception):
    """A tool call that can't be answered. Its text goes back to the caller."""


def _optional(value):
    """None for a missing or blank argument, else the stripped text."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clamp_limit(limit, bounds):
    default, maximum = bounds
    if limit is None or limit == "":
        return default
    if isinstance(limit, bool):
        raise QueryError(f"limit must be a whole number, not {limit!r}")
    try:
        number = int(limit)
    except (TypeError, ValueError):
        raise QueryError(f"limit must be a whole number, not {limit!r}") from None
    return min(max(number, 1), maximum)


def filters_from(**given):
    """The non-blank filters, in FILTER_KEYS order."""
    unknown = sorted(set(given) - set(FILTER_KEYS))
    if unknown:
        raise QueryError(f"unknown filter: {', '.join(unknown)}")
    return {key: text for key in FILTER_KEYS if (text := _optional(given.get(key))) is not None}


def qdrant_filter(filters):
    """A Qdrant filter on the keyword payload indexes, or None when unfiltered."""
    if not filters:
        return None
    return {"must": [{"key": key, "match": {"value": value}} for key, value in filters.items()]}


def search_body(vector, filters, limit):
    body = {
        "query": list(vector),
        "limit": limit,
        "with_payload": ["document_id"],
        "with_vector": False,
    }
    flt = qdrant_filter(filters)
    if flt:
        body["filter"] = flt
    return body


def search(store, embedding, query, limit=None, **given):
    """Semantic search over the active collection. Hits come back in get's shape, plus score."""
    text = _optional(query)
    if text is None:
        raise QueryError("query is blank")
    if len(text) > MAX_QUERY:
        raise QueryError(f"query is longer than {MAX_QUERY} characters")
    size = clamp_limit(limit, SEARCH_LIMIT)
    filters = filters_from(**given)
    if embedding.state == "mismatch":
        raise QueryError(f"search is off until the embedder is fixed: {embedding.error}")
    try:
        vector = embedding.embed_query(text)
    except EmbedderError as error:
        raise QueryError(f"the query could not be embedded: {error}") from None
    try:
        points = embedding.qdrant.query(embedding.collection, search_body(vector, filters, size))
    except QdrantUnavailable as error:
        raise QueryError(f"{error}. get and list still work.") from None
    except QdrantError as error:
        log.warning("search failed: %s", error)
        raise QueryError(f"search failed: {error}") from None
    if points is None:
        raise QueryError(
            f"collection {embedding.collection} is not in Qdrant yet; the server creates it when "
            "Qdrant answers. get and list still work."
        )
    scores = {}
    for point in points:
        document_id = (point.get("payload") or {}).get("document_id")
        if document_id and document_id not in scores:
            scores[document_id] = round(float(point.get("score") or 0.0), 4)
    try:
        rows = store.memories_by_ids(list(scores))
        waiting = store.count_pending(embedding.collection)
    except StoreError as error:
        raise QueryError(str(error)) from None
    results = [{**rows[document_id], "score": score} for document_id, score in scores.items() if document_id in rows]
    return {"query": text, "filter": filters, "results": results, "not_yet_searchable": waiting}


def get(store, document_id):
    wanted = _optional(document_id)
    if wanted is None:
        raise QueryError("document_id is blank")
    try:
        found = store.get_memory(wanted)
    except (StoreError, ValueError) as error:
        raise QueryError(str(error)) from None
    if found is None:
        raise QueryError(f"no approved memory {wanted}")
    return found


def list_memories(store, since=None, until=None, limit=None, **given):
    """Metadata query, newest occurred_at first. No ranking."""
    filters = filters_from(**given)
    try:
        bounds = {"since": rules.time_filter(since, "since"), "until": rules.time_filter(until, "until")}
    except ValueError as error:
        raise QueryError(str(error)) from None
    try:
        hits = store.query_memories(**filters, **bounds, limit=clamp_limit(limit, LIST_LIMIT))
    except (StoreError, ValueError) as error:
        raise QueryError(str(error)) from None
    return {"filter": filters, "memories": hits}


FILTER_HELP = (
    "repo is a remote id such as github.com/owner/name. kind is change, learning or decision. "
    "domain is code, architecture or operations. org, team and person match who approved it."
)


def build_server(store, embedding):
    """The MCP server with the three tools. Needs the server extra."""
    from typing import Any

    import anyio
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    # subscriptions=False: the tools are read-only and fixed, so there is nothing
    # to notify. The SDK's default subscriptions/listen stream stays open until
    # the client leaves, and on SIGTERM uvicorn waits for it before anything else.
    server = MCPServer("agent-memory", instructions=INSTRUCTIONS, subscriptions=False)
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    async def answer(work, *args, **kwargs):
        # SQLite, the model and Qdrant all block, so they run off the event loop.
        try:
            return await anyio.to_thread.run_sync(lambda: work(*args, **kwargs))
        except QueryError as error:
            raise ToolError(str(error)) from None

    @server.tool(
        name="search",
        description=(
            "Semantic search over approved memories, best match first. Each hit has document_id, "
            "score, text, kind, domain, repo, commits, pull_request, occurred_at, person, org and "
            "team, and superseded_by or supersedes when set. " + FILTER_HELP
        ),
        annotations=read_only,
    )
    async def search_tool(
        query: str,
        repo: str | None = None,
        domain: str | None = None,
        kind: str | None = None,
        org: str | None = None,
        team: str | None = None,
        person: str | None = None,
        limit: int = SEARCH_LIMIT[0],
    ) -> dict[str, Any]:
        return await answer(
            search, store, embedding, query, limit=limit,
            repo=repo, domain=domain, kind=kind, org=org, team=team, person=person,
        )

    @server.tool(
        name="get",
        description="Read one approved memory by document_id, with its commits.",
        annotations=read_only,
    )
    async def get_tool(document_id: str) -> dict[str, Any]:
        return await answer(get, store, document_id)

    @server.tool(
        name="list",
        description=(
            "List approved memories by metadata, newest first. No semantic ranking. since and "
            "until are ISO-8601 times with a zone, such as 2026-08-01T00:00:00+00:00, or a date "
            "such as 2026-08-01 (midnight UTC). " + FILTER_HELP
        ),
        annotations=read_only,
    )
    async def list_tool(
        repo: str | None = None,
        domain: str | None = None,
        kind: str | None = None,
        org: str | None = None,
        team: str | None = None,
        person: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = LIST_LIMIT[0],
    ) -> dict[str, Any]:
        return await answer(
            list_memories, store, since=since, until=until, limit=limit,
            repo=repo, domain=domain, kind=kind, org=org, team=team, person=person,
        )

    return server
