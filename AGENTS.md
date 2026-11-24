# Agent brief

This repo ships two things. The `agent-memory` CLI files coding-agent sessions and git history into a local inbox (`capture`, `land`, the git hooks), and lets a person keep or drop each one (`review`). It sends what was kept to the server, through the outbox when the server is down (`sync`), copies an old laptop store into the server (`migrate`), and points the CLI and editors at a server (`connect`). The agent-memory server in Docker stores approved memories in `memories.sqlite`, takes writes on the `/v1` API, embeds each write into Qdrant, and answers read-only MCP (`search`, `get`, `list`) over streamable HTTP at `/mcp`.

The vector engine is Qdrant server, which only the agent-memory server container in `compose.yaml` can reach. The default embedding model is `BAAI/bge-small-en-v1.5` through fastembed at a pinned revision, run inside that server. Other models plug in through `scripts/embedder.py`. See [docs/vector-engine.md](docs/vector-engine.md), [docs/embedding-model.md](docs/embedding-model.md), and [docs/server-plan.md](docs/server-plan.md).

## Working here

Read [docs/context.md](docs/context.md) first. [docs/server.md](docs/server.md) describes the server: routes, auth, MCP, embedding, and `reindex`. [docs/deploy.md](docs/deploy.md) covers running it on one laptop or as a team server, and how a release is made. [docs/harnesses.md](docs/harnesses.md) and [docs/integration.md](docs/integration.md) cover the editor hooks. [docs/server-plan.md](docs/server-plan.md) is the plan the server was built from. All of its PRs are merged.

Sections 2 to 4 of [docs/research.md](docs/research.md) are answered: the engine, the model, and the hook facts. Section 1, what already does this, is still open.

Run the tests before you open a PR:

```
python3 -m unittest discover -s scripts -p 'test_*.py'
scripts/test-server
```

The first needs only the standard library and skips the tests that need Starlette or the MCP SDK. The second runs the whole suite under `uv run --extra server`, so nothing is skipped. CI runs both.

One PR per piece, each small enough to review on its own.

## Do not

- Swap the vector engine. It is Qdrant server.
- Change the default embedding model or its pinned revision, or implement a hosted adapter (OpenAI, Bedrock), without a plan PR first. A deployment runs one embedder. Each collection records its provider, model, revision, and dimension, and the server refuses to start on a mismatch. Switching means `agent-memory-server reindex`, which embeds every row of `memories.sqlite` that is missing from that model's collection.
- Add a dependency for capture or review until section 1 of [docs/research.md](docs/research.md) is answered.
- Install hooks into `~/.cursor`, `~/.claude`, `~/.codex`, or Pi.
- Commit transcripts, inbox dumps, or anything under `data/`.
- Reopen the decisions in [docs/context.md](docs/context.md). Those are locked.
- Add a write tool to the MCP server. Writes go through the authenticated `/v1` API, which only the review flow calls.
- Build automatic retrieval: no session-start hook that injects memories into a session. Query is on demand only, through the server's read-only MCP tools.
