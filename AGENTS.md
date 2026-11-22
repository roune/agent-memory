# Agent brief

This repo is a brief, not a product yet. There is no application code. The vector engine is Qdrant server, started by `scripts/setup` into `data/qdrant/`. The default embedding model is `BAAI/bge-small-en-v1.5` through fastembed at a pinned revision, run by `scripts/embed`. Other models plug in through `scripts/embedder.py`. See [docs/vector-engine.md](docs/vector-engine.md) and [docs/embedding-model.md](docs/embedding-model.md).

## This session’s job

Research first. Read [docs/context.md](docs/context.md), [docs/harnesses.md](docs/harnesses.md), and [docs/research.md](docs/research.md). Answer the questions in the research doc against current docs and repos. Record the gap. Do not adopt a project by default.

## Do not

- Swap the vector engine. It is Qdrant server.
- Change the default embedding model or its pinned revision, or implement a hosted adapter (OpenAI, Bedrock), without a plan PR first. A deployment runs one embedder. A different model means embedding every row of `memories.sqlite` again. Today `scripts/embed --rebuild` deletes the `memories` collection and builds it again at the new model's size. The server's `reindex` will keep one collection per model instead.
- Add a dependency for capture or review until section 1 of [docs/research.md](docs/research.md) is answered.
- Install hooks into `~/.cursor`, `~/.claude`, `~/.codex`, or Pi.
- Commit transcripts, inbox dumps, or anything under `data/`.
- Reopen the decisions in [docs/context.md](docs/context.md). Those are locked.
- Add a write tool to the MCP server. Writes go through the authenticated `/v1` API, which only the review flow calls.
- Build automatic retrieval: no session-start hook that injects memories into a session. Query is on demand only, through the server's read-only MCP tools.

## Server work

The move to a server in Docker follows [docs/server-plan.md](docs/server-plan.md), one PR at a time and in its order.

## When research is done

Propose one vector engine and one embedding model that a setup script can create locally and that can later run as a container. Then implement capture, review, and embed. Leave session-start retrieval for a later phase.
