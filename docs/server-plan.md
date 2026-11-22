# Server plan

Status: approved by Miguel on 21 November 2026, and amended on 22 November after an Opus review of PR 0 and PR 1. Each PR below says what it builds. The `context.md` wording changes near the end still need their own yes before PR 5.

This plan moves agent-memory from laptop scripts to one server in Docker. It covers three things together, because each one shapes the others:

- the server
- a pluggable embedding model
- the clients: editors and the `agent-memory` CLI

It ends in an ordered list of PRs. Each PR can be reviewed on its own, and `python3 -m unittest discover -s scripts -p 'test_*.py'` passes at every commit.

## What stays locked

These come from [context.md](context.md) and are not reopened here:

- **No automatic save.** A person approves every memory. The server's write API is never an MCP tool.
- **No raw transcripts.** Transcripts are never embedded and never leave the laptop. The write API accepts the fields of an approved document and nothing else. A `transcript_path` or any other unknown key gets a 400.
- **Review is the redaction step.** The inbox and the review page stay on the laptop. Only approved, edited text is sent.
- **Local spool first, then a sync of approved documents only.** The new outbox is that spool. It holds approved documents and rejections, never bundles.
- **Retrieval is on demand only.** The MCP tools answer when a client calls them. There is still no session-start hook.
- **No cloud deploy in this plan.** It ships a container image. Running that image somewhere is a later choice.

## Decisions from the interview

| Question | Decision |
| --- | --- |
| Auth | Static bearer tokens per person, minted by a server command and stored hashed in `memories.sqlite`. Each token has a scope: `read`, `write`, or `admin`. Auth is off only in the local compose file, which publishes the port on `127.0.0.1` alone. |
| Identity with auth on | `person` always comes from the token. `org` and `team` default to the token's values. The review form may override them, but only with values listed on that token. With auth off, the form and `~/.config/agent-memory/identity` work as they do today. |
| Offline Approve | Approve sends the document. If the server can't be reached, it writes the document to a local outbox and deletes the bundle. The next command that reaches the server sends the outbox, and so does `agent-memory sync`. A git-hook `auto_approve` falls back to the outbox in the same way. |
| Endpoint shape | One URL and one port. `/mcp` is for agents and `/v1/…` is for the CLI. See [One endpoint?](#one-endpoint). |
| Per-repo mode | Kept. A remote id now maps to a server URL and token, where it used to map to a Qdrant URL and a SQLite file. |
| Embedding providers | Only the interface and the fastembed adapter ship. A doc explains how an OpenAI or Bedrock adapter would plug in. Any other value in the embedder config stops the server at startup with a clear error. |
| stdio MCP branch (`claude/angry-ptolemy-335739`, `16a1f2c`) | It becomes the MCP part of the server, in PR 3, and is not merged as stdio. See [The stdio branch](#the-stdio-branch). |

### One endpoint?

For a client it already is one endpoint: one base URL such as `http://127.0.0.1:8765`, and one port. With auth on, the CLI holds a write token and each editor holds a read token minted for it. The paths differ because the two callers must not share a protocol:

- Anything exposed on `/mcp` is a tool that an agent can call by itself. A write tool there would let an agent save a memory that no person approved.
- So writes live under `/v1`, which only the CLI's review flow calls.

A read-scope token can call `/mcp` and the read routes under `/v1`. A write call needs the `write` scope.

## Target shape

```
laptop                                          Docker (compose.yaml)
------                                          ---------------------
editor hooks, git hooks, land                   agent-memory-server  :8765 (the only published port)
  -> inbox/ (bundles, transcripts stay here)      /mcp        search, get, list   (read)
agent-memory review (127.0.0.1:4173)              /v1/...     write API           (write, admin)
  Approve / Reject  --HTTP(S) + bearer-->        owns /data/memories.sqlite (source of truth)
  offline -> outbox/ --sync-->                    embeds on write with the configured embedder
editors (Claude Code, Cursor, Codex)            qdrant  (internal network only, no ports)
  MCP over streamable HTTP --> /mcp               /qdrant/storage
```

In a clone, `./data/server` holds the server's files and `./data/qdrant` holds Qdrant's. Both are bind-mounted volumes and both stay gitignored. Locally the link is plain HTTP on loopback. A team server gets HTTPS from its reverse proxy.

## 1. Server

### Process

- It is one Python 3.12 process, started by `agent-memory-server serve`.
- It uses the MCP Python SDK (`mcp==2.3.0`, the version pinned on the stdio branch). The SDK's streamable-HTTP app is mounted at `/mcp`. The `/v1` routes are plain Starlette routes in the same app, which the SDK already depends on. PR 3 checks that the mount doesn't double the path, and that the SDK's lifespan runs inside the combined app.
- uvicorn serves it on `0.0.0.0:8765` inside the container. Compose decides where that port is published.
- The image bakes in the pinned bge-small ONNX checkpoint at `/opt/agent-memory/models`. That is outside `/data`, so the volume mount can't hide it, and the server never downloads a model at runtime.
- `serve` holds an exclusive lock on `/data/server.lock`. `reindex` takes the same lock, so it can't run while the server is up and race the retry loop.

### Storage

- The server owns `/data/memories.sqlite`. The tables `memory`, `memory_commit`, and `rejected_commit` are unchanged.
- Schema v3 adds:
  - `token`: `id`, `hash` (sha256), `parent_id`, `label`, `person`, `org`, `team`, `allowed_orgs`, `allowed_teams`, `scopes`, `created_at`, `revoked_at`.
  - `embedding_state`, a single row for the active model: `provider`, `model`, `revision`, `dimension`, `distance`, `collection`, `activated_at`.
  - `memory_embedding`: `memory_id`, `collection`, `updated_at`. One row per memory per collection it is embedded in. A memory is pending for a collection when it has no row there, or when that row's `updated_at` is older than the memory's. This replaces `memory.embedded_at` on the server, so each collection knows what it is missing, and a switch back to an older model embeds only what that collection lacks.
  - `memory.written_by`: the token id, or `local` when auth is off.
- The code works against a path rather than `~/.config`. PR 2a splits `memory_store.py` into a core that takes a database path. The laptop wrapper uses that core until the cutover, and then only `migrate` uses it.
- `flushed_at` and `flush()` are retired at the cutover. The column stays, unused, so v2 files still open.

### Write path

1. `POST /v1/memories` checks the fields with the same rules `approve()` uses today. PR 2a moves those rules into a shared module, so the CLI and the server can't drift apart.
2. It fills `person`, `org`, and `team` from the token according to the identity rule above. It builds the id, `<person>-<kind>-<session_id>`.
3. It writes the row in SQLite and commits:
   - **No row with that id:** insert, then 201.
   - **Same id and same content:** nothing changes, then 200. This is an outbox resend.
   - **Same id and different content:** 409, with the stored text in the response. The API never edits a kept memory. The review page shows the conflict and keeps the bundle, and a newer note replaces an old one through `supersedes`, as today.
4. It embeds every row that is pending for the active collection, which includes an old row whose `superseded_by` just changed. It upserts those points into Qdrant and writes their `memory_embedding` rows.
5. If step 4 fails, for example because Qdrant is down, the row is still stored. The response says `embedded: false`, and a retry loop embeds pending rows every 30 seconds and at startup.

At about 10 ms per text with bge-small, embedding inside the request costs nothing worth a queue.

### Routes

| Route | Scope | Purpose |
| --- | --- | --- |
| `GET /v1/health` | none | Liveness, Qdrant readiness, and the embedder state. Compose and `setup` wait on it. |
| `GET /v1/whoami` | any token | The token's person, org, team, allowed orgs and teams, and scopes, plus whether auth is on and the active embedder. The review form fills its defaults from this. |
| `POST /mcp` (and `GET` for the stream) | read | MCP tools `search`, `get`, `list`. |
| `GET /v1/memories?org&team&repo&since&limit` | read | Backs `agent-memory list`. |
| `GET /v1/memories/{id}` | read | One row with its commits. |
| `POST /v1/memories` | write | Approve. Returns `{id, updated_at, embedded}`: 201 when new, 200 for an identical resend, 409 for different content under an existing id. |
| `POST /v1/rejections` `{repo, shas}` | write | Reject remembers commits, as it does today. |
| `POST /v1/commits/status` `{repo, shas}` | read | For each sha: `{kept, rejected}`. It matches abbreviated hashes both ways, as `_commit_in` does today. `land` sends a batch in one call. |
| `POST /v1/tokens` `{label}` | read and write | Mints a read-scope token for the caller's own person, as a child of the caller's token. A second call with the same label revokes the earlier child. Revoking a parent revokes its children. `connect --editor` uses it. |
| `POST /v1/admin/import` | admin | Bulk upsert of full rows that keep their original `id`, `person`, and `updated_at`, plus `rejected_commit` rows. A newer `updated_at` wins. Used by `agent-memory migrate`. |

The server also has commands:

```
agent-memory-server token create --person miguel --org acme --team platform \
  --scopes read,write [--allow-team infra] [--label laptop]
agent-memory-server token list
agent-memory-server token revoke <id>
agent-memory-server reindex [--rebuild] [--prune [--yes]]
```

You run `token` with `docker compose exec server …`, and `reindex` with `docker compose run --rm server …` while the server is stopped (see [Switching models](#switching-models)). `token create` prints the secret once, as `am_…`. It refuses a `--person` that already has active tokens unless you pass `--same-person`, so two people can't share a name by accident. Tokens are never managed over HTTP, except for the read-only child token that `POST /v1/tokens` mints.

### Requests from browsers

A port on `127.0.0.1` is still reachable from any web page the user opens. A page can send a simple cross-site `POST` with no preflight, and DNS rebinding can make a page's own origin resolve to `127.0.0.1`. With auth off, either could save a memory nobody approved, which breaks "no automatic save". It could also reject commits or run an import. So the server applies these rules to every route, whether auth is on or off:

- **JSON only.** A `POST` without `Content-Type: application/json` gets a 415. A browser can't send that header cross-site without a preflight, and the server answers no preflight.
- **No browser origins.** A request with an `Origin` header gets a 403 unless that origin is in `AGENT_MEMORY_ALLOWED_ORIGINS`, which is empty by default. The CLI and the editors send no `Origin`. The review page at `127.0.0.1:4173` calls the server from its Python process, not from the browser, so it doesn't need one either.
- **Known hosts only.** A request whose `Host` header isn't in `AGENT_MEMORY_ALLOWED_HOSTS` gets a 421. The default is `127.0.0.1:8765` and `localhost:8765`, and a team setup adds its own name.
- **Explicit protection for `/mcp`.** The SDK turns on its DNS-rebinding protection by itself only when it binds to a loopback host. The server binds `0.0.0.0` inside the container, so it passes `TransportSecuritySettings` explicitly, with the same hosts and origins.

PR 2b builds these rules and tests each one.

### Auth off

- Auth is off only when `AGENT_MEMORY_AUTH=off`. The local `compose.yaml` sets it and publishes `127.0.0.1:8765:8765` only.
- With auth off, `person`, `org`, and `team` come from the request body, which means the laptop's identity file, as today. Every caller has every scope, admin included, so the browser rules above are what keep web pages out.
- The server logs a warning at startup when auth is off.
- The team setup in PR 7, `compose.team.yaml`, sets auth on.
- TLS is out of scope. A team setup puts the server behind its existing reverse proxy.

### Qdrant

- The `ports:` block goes away, and so does the `./data/shared:/agent-memory` mount. Qdrant is reachable only as `http://qdrant:6333` on the compose network.
- Setting `QDRANT__SERVICE__API_KEY` from a `.env` file is an optional extra layer, documented but not required.
- Until the cutover in PR 5, Qdrant keeps its loopback port, so the laptop's `scripts/embed` keeps working. The comment in `compose.yaml` says that the port is temporary.

## 2. Pluggable embedding model

### Interface

The interface lives in `scripts/embedder.py`:

```python
class Embedder(Protocol):
    provider: str      # "fastembed"
    model: str         # "BAAI/bge-small-en-v1.5"
    revision: str      # "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
    dimension: int     # 384
    distance: str      # "Cosine"
    def load(self) -> None: ...
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...

# A class in PROVIDERS is built as cls(models_dir, backend=None).
def load_embedder(config, models_dir, backend=None) -> Embedder
```

- `FastEmbedBge` is the only adapter. It moves the existing code out of `embed.py`, including `load_model()` from the stdio branch. `embed_query` adds the BGE query prefix, as [embedding-model.md](embedding-model.md) describes, and `embed_documents` does not. fastembed 0.9.1's own `query_embed` adds no prefix for this model, so the stdio branch searched without one.
- Two environment variables choose the embedder: `AGENT_MEMORY_EMBEDDER` (default `fastembed`) and `AGENT_MEMORY_EMBED_MODEL` (default `BAAI/bge-small-en-v1.5`).
- Any other provider exits at startup with a message along these lines: "embedder 'openai' is not implemented. See docs/embedding-model.md, Adding a provider."

### One model per collection

- Each model gets its own collection, named `memories-<provider>-<dimension>-<hash8>`. `hash8` is the first 8 hex digits of the sha256 of `provider|model|revision|dimension`. For the default that is `memories-fastembed-384-2d705fdd`. The hash covers revisions that aren't git SHAs, such as `amazon.titan-embed-text-v2:0`, and two sizes of one model get two names.
- The `embedding_state` row in SQLite records the active collection and its provider, model, revision, and dimension. SQLite is the source of truth for this, just as it is for memories.
- Qdrant has had collection metadata since v1.16, so the server writes the same record into the collection's metadata as well. A collection then says what it holds, even to someone looking at it without the SQLite file.
- At startup, the server compares the configured embedder with `embedding_state`, and that state with the live collection's vector size. On any mismatch it refuses to start, and names the command that fixes it.
- Every point's payload keeps `embedding_model` and `embedding_revision`, and adds `embedding_provider`.

### Switching models

1. Stop the server: `docker compose stop server`.
2. Set the new embedder in `.env`.
3. Run `docker compose run --rm server agent-memory-server reindex`. It takes the server lock, creates or opens the collection for the configured embedder, and embeds every row that is pending for that collection. It checks that the point count matches the row count, then switches `embedding_state` over. `--rebuild` drops that collection first and embeds everything again.
4. Start the server: `docker compose start server`.
5. **Switching back** to an earlier model is the same three steps. `reindex` embeds only the rows that the older collection is missing, because `memory_embedding` is kept per collection.
6. **Prune** with `reindex --prune --yes`. It lists, then deletes, every collection whose name starts with `memories` and isn't the active one. That includes the legacy `memories` collection from before the server. Without `--yes`, it only lists them.

For 86 rows this takes seconds.

### Wording changes

The legacy collection `memories` is retired at the cutover. This is the "a second collection is for a different embedding model only" rule from [context.md](context.md). [embedding-model.md](embedding-model.md) gets a new section, "Adding a provider", and its line "A hosted embeddings API is a different vector space. It is not a second run mode for this index." is reworded to "A hosted model is a different vector space, so it gets its own collection and a full reindex."

The `AGENTS.md` wording changes in steps, so it never describes code that isn't there yet:

- **PR 1:** the model sentence and the two model bullets, with `scripts/embed --rebuild` as the way to switch.
- **PR 3:** the MCP write-tool bullet and the retrieval bullet.
- **PR 5:** the server sentence, and `reindex` in place of `scripts/embed --rebuild`.

The final opening paragraph:

> This repo is a brief, not a product yet. The vector engine is Qdrant server, which only the agent-memory server container in `compose.yaml` can reach. The default embedding model is `BAAI/bge-small-en-v1.5` through fastembed at a pinned revision, run inside that server. Other models plug in through `scripts/embedder.py`. See [docs/vector-engine.md](docs/vector-engine.md), [docs/embedding-model.md](docs/embedding-model.md), and [docs/server-plan.md](docs/server-plan.md).

Under **Do not**, this bullet is removed:

> Swap the vector engine or the embedding model. They are Qdrant server and `BAAI/bge-small-en-v1.5`. A different model means deleting the collection and embedding again.

These bullets replace it:

> - Swap the vector engine. It is Qdrant server.
> - Change the default embedding model or its pinned revision, or implement a hosted adapter (OpenAI, Bedrock), without a plan PR first. A deployment runs one embedder. Each collection records its provider, model, revision, and dimension, and the server refuses to start on a mismatch. Switching means `agent-memory-server reindex`, which embeds every row of `memories.sqlite` that is missing from that model's collection.
> - Add a write tool to the MCP server. Writes go through the authenticated `/v1` API, which only the review flow calls.
> - Build automatic retrieval: no session-start hook that injects memories into a session. Query is on demand only, through the server's read-only MCP tools.

The last bullet replaces "Build retrieval…". It carries over the wording from the stdio branch.

## 3. Clients

### Config in `~/.config/agent-memory/`

| File | Contents |
| --- | --- |
| `server` | `url = http://127.0.0.1:8765`, `token = am_…` (optional), and `mode = shared` or `per-repo`. Written with mode 0600. |
| `servers` | Per-repo mode only: lines like `github.com/acme/api = https://memory.acme.internal`. |
| `tokens` | Per-repo mode only: `<server url> = am_…`. Mode 0600. |
| `identity` | Unchanged. It is used when auth is off, and as a hint when it is on. The token always wins for `person`. |
| `database`, `databases` | Legacy. `connect` and `migrate` read them. After the cutover the CLI ignores them and prints one warning. |

### The CLI

- `scripts/server_client.py` talks to the server with the standard library's `urllib` only. The CLI keeps no third-party dependencies, which respects the `AGENTS.md` rule against new capture or review dependencies.
- Calls have short timeouts: 2 seconds in hooks and 10 seconds elsewhere.

| Command | After the cutover |
| --- | --- |
| `review`, Approve | `POST /v1/memories`. On a connection error or a 5xx, the document goes to the outbox, the bundle is deleted, and the page says "queued, N waiting". On a 4xx, the page shows the error and keeps the bundle. For a 409 it also shows the stored text, and for a 401 or 403 it says to run `agent-memory connect` again. |
| `review`, Reject | `POST /v1/rejections`, with the outbox as fallback, then the bundle is deleted. |
| Review page | It fills the org and team defaults, and the allowed values, from `/v1/whoami`. The `person` field is read-only when auth is on. A banner shows whether the server is connected, offline, or has queued items. Items that failed for good are listed with their error. |
| `land`, capture's "already settled" checks | `POST /v1/commits/status`, combined with what is waiting in the outbox. If the server can't be reached, they offer the commit anyway. A duplicate costs one Reject, while a missed memory is lost. |
| `git-hook` | It writes locally first, as `context.md` requires of hooks. With `auto_approve` on, the approved document goes straight into the outbox. Without it, a bundle goes into the inbox, as today. Then the hook makes one `commits/status` call with a 1-second timeout, to skip commits that are already settled. On any failure it files the bundle anyway. Last, it starts `agent-memory sync` as a detached background process and returns. The hook does no other network work, so it finishes within 2 seconds even when the server doesn't answer. |
| `list` | `GET /v1/memories`. |
| `sync` (new) | Sends the outbox in order. A lock file makes sure only one `sync` runs at a time. It runs at the start of `review`, `land` and `list`, and detached after `git-hook`. See [The outbox](#the-outbox) for what each response does. `sync --retry-failed` moves `outbox/failed/` back into the queue. |
| `flush` | Removed. For one release it prints "use `agent-memory sync`". |
| `connect` (new) | See below. |
| `migrate` (new) | See [Moving the existing store](#moving-the-existing-store). |
| `scripts/embed` | Approve embeds now, so plain `scripts/embed` only says so. With `--rebuild` or `--prune`, it runs `docker compose stop server`, then `docker compose run --rm server agent-memory-server reindex` with those flags, then `docker compose start server`. Other flags, `--repo` among them, are refused. One release later it is removed. |

### The outbox

- Each item is one JSON file in `outbox/`, next to `inbox/`, named `<utc stamp>-<uuid>.json`.
- It holds `{server_url, op: "memory" | "rejection", body, created_at, attempts, last_error}`.
- The `body` is the request exactly as it would have been sent: approved text and tags. There are no transcript paths, because the request validator has none to send.
- Files are written with mode 0600. `outbox/` is gitignored.
- What `sync` does with each response:
  - **2xx:** delete the item.
  - **Connection error or 5xx:** stop at the first one, keep the item and everything after it, and record `attempts` and `last_error`. The order is kept, and a server that's down costs one timeout, not one per item.
  - **401 or 403:** stop and keep the whole queue. A revoked or rotated token is fixed with `agent-memory connect`, and nothing approved is set aside.
  - **409:** move the item to `outbox/failed/`, with the stored text alongside. The review page lists it.
  - **Any other 4xx:** move the item to `outbox/failed/` with the error. The review page lists it, and `sync --retry-failed` sends it again after a fix.
- The review banner shows the oldest item's age, so a queue that has been stuck for days is visible.

### `agent-memory connect`

```
agent-memory connect --url https://memory.acme.internal --token-stdin
agent-memory connect --url http://127.0.0.1:8765                # local, auth off
agent-memory connect --editor claude-code,cursor,codex [--print]
```

- `connect` calls `/v1/whoami`, then writes `server` and prints who you are connected as.
- `--editor` mints a read-scope token through `POST /v1/tokens`, with the label `editor-<tool>`, so editor configs never hold the write token. Running it again replaces that editor's token instead of adding one. It then prints the lines below. With a yes at a prompt (or `--yes`), it writes them instead. That is the same consent pattern `install` uses.
- The commands to run, or the files to change. Each holds the read token as a literal value:
  - **Claude Code:** `claude mcp add --transport http --scope user --header "Authorization: Bearer <read token>" agent-memory <url>/mcp`. User scope is stored in `~/.claude.json`.
  - **Cursor:** `~/.cursor/mcp.json` → `{"mcpServers": {"agent-memory": {"url": "<url>/mcp", "headers": {"Authorization": "Bearer <read token>"}}}}`
  - **Codex:** `~/.codex/config.toml` → `[mcp_servers.agent-memory]` with `url = "<url>/mcp"` and `http_headers = { "Authorization" = "Bearer <read token>" }`. `bearer_token_env_var` would need the variable set wherever Codex is launched, which a desktop app doesn't inherit.
  - **Pi:** no MCP registration. Pi keeps capture through its extension.
  - PR 6 checks every key name and flag order above against each tool's current docs.
- With auth off, the header lines are left out.
- **Per-repo mode:** an editor registers MCP servers, not repos. `connect --editor` registers one entry for each distinct server URL in `server` and `servers`. The default server is named `agent-memory`, and each other server is named `agent-memory-<host>`. An agent picks the server whose repos it is asking about. The MCP instructions text says which repos each server holds.
- **This repo's own `.mcp.json`:** Claude Code uses the first match in this order: local, then project, then user, and it takes that entry whole. A project entry named `agent-memory` would hide a user's team server whenever they work in this repo. So the project entry that PR 3 adds is named `agent-memory-dev`, and it points at the local server.
- **Tests:** PR 6's tests run with a temporary `HOME`. They never touch the real `~/.cursor`, `~/.codex` or `~/.claude.json`, as `AGENTS.md` requires.

A caveat, written down so it isn't oversold: a token proves which user is calling, not that a person was in the loop. An agent with shell access runs as you, so it can read `~/.config/agent-memory/server`. These rules are what enforce review:

- MCP has no write tool.
- Editors hold a read-scope token.
- Only the review page, and `sync` sending what Approve or the git-hook `auto_approve` you turned on yourself queued, call `/v1/memories`.

### Packaging

- **CLI:** a `pyproject.toml` and `uv.lock` arrive in PR 2a, because the server's image and CI need them. From PR 7 they let you run `uv tool install git+https://github.com/roune/agent-memory`.
  - The `agent-memory` package has no runtime dependencies.
  - A `[server]` extra adds `mcp==2.3.0` and `fastembed==0.9.1`.
  - Both entry points are always installed, because an extra can't decide which console scripts get installed. So `agent-memory-server` checks first that `mcp` and `fastembed` can be imported. If they can't, it exits with: "this command needs the server extra: `uv tool install 'agent-memory[server] @ git+https://github.com/roune/agent-memory'`".
  - Modules stay in `scripts/` (`package-dir = {"" = "scripts"}`), so the test command does not change.
  - `./scripts/install` becomes `uv tool install --editable .` for a clone in PR 7.
- **Paths:** a tool install has no clone, so the laptop's own state moves to `${XDG_DATA_HOME:-~/.local/share}/agent-memory/`: `inbox/`, `outbox/`, `review.json`, `capture.log`. `AGENT_MEMORY_DIR` still overrides that location.
  - The first run after the upgrade moves the existing `inbox/` and the other files there.
  - `agent-memory install` rewrites the hook entries it marked to point at the installed `agent-memory`. Today they point at `<clone>/bin/agent-memory`.
- **Server:** a `Dockerfile` (added in PR 2b) builds from `python:3.12-slim` with `uv sync --frozen --extra server`.
  - It bakes in the model at the pinned revision, under `/opt/agent-memory/models`.
  - It exposes 8765. `python:3.12-slim` has no `curl`, so the `HEALTHCHECK` is a one-line Python `urllib` call to `/v1/health`.
  - It runs as a non-root user. Compose sets `user: "${AGENT_MEMORY_UID:-1000}:${AGENT_MEMORY_GID:-1000}"`, and `scripts/setup` writes the host user's ids into `.env` (gitignored). `setup` also creates `data/server` before it starts anything. On Linux, Docker would otherwise create a missing bind directory owned by root, and the server couldn't create `memories.sqlite`.
  - A GitHub Actions workflow (added in PR 7) builds `ghcr.io/roune/agent-memory-server:<version>` for amd64 and arm64 on every `v*` tag. A package that GHCR creates on first publish is private. So PR 7's release steps include setting it to public once, in the package settings, before the no-clone setup can pull it.
  - `compose.yaml` uses that image, and keeps `build: .` for development in a clone.

## Local single-user setup

The setup stays as short as today's: `./scripts/install`, then `agent-memory install`, then `./scripts/setup`.

```
git clone … && cd agent-memory
./scripts/install            # CLI on PATH (uv tool install --editable . after PR 7)
./scripts/setup              # starts qdrant and the server, waits for /v1/health, and writes
                             # ~/.config/agent-memory/server with url=http://127.0.0.1:8765 if it is missing
agent-memory install         # editor and git hooks, unchanged
agent-memory connect --editor claude-code   # optional: MCP in the editor
```

There is no `scripts/embed` step any more: Approve embeds. Auth is off, and nothing but loopback can reach the server.

Without a clone, after PR 7, the setup is:

1. `uv tool install` the CLI.
2. Download `compose.yaml` from the release.
3. Run `docker compose up -d` in the folder that holds it. Its `./data` is the store.

## Moving the existing store

Today's store:

- 86 memories, all embedded and none flushed.
- 2 repos and 1 person.
- 2 rejected commits.
- Shared mode: there is no `database` file.
- `data/shared/` is empty.

The move happens in PR 4 and PR 5:

1. **Back up:** `sqlite3 data/memories.sqlite ".backup data/backups/memories-pre-server.sqlite"`
2. Start the server next to Qdrant. Before PR 5 that is `docker compose --profile server up -d`, and from PR 5 on it is `./scripts/setup`. The server gets its own `data/server/memories.sqlite`.
3. `agent-memory connect --url http://127.0.0.1:8765`
4. `agent-memory migrate --dry-run` prints the counts: 86 memories, 2 rejected commits, the repos, and the people.
5. `agent-memory migrate` sends the rows and rejections to `/v1/admin/import` in batches of 50. With auth off locally, admin is implied. A team server needs an admin token. The server keeps each `id`, `person`, and `updated_at`, and embeds the rows into the new model-named collection.
6. **Verify:** `migrate` reads back the ids and counts and prints any difference. Then check by hand:
   - `migrate` prints the server's own count: 86.
   - `agent-memory list --limit 200` shows 86 rows. The plain list stops at its default limit.
   - An MCP `search` returns hits.
   - `/v1/commits/status` reports both rejected shas as rejected.
7. **Rerun after the cutover:** `migrate` is idempotent, because a newer `updated_at` wins. Run it once more after upgrading to PR 5, so anything approved between PR 4 and PR 5 is copied too.
8. **Clean up:** `scripts/embed --prune` runs `reindex --prune --yes --include-legacy` with the server stopped, which drops the legacy `memories` collection. The laptop's `data/memories.sqlite` is left in place as a backup. The CLI no longer opens it, apart from `migrate`.

Per-repo mode works the same way. Each local file under `~/.local/share/agent-memory/databases/<remote>/` goes to the server that `servers` maps that remote to.

## The stdio branch

**Decision:** reshape `16a1f2c` into PR 3, and don't merge stdio first. Shipping stdio would add a `.mcp.json` entry and a `scripts/mcp` launcher that PR 3 would remove a week later. Its code also reads the laptop's SQLite file, which stops being the source of truth in PR 5.

What carries over:

- **Into PR 1:** `embed.load_model()` and the move of the checkpoint path under `store_root()`.
- **Into PR 2a:** `memory_store.get_memory()`.
- **Into PR 3:** `build_filter`, `search_body`, `result_from_point`, `clamp_limit`, the `INSTRUCTIONS` text, and most of `test_mcp_server.py`.

What is dropped:

- `scripts/mcp`
- the stdio `.mcp.json`
- `remote_for`: a server is one store, and per-repo routing now happens in the client.

When PR 3 merges, the branch and its worktree (`.claude/worktrees/angry-ptolemy-335739`) can be deleted. That is Miguel's call.

Separately, `/Users/miguel/projects/agent-memory-mcp` (branch `cursor/loopback-mcp-1134`) has an older MCP draft that was never committed, on a base from before schema v2. This plan supersedes it. It is left alone here.

## PRs, in order

Each PR is one piece. PR 2 is split into 2a, 2b and 2c, so no single PR holds the whole server. The numbers of the later PRs stay as they were.

Server tests that need Starlette or the MCP SDK are skipped under the system `python3`, with `skipUnless(importlib.util.find_spec(...))`. `scripts/test-server` runs the whole suite under `uv run --extra server`, so nothing is skipped there. CI runs the stdlib suite from PR 2a on, and both commands from PR 2b on.

Test doubles:

- a deterministic `HashEmbedder`, with dimension 8
- an injectable Qdrant transport, so no test needs Docker or the model

| PR | Needs |
| --- | --- |
| 1 Embedder interface | nothing |
| 2a Store core and packaging skeleton | 1 |
| 2b Server and write API, auth off | 2a |
| 2c Tokens and auth | 2b |
| 3 MCP over streamable HTTP | 2b (2c for auth on `/mcp`) |
| 4 Client and `migrate` | 2b |
| 5 Cutover | 4 and 2c |
| 6 Editors | 4 and 3 |
| 7 Packaging and image | all of them |

### PR 0: This plan

- **Scope:** `docs/server-plan.md`, plus links to it from the README's "Where to read next" and from `AGENTS.md`.
- **Tests:** none.
- **Migration:** none.

### PR 1: Embedder interface

- **Scope:**
  - `scripts/embedder.py`: the `Embedder` protocol, the `FastEmbedBge` adapter, and `load_embedder()`, which rejects unknown providers.
  - `embed.py` uses the interface and checks vector lengths before `--rebuild` deletes anything.
  - `--rebuild` refuses `--repo` in shared mode, and refuses a Qdrant URL shared with another remote in per-repo mode. Both would delete points that it doesn't embed again.
  - The `load_model` refactor from `16a1f2c`.
  - The model parts of the `AGENTS.md` wording above.
  - `embedding-model.md`: the new "Choosing the embedder" and "Adding a provider" sections, and the reworded hosted-model line.
- **Tests:**
  - unknown provider → a clear error
  - `embed_query` adds the BGE prefix, and `embed_documents` doesn't
  - `embed.py` checks the dimension against `embedder.dimension`
  - `main(["--rebuild"])` with vectors of the wrong length sends no DELETE
  - the `--rebuild` refusals
  - tests ignore `AGENT_MEMORY_EMBEDDER` and `AGENT_MEMORY_EMBED_MODEL` in the developer's shell
  - the existing embed tests stay green
- **Migration:** none. Vectors are unchanged: same model, same revision, same collection.

### PR 2a: Store core and packaging skeleton

- **Scope:**
  - A path-based core split out of `memory_store.py`. The laptop functions become thin wrappers that resolve the path from `~/.config`, so laptop behaviour doesn't change.
  - The validation rules of `approve()` move into a shared module that the CLI and the server both import.
  - `memory_store.get_memory()`, from the stdio branch.
  - `pyproject.toml` and `uv.lock`: the `agent-memory` package with no runtime dependencies, the `[server]` extra, and both entry points. `agent-memory-server` only checks its imports and prints the install hint for now.
  - A CI workflow that runs the stdlib suite.
- **Tests:**
  - The core works on a temporary path without `HOME` or `AGENT_MEMORY_DIR`.
  - Every `approve()` rule gets the same answer from the shared module.
  - The existing CLI and store tests stay green, unchanged.
  - `agent-memory-server` without the extra exits with the install hint.
- **Migration:** none.

### PR 2b: Server and write API, auth off

- **Scope:**
  - `scripts/agent_memory_server/` (a package, so the installed top-level name can't collide with another `server`): the Starlette app with `/v1/health`, `/v1/whoami` (which reports auth off), the `/v1/memories` GET and POST routes, `/v1/rejections`, `/v1/commits/status` and `/v1/admin/import`.
  - [The browser rules](#requests-from-browsers): JSON only, `Origin` and `Host` allowlists, and explicit `TransportSecuritySettings` ready for `/mcp`.
  - The write semantics: insert, identical no-op, or 409.
  - Embed-on-write and the retry loop.
  - Schema v3 without the `token` table: `embedding_state`, `memory_embedding` and `written_by`.
  - The startup mismatch refusal, collection names and metadata, the server lock, and `reindex [--rebuild] [--prune [--yes]]`.
  - The `Dockerfile`. A `server` service in `compose.yaml` under the compose profile `server`, published on `127.0.0.1:8765`, with auth off, the host user's ids, and `data/server` mounted at `/data`. Because of the profile, the bare `docker compose up -d` in `scripts/setup` doesn't start it before the cutover. `scripts/setup` creates `data/server` and writes `.env`.
  - Qdrant keeps its loopback port for now.
  - `scripts/test-server`, and CI runs it.
- **Tests:**
  - **Validation:** unknown keys and `transcript_path` are rejected.
  - **Browser rules:** a `text/plain` POST gets a 415, a foreign `Origin` a 403, and an unknown `Host` a 421, on `/v1` and on `/mcp`.
  - **Writes:** a new id gets a 201, an identical resend a 200, and different content a 409 that leaves the stored row unchanged.
  - **Supersedes:** the old row is updated and embedded again.
  - **Qdrant down:** the row is stored and `embedded: false` is returned. The retry loop embeds it later.
  - **Mismatch:** a changed dimension or model refuses startup.
  - **Reindex:** builds the configured collection, checks the count, and switches state. Switching back embeds only the missing rows. `--prune` lists without `--yes` and keeps the active collection. Reindex refuses to run while the server holds the lock.
  - **Collection names:** two dimensions of one model, and a Bedrock-style revision, give distinct names.
  - **Commit status:** abbreviated hashes match both ways.
  - **Import:** a newer `updated_at` wins.
  - **Image:** it builds, and `/v1/health` answers, in CI.
- **Migration:** none. The server starts empty, only when you ask for it, and the laptop flow is unchanged.

### PR 2c: Tokens and auth

- **Scope:**
  - The `token` table, hashed secrets, and scopes.
  - `token create`, `list` and `revoke`. Revoking a token cascades to its children. `create` checks for an existing person.
  - Auth on by default: `AGENT_MEMORY_AUTH=off` only in the local `compose.yaml`.
  - Identity pinning: `person` comes from the token, and `org` and `team` only from the token's allowed lists.
  - `POST /v1/tokens` for labelled child read tokens.
  - `/v1/whoami` with the token's identity.
- **Tests:**
  - **Identity:** the token pins `person`. `org` and `team` overrides are allowed only from the token's lists. With auth off the body is used.
  - **Scopes:** `read` can't write, and `write` can't import.
  - **Tokens:** stored as hashes. A revoked token gets a 401, and so do its children. The same child label replaces the earlier token. A second token for an existing person needs `--same-person`.
- **Migration:** none.

### PR 3: MCP over streamable HTTP

- **Scope:**
  - `search`, `get`, and `list` at `/mcp`, reshaped from `16a1f2c`.
  - They read the server's SQLite and the active collection, and embed the query with `embed_query`.
  - Read scope is required when auth is on.
  - Superseded memories are returned too, with `superseded_by` as metadata. Nothing is hidden: a supersede may come from another org (PR 2c), so the reader sees both notes. Ranking by the caller's org is a later option.
  - Check that the SDK's app mounts cleanly at `/mcp`, with no doubled path, and that its lifespan runs.
  - `.mcp.json` → `{"mcpServers": {"agent-memory-dev": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}}`. The name keeps the project entry from hiding a user's team server.
  - The MCP `INSTRUCTIONS` text.
  - The `AGENTS.md` bullets on the MCP write tool and on retrieval.
- **Tests:**
  - the filter, body, and result-shape tests from the branch
  - the tool list contains no write tool, which is asserted by name
  - a Qdrant error becomes a tool error, not a crash
  - 401 without a token when auth is on
  - the browser rules on `/mcp`
- **Migration:** none. The tools return results after PR 4's `migrate`.

### PR 4: Client foundations and `migrate`

- **Scope:**
  - `scripts/server_client.py`, using only the standard library.
  - The `server`, `servers`, and `tokens` config files.
  - `agent-memory connect --url`, without editors.
  - `agent-memory migrate [--dry-run]`, which prints the server's count after the import.
  - `list` goes through the server when one is configured.
  - Approve and Reject still write locally.
- **Tests:**
  - parsing of the config files, including the per-repo mapping
  - migrate against a stub server: 86 rows in a fixture file arrive with ids, people, and timestamps intact; rejections arrive; a rerun is a no-op
  - the dry-run counts
  - `connect` writes mode 0600
  - the client sends `Content-Type: application/json` and no `Origin`
- **Migration:** steps 1 to 6 of [Moving the existing store](#moving-the-existing-store). Before PR 5, start the server with `docker compose --profile server up -d`.

### PR 5: Cutover

- **Scope:**
  - Approve, Reject and `land` go through the server.
  - `git-hook` writes to the outbox first, and starts a detached `sync`.
  - The outbox, `agent-memory sync` with its lock, and `sync --retry-failed`.
  - The review page uses `whoami` for its defaults and shows the banner, the queue age, and items that failed or got a 409.
  - `flush` is retired, and `scripts/embed` becomes the stop, reindex, start wrapper.
  - `compose.yaml` loses Qdrant's ports, the `./data/shared` mount, and the `server` profile, so `scripts/setup` starts both services. `setup` writes the `server` config if it is missing.
  - The `AGENTS.md` server sentence.
  - README and `integration.md` updated, plus the `context.md` wording amendment below, after Miguel approves it.
- **Tests:**
  - Approve while the server is up → POST, and the bundle is deleted.
  - Approve while it is down → an outbox file holding only the request body, and the bundle is deleted.
  - A 4xx → the bundle is kept. A 409 shows the stored text.
  - `sync`:
    - It sends in order and stops at the first connection error.
    - A 401 or 403 keeps the whole queue.
    - A 409 or another 4xx moves the item to `failed/`, and `--retry-failed` brings it back.
    - Two `sync` runs at once send each item once.
  - `land` skips a commit reported kept or rejected, and one that is waiting in the outbox.
  - `git-hook` with `auto_approve` writes the outbox before any network call, and finishes in 2 seconds against a server that doesn't answer.
  - `scripts/embed --repo` is refused.
  - Existing CLI tests run against a stub server.
- **Migration:**
  1. Upgrade.
  2. `./scripts/setup`.
  3. `agent-memory migrate`, once more.
  4. `scripts/embed --prune`, which drops the legacy `memories` collection.
  5. Check that `curl 127.0.0.1:6333` now fails.

### PR 6: Editors

- **Scope:**
  - `agent-memory connect --editor claude-code,cursor,codex [--print] [--yes]`.
  - Each editor gets a labelled read-scope token. Running it again replaces that token.
  - The lines it writes or prints, as above, after checking each tool's current docs.
  - Per-repo mode registers one entry per server.
  - `uninstall` removes the MCP entries it added.
- **Tests:** all with a temporary `HOME`.
  - It merges into existing `mcp.json`, `config.toml` and `~/.claude.json` entries without touching other servers.
  - `--print` writes nothing.
  - The token in the editor files is never the write token.
  - With auth off, no header is written.
  - Two servers in per-repo mode give two entries with distinct names.
  - `uninstall` removes only its own entries.
- **Migration:** run `agent-memory connect --editor …` once per laptop. `.mcp.json` in this repo keeps its `agent-memory-dev` entry.

### PR 7: Packaging and image

- **Scope:**
  - `uv tool install git+https://github.com/roune/agent-memory`, documented and tested. `pyproject.toml` already exists from PR 2a.
  - `./scripts/install` → `uv tool install --editable .`.
  - The laptop's state moves to the XDG data directory, with a one-time move.
  - `agent-memory install` points hooks at the installed binary.
  - The GHCR workflow, building amd64 and arm64 on `v*` tags. The release steps include making the package public once.
  - `compose.yaml` pinned to the image tag.
  - `compose.team.yaml` (auth on, team hosts in `AGENT_MEMORY_ALLOWED_HOSTS`) and `docs/deploy.md`.
- **Tests:**
  - The move: an inbox at the old path ends up at the new one, and a second run does nothing.
  - Hook commands are rewritten only where they carry the marker.
  - The wheel builds, and `agent-memory --help` runs from a fresh `uv tool install`, in CI.
  - The published image answers `/v1/health`, in CI.
- **Migration:**
  1. `uv tool install git+https://github.com/roune/agent-memory`
  2. `agent-memory install`, which repoints the hooks.
  3. The first command moves `inbox/` and the outbox.

## Wording `context.md` needs, for Miguel to approve

These lines describe file paths and steps. They change no decision.

| Where | Today | Proposed |
| --- | --- | --- |
| "How work becomes a memory", agent sessions | `setup and embed script -> vectors in data/` | `Approve -> agent-memory server, which embeds -> vectors in Qdrant` |
| Git hooks | `auto-approve into data/memories.sqlite` | `auto-approve to the server (outbox when offline)` |
| Review paragraph | `writes the row … in data/memories.sqlite` | `sends the document to the server, which writes the row in its memories.sqlite` |
| "The store" | "A setup script creates it in this repo's `data/` directory" | Unchanged for a clone. Add: "Without a clone, `data/` sits next to the `compose.yaml` you run." |
| "Hooks, once they exist" | "A sync step, later, pushes only approved documents when a remote is configured." | Add: "That step is `agent-memory sync` and the outbox." |

## Out of this plan

- OpenAI and Bedrock adapters. Only the interface and the doc ship.
- OIDC or JWT auth. Static tokens can be replaced later behind the same check of the `Authorization` header.
- TLS inside the server, and any cloud deploy.
- Editing or deleting a kept memory through the API. Supersede is the way to replace one, as today.
- A session-start retrieval hook.
- MCP registration for Pi.

## Risks to watch

- **Shared SQLite over a bind mount.** Don't let the laptop and the container open the same SQLite file. WAL's shared memory doesn't work across the Docker Desktop VM. This plan never shares the file: the server has its own file, and data moves only through `migrate`.
- **The outbox and `land`.** A memory waiting in the outbox counts as kept for `land` only on that laptop. Two laptops of the same person can offer the same commit twice until both have synced. That costs one Reject.
- **Auth-off misuse.** The server can't see how Docker publishes its port. Auth off relies on two things: the `127.0.0.1:` mapping in `compose.yaml`, which keeps other machines out, and [the browser rules](#requests-from-browsers), which keep web pages on this machine out. `docs/deploy.md` says so, and `compose.team.yaml` turns auth on.
- **Agents with a shell.** An agent runs as you, so it could read a write token, or call the loopback API with auth off. The browser rules don't stop that, and nothing here claims to. See the caveat in [`agent-memory connect`](#agent-memory-connect).
