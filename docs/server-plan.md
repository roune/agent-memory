# Server plan

Status: approved by Miguel, 21 November 2026. Nothing here is built yet. The `context.md` wording changes near the end still need their own yes before PR 5.

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
| Endpoint shape | One URL, one port, and one token. `/mcp` is for agents and `/v1/…` is for the CLI. See [One endpoint?](#one-endpoint). |
| Per-repo mode | Kept. A remote id now maps to a server URL and token, where it used to map to a Qdrant URL and a SQLite file. |
| Embedding providers | Only the interface and the fastembed adapter ship. A doc explains how an OpenAI or Bedrock adapter would plug in. Any other value in the embedder config stops the server at startup with a clear error. |
| stdio MCP branch (`claude/angry-ptolemy-335739`, `16a1f2c`) | It becomes the MCP part of the server, in PR 3, and is not merged as stdio. See [The stdio branch](#the-stdio-branch). |

### One endpoint?

For a client it already is one endpoint: one base URL such as `http://127.0.0.1:8765`, one port, and one token. The paths differ because the two callers must not share a protocol:

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
  Approve / Reject  --HTTPS + bearer-->           owns /data/memories.sqlite (source of truth)
  offline -> outbox/ --sync-->                    embeds on write with the configured embedder
editors (Claude Code, Cursor, Codex)            qdrant  (internal network only, no ports)
  MCP over streamable HTTP --> /mcp               /qdrant/storage
```

In a clone, `./data/server` holds the server's files and `./data/qdrant` holds Qdrant's. Both are bind-mounted volumes and both stay gitignored.

## 1. Server

### Process

- It is one Python 3.12 process, started by `agent-memory-server serve`.
- It uses the MCP Python SDK (`mcp==2.3.0`, the version pinned on the stdio branch). The SDK's streamable-HTTP app is mounted at `/mcp`. The `/v1` routes are plain Starlette routes in the same app, which the SDK already depends on.
- uvicorn serves it on `0.0.0.0:8765` inside the container. Compose decides where that port is published.
- The image bakes in the pinned bge-small ONNX checkpoint, so the server never downloads a model at runtime.

### Storage

- The server owns `/data/memories.sqlite`. The tables `memory`, `memory_commit`, and `rejected_commit` are unchanged.
- Schema v3 adds:
  - `token`: `id`, `hash` (sha256), `label`, `person`, `org`, `team`, `allowed_orgs`, `allowed_teams`, `scopes`, `created_at`, `revoked_at`.
  - `embedding_state`, a single row: `provider`, `model`, `revision`, `dimension`, `distance`, `collection`, `activated_at`.
  - `memory.written_by`: the token id, or `local` when auth is off.
- The code works against a path rather than `~/.config`. PR 2 splits `memory_store.py` into a core that takes a database path. The laptop wrapper uses that core until the cutover, and then only `migrate` uses it.
- `flushed_at` and `flush()` are retired at the cutover. The column stays, unused, so v2 files still open.

### Write path

1. `POST /v1/memories` checks the fields with the same rules `approve()` uses today. PR 2 moves those rules into a shared module, so the CLI and the server can't drift apart.
2. It fills `person`, `org`, and `team` from the token according to the identity rule above. It builds the id, `<person>-<kind>-<session_id>`.
3. It upserts the row in SQLite and commits.
4. It embeds every pending row, which includes an old row whose `superseded_by` just changed. It upserts those points into Qdrant and sets `embedded_at`.
5. If step 4 fails, for example because Qdrant is down, the row is still stored. The response says `embedded: false`, and a retry loop embeds pending rows every 30 seconds and at startup.

At about 10 ms per text with bge-small, embedding inside the request costs nothing worth a queue.

### Routes

| Route | Scope | Purpose |
| --- | --- | --- |
| `GET /v1/health` | none | Liveness, Qdrant readiness, and the embedder state. Compose and `setup` wait on it. |
| `GET /v1/whoami` | read | The token's person, org, team, allowed orgs and teams, and scopes, plus whether auth is on and the active embedder. The review form fills its defaults from this. |
| `POST /mcp` (and `GET` for the stream) | read | MCP tools `search`, `get`, `list`. |
| `GET /v1/memories?org&team&repo&since&limit` | read | Backs `agent-memory list`. |
| `GET /v1/memories/{id}` | read | One row with its commits. |
| `POST /v1/memories` | write | Approve. Returns 201 with `{id, updated_at, embedded}`. Idempotent by id: a resend from the outbox is harmless. |
| `POST /v1/rejections` `{repo, shas}` | write | Reject remembers commits, as it does today. |
| `POST /v1/commits/status` `{repo, shas}` | read | For each sha: `{kept, rejected}`. It matches abbreviated hashes both ways, as `_commit_in` does today. `land` sends a batch in one call. |
| `POST /v1/tokens` | write | Mints a read-scope token for the caller's own person. `connect --editor` uses it. |
| `POST /v1/admin/import` | admin | Bulk upsert of full rows that keep their original `id`, `person`, and `updated_at`, plus `rejected_commit` rows. A newer `updated_at` wins. Used by `agent-memory migrate`. |

The server also has commands:

```
agent-memory-server token create --person miguel --org acme --team platform \
  --scopes read,write [--allow-team infra] [--label laptop]
agent-memory-server token list
agent-memory-server token revoke <id>
agent-memory-server reindex [--rebuild] [--prune]
```

You run them with `docker compose exec server …`. `token create` prints the secret once, as `am_…`. Tokens are never managed over HTTP, except for the read-only child token that `POST /v1/tokens` mints.

### Auth off

- Auth is off only when `AGENT_MEMORY_AUTH=off`. The local `compose.yaml` sets it and publishes `127.0.0.1:8765:8765` only.
- With auth off, `person`, `org`, and `team` come from the request body, which means the laptop's identity file, as today.
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
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...

def load_embedder(config) -> Embedder
```

- `FastEmbedBge` is the only adapter. It moves the existing code out of `embed.py`, including `load_model()` from the stdio branch. `embed_query` adds the BGE query prefix, as [embedding-model.md](embedding-model.md) describes, and `embed_documents` does not.
- Two environment variables choose the embedder: `AGENT_MEMORY_EMBEDDER` (default `fastembed`) and `AGENT_MEMORY_EMBED_MODEL` (default `BAAI/bge-small-en-v1.5`).
- Any other provider exits at startup with a message along these lines: "embedder 'openai' is not implemented. See docs/embedding-model.md, Adding a provider."

### One model per collection

- Each model gets its own collection, named `memories-<provider>-<model-slug>-<rev7>`. For example: `memories-fastembed-bge-small-en-v1.5-aa8f8b0`.
- The `embedding_state` row in SQLite records the active collection and its provider, model, revision, and dimension. SQLite is the source of truth for this, just as it is for memories.
- PR 2 also checks whether Qdrant v1.20 takes collection-level metadata. If it does, the server writes the same record there.
- At startup, the server compares the configured embedder with `embedding_state`, and that state with the live collection's vector size. On any mismatch it refuses to start, and names the command that fixes it.
- Every point's payload keeps `embedding_model` and `embedding_revision`, and adds `embedding_provider`.

### Switching models

1. Stop the server.
2. Set the new embedder in `.env`.
3. Run `docker compose run --rm server agent-memory-server reindex --rebuild`. It embeds every row of `memories.sqlite` into the new collection, checks that the point count matches the row count, and switches `embedding_state` over.
4. Start the server.
5. Delete the old collection with `--prune`. Until then you can switch back.

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
> - Change the default embedding model or its pinned revision, or implement a hosted adapter (OpenAI, Bedrock), without a plan PR first. A deployment runs one embedder. Each collection records its provider, model, revision, and dimension, and the server refuses to start on a mismatch. Switching means `agent-memory-server reindex --rebuild`, which embeds every row of `memories.sqlite` into a new collection.
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
| `review`, Approve | `POST /v1/memories`. On a connection error or a 5xx, the document goes to the outbox, the bundle is deleted, and the page says "queued, N waiting". On a 4xx, the page shows the error and keeps the bundle. |
| `review`, Reject | `POST /v1/rejections`, with the outbox as fallback, then the bundle is deleted. |
| Review page | It fills the org and team defaults, and the allowed values, from `/v1/whoami`. The `person` field is read-only when auth is on. A banner shows whether the server is connected, offline, or has queued items. Items that failed for good are listed with their error. |
| `land`, `git-hook`, capture's "already settled" checks | `POST /v1/commits/status`, combined with what is waiting in the outbox. If the server can't be reached, they offer the commit anyway. A duplicate costs one Reject, while a missed memory is lost. |
| `git-hook` with `auto_approve` | Approves with a 2-second timeout and falls back to the outbox. The hook never blocks the commit. |
| `list` | `GET /v1/memories`. |
| `sync` (new) | Sends the outbox in order. A 4xx moves the item to `outbox/failed/`. It runs at the start of `review`, `land`, `list`, and `git-hook`. |
| `flush` | Removed. For one release it prints "use `agent-memory sync`". |
| `connect` (new) | See below. |
| `migrate` (new) | See [Moving the existing store](#moving-the-existing-store). |
| `scripts/embed` | Becomes `docker compose exec server agent-memory-server reindex "$@"` for one release, then is removed. |

### The outbox

- Each item is one JSON file in `outbox/`, next to `inbox/`, named `<utc stamp>-<uuid>.json`.
- It holds `{server_url, op: "memory" | "rejection", body, created_at, attempts, last_error}`.
- The `body` is the request exactly as it would have been sent: approved text and tags. There are no transcript paths, because the request validator has none to send.
- Files are written with mode 0600. `outbox/` is gitignored.

### `agent-memory connect`

```
agent-memory connect --url https://memory.acme.internal --token-stdin
agent-memory connect --url http://127.0.0.1:8765                # local, auth off
agent-memory connect --editor claude-code,cursor,codex [--print]
```

- `connect` calls `/v1/whoami`, then writes `server` and prints who you are connected as.
- `--editor` mints a read-scope token through `POST /v1/tokens`, so editor configs never hold the write token. It then prints the lines below. With a yes at a prompt (or `--yes`), it writes them instead. That is the same consent pattern `install` uses.
- The commands to run, or the files to change:
  - **Claude Code:** `claude mcp add --scope user --transport http agent-memory <url>/mcp --header "Authorization: Bearer <read token>"`
  - **Cursor:** `~/.cursor/mcp.json` → `{"mcpServers": {"agent-memory": {"url": "<url>/mcp", "headers": {"Authorization": "Bearer <read token>"}}}}`
  - **Codex:** `~/.codex/config.toml` → `[mcp_servers.agent-memory]` with `url = "<url>/mcp"` and `bearer_token_env_var = "AGENT_MEMORY_TOKEN"`. PR 6 checks these key names against current Codex docs.
  - **Pi:** no MCP registration. Pi keeps capture through its extension.
- With auth off, the header lines are left out.

A caveat, written down so it isn't oversold: a token proves which user is calling, not that a person was in the loop. An agent with shell access runs as you, so it can read `~/.config/agent-memory/server`. These rules are what enforce review:

- MCP has no write tool.
- Editors hold a read-scope token.
- Only the review page and the git-hook `auto_approve` you turned on yourself call `/v1/memories`.

### Packaging

- **CLI:** a `pyproject.toml` lets you run `uv tool install git+https://github.com/roune/agent-memory`.
  - The `agent-memory` package has no runtime dependencies.
  - A `[server]` extra adds `mcp==2.3.0` and `fastembed==0.9.1`, along with the `agent-memory-server` entry point.
  - Modules stay in `scripts/` (`package-dir = {"" = "scripts"}`), so the test command does not change.
  - `./scripts/install` becomes `uv tool install --editable .` for a clone.
- **Paths:** a tool install has no clone, so the laptop's own state moves to `${XDG_DATA_HOME:-~/.local/share}/agent-memory/`: `inbox/`, `outbox/`, `review.json`, `capture.log`. `AGENT_MEMORY_DIR` still overrides that location.
  - The first run after the upgrade moves the existing `inbox/` and the other files there.
  - `agent-memory install` rewrites the hook entries it marked to point at the installed `agent-memory`. Today they point at `<clone>/bin/agent-memory`.
- **Server:** a `Dockerfile` (added in PR 2) builds from `python:3.12-slim` with `uv sync --frozen --extra server`. It bakes in the model at the pinned revision, runs as a non-root user, exposes 8765, and has a `HEALTHCHECK` on `/v1/health`.
  - A GitHub Actions workflow (added in PR 7) builds `ghcr.io/roune/agent-memory-server:<version>` for amd64 and arm64 on every `v*` tag.
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
2. `./scripts/setup` starts the server next to Qdrant. The server gets its own `data/server/memories.sqlite`.
3. `agent-memory connect --url http://127.0.0.1:8765`
4. `agent-memory migrate --dry-run` prints the counts: 86 memories, 2 rejected commits, the repos, and the people.
5. `agent-memory migrate` sends the rows and rejections to `/v1/admin/import` in batches of 50. With auth off locally, admin is implied. A team server needs an admin token. The server keeps each `id`, `person`, and `updated_at`, and embeds the rows into the new model-named collection.
6. **Verify:** `migrate` reads back the ids and counts and prints any difference. Then check by hand:
   - `agent-memory list` shows 86.
   - An MCP `search` returns hits.
   - `/v1/commits/status` reports both rejected shas as rejected.
7. **Rerun after the cutover:** `migrate` is idempotent, because a newer `updated_at` wins. Run it once more after upgrading to PR 5, so anything approved between PR 4 and PR 5 is copied too.
8. **Clean up:** `agent-memory-server reindex --prune` drops the legacy `memories` collection. The laptop's `data/memories.sqlite` is left in place as a backup. The CLI no longer opens it, apart from `migrate`.

Per-repo mode works the same way. Each local file under `~/.local/share/agent-memory/databases/<remote>/` goes to the server that `servers` maps that remote to.

## The stdio branch

**Decision:** reshape `16a1f2c` into PR 3, and don't merge stdio first. Shipping stdio would add a `.mcp.json` entry and a `scripts/mcp` launcher that PR 3 would remove a week later. Its code also reads the laptop's SQLite file, which stops being the source of truth in PR 5.

What carries over:

- **Into PR 1:** `embed.load_model()` and the move of the checkpoint path under `store_root()`.
- **Into PR 2:** `memory_store.get_memory()`.
- **Into PR 3:** `build_filter`, `search_body`, `result_from_point`, `clamp_limit`, the `INSTRUCTIONS` text, and most of `test_mcp_server.py`.

What is dropped:

- `scripts/mcp`
- the stdio `.mcp.json`
- `remote_for`: a server is one store, and per-repo routing now happens in the client.

When PR 3 merges, the branch and its worktree (`.claude/worktrees/angry-ptolemy-335739`) can be deleted. That is Miguel's call.

Separately, `/Users/miguel/projects/agent-memory-mcp` (branch `cursor/loopback-mcp-1134`) has an older MCP draft that was never committed, on a base from before schema v2. This plan supersedes it. It is left alone here.

## PRs, in order

Each PR is one piece. Server tests that need Starlette or the MCP SDK are skipped under the system `python3`, with `skipUnless(importlib.util.find_spec(...))`. `scripts/test-server` runs the whole suite under `uv run --extra server`, so nothing is skipped there. From PR 2 on, CI runs both commands.

Test doubles:

- a deterministic `HashEmbedder`, with dimension 8
- an injectable Qdrant transport, so no test needs Docker or the model

### PR 0: This plan

- **Scope:** `docs/server-plan.md`, plus links to it from the README's "Where to read next" and from `AGENTS.md`.
- **Tests:** none.
- **Migration:** none.

### PR 1: Embedder interface

- **Scope:**
  - `scripts/embedder.py`: the `Embedder` protocol, the `FastEmbedBge` adapter, and `load_embedder()`, which rejects unknown providers.
  - `embed.py` uses the interface.
  - The `load_model` refactor from `16a1f2c`.
  - The model parts of the `AGENTS.md` wording above.
  - `embedding-model.md`: the new "Adding a provider" section and the reworded hosted-API line.
- **Tests:**
  - unknown provider → a clear error
  - `embed_query` adds the BGE prefix, and `embed_documents` doesn't
  - `embed.py` checks the dimension against `embedder.dimension`
  - the existing embed tests stay green
- **Migration:** none. Vectors are unchanged: same model, same revision, same collection.

### PR 2: Server core and write API

- **Scope:**
  - `scripts/server/`: the Starlette app, `/v1/health`, `/v1/whoami`, the `/v1/memories` GET and POST routes, `/v1/rejections`, `/v1/commits/status`, `/v1/tokens`, and `/v1/admin/import`.
  - Embed-on-write with the retry loop.
  - `embedding_state`, the startup mismatch refusal, and `reindex [--rebuild] [--prune]`.
  - Token commands and schema v3.
  - A path-based core split out of `memory_store.py`, with the validation shared with `approve()`.
  - The `Dockerfile`, a `server` service in `compose.yaml` on `127.0.0.1:8765` with auth off, and `data/server` mounted at `/data`.
  - Qdrant keeps its loopback port for now.
  - The CI workflow that runs the tests.
- **Tests:**
  - **Validation:** each `approve()` rule, plus the rejection of unknown keys and `transcript_path`.
  - **Identity:** the token pins `person`; `org` and `team` overrides are allowed only from the token's lists; with auth off the body is used.
  - **Scopes:** `read` can't write, and `write` can't import.
  - **Idempotency:** posting the same document twice leaves one row.
  - **Supersedes:** the old row is updated and embedded again.
  - **Qdrant down:** the row is stored and `embedded: false` is returned; the retry loop embeds it later.
  - **Mismatch:** a changed dimension or model refuses startup.
  - **Reindex:** builds the new collection, checks the count, and switches state.
  - **Commit status:** abbreviated hashes match both ways.
  - **Import:** a newer `updated_at` wins.
  - **Tokens:** stored as hashes; a revoked token gets a 401.
- **Migration:** none. The server starts empty, and the laptop flow is unchanged.

### PR 3: MCP over streamable HTTP

- **Scope:**
  - `search`, `get`, and `list` at `/mcp`, reshaped from `16a1f2c`.
  - They read the server's SQLite and the active collection, and embed the query with `embed_query`.
  - Read scope is required when auth is on.
  - `.mcp.json` → `{"type": "http", "url": "http://127.0.0.1:8765/mcp"}`.
  - The MCP `INSTRUCTIONS` text.
- **Tests:**
  - the filter, body, and result-shape tests from the branch
  - the tool list contains no write tool, which is asserted by name
  - a Qdrant error becomes a tool error, not a crash
  - 401 without a token when auth is on
- **Migration:** none. The tools return results after PR 4's `migrate`.

### PR 4: Client foundations and `migrate`

- **Scope:**
  - `scripts/server_client.py`, using only the standard library.
  - The `server`, `servers`, and `tokens` config files.
  - `agent-memory connect --url`, without editors.
  - `agent-memory migrate [--dry-run]`.
  - `list` goes through the server when one is configured.
  - Approve and Reject still write locally.
- **Tests:**
  - parsing of the config files, including the per-repo mapping
  - migrate against a stub server: 86 rows in a fixture file arrive with ids, people, and timestamps intact; rejections arrive; a rerun is a no-op
  - the dry-run counts
  - `connect` writes mode 0600
- **Migration:** steps 1 to 6 of [Moving the existing store](#moving-the-existing-store).

### PR 5: Cutover

- **Scope:**
  - Approve, Reject, `land`, `git-hook`, and `auto_approve` go through the server.
  - The outbox and `agent-memory sync`.
  - The review page uses `whoami` for its defaults and shows the banner.
  - `flush` is retired, and `scripts/embed` becomes a wrapper.
  - `compose.yaml` loses Qdrant's ports and the `./data/shared` mount.
  - `scripts/setup` starts both services and writes the `server` config.
  - README and `integration.md` updated, plus the `context.md` wording amendment below, after Miguel approves it.
- **Tests:**
  - Approve while the server is up → POST, and the bundle is deleted.
  - Approve while it is down → an outbox file holding only the request body, and the bundle is deleted.
  - A 4xx → the bundle is kept.
  - `sync` sends in order, and an item that fails for good goes to `failed/`.
  - `land` skips a commit reported kept or rejected, and one that is waiting in the outbox.
  - `git-hook` finishes in 2 seconds against a server that doesn't answer.
  - Existing CLI tests run against a stub server.
- **Migration:**
  1. Upgrade.
  2. `./scripts/setup`.
  3. `agent-memory migrate`, once more.
  4. `agent-memory-server reindex --prune`.
  5. Check that `curl 127.0.0.1:6333` now fails.

### PR 6: Editors

- **Scope:**
  - `agent-memory connect --editor claude-code,cursor,codex [--print] [--yes]`.
  - Each editor gets a read-scope token minted for it.
  - The lines it writes or prints, as above. The Codex and Cursor key names are checked against current docs in this PR.
  - `uninstall` removes the MCP entries it added.
- **Tests:**
  - It merges into existing `mcp.json` and `config.toml` without touching other servers.
  - `--print` writes nothing.
  - The token in the editor files is never the write token.
  - With auth off, no header is written.
  - `uninstall` removes only its own entry.
- **Migration:** run `agent-memory connect --editor …` once per laptop. `.mcp.json` in this repo stays as the project-level entry.

### PR 7: Packaging and image

- **Scope:**
  - `pyproject.toml` and `uv.lock`, with the `[server]` extra and both entry points.
  - `./scripts/install` → `uv tool install --editable .`.
  - The laptop's state moves to the XDG data directory, with a one-time move.
  - `agent-memory install` points hooks at the installed binary.
  - The GHCR workflow, building amd64 and arm64 on `v*` tags.
  - `compose.yaml` pinned to the image tag.
  - `compose.team.yaml` (auth on) and `docs/deploy.md`.
- **Tests:**
  - The move: an inbox at the old path ends up at the new one, and a second run does nothing.
  - Hook commands are rewritten only where they carry the marker.
  - The wheel builds, and `agent-memory --help` runs from a fresh `uv tool install`, in CI.
  - The image builds and answers `/v1/health`, in CI.
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
- **Auth-off misuse.** The server can't see how Docker publishes its port. Auth off is safe only because of the `127.0.0.1:` mapping in `compose.yaml`. `docs/deploy.md` says so, and `compose.team.yaml` turns auth on.
