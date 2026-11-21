# Agent brief

This repo is a brief, not a product yet. There is no application code. The vector engine is Qdrant server, started by `scripts/setup` into `data/qdrant/`. The default embedding model is `BAAI/bge-small-en-v1.5` through fastembed at a pinned revision, run by `scripts/embed`. Other models plug in through `scripts/embedder.py`. See [docs/vector-engine.md](docs/vector-engine.md) and [docs/embedding-model.md](docs/embedding-model.md).

## This session’s job

Research first. Read [docs/context.md](docs/context.md), [docs/harnesses.md](docs/harnesses.md), and [docs/research.md](docs/research.md). Answer the questions in the research doc against current docs and repos. Record the gap. Do not adopt a project by default.

## Do not

- Swap the vector engine. It is Qdrant server.
- Change the default embedding model or its pinned revision, or implement a hosted adapter (OpenAI, Bedrock), without a plan PR first. A deployment runs one embedder. A different model means a new collection and embedding every row of `memories.sqlite` again. Until the server's `reindex` exists, that is `scripts/embed --rebuild`.
- Add a dependency for capture or review until section 1 of [docs/research.md](docs/research.md) is answered.
- Install hooks into `~/.cursor`, `~/.claude`, `~/.codex`, or Pi.
- Commit transcripts, inbox dumps, or anything under `data/`.
- Reopen the decisions in [docs/context.md](docs/context.md). Those are locked.
- Build retrieval. The first build stops at capture, review, and embed.

## Server work

The move to a server in Docker follows [docs/server-plan.md](docs/server-plan.md), one PR at a time and in its order.

## When research is done

Propose one vector engine and one embedding model that a setup script can create locally and that can later run as a container. Then implement capture, review, and embed. Leave session-start retrieval for a later phase.
