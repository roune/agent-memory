# Agent brief

This repo is a brief, not a product yet. There is no application code. The vector engine is Qdrant server, started by `scripts/setup` into `data/qdrant/`. The embedding model is `BAAI/bge-small-en-v1.5`, run by `scripts/embed`. See [docs/vector-engine.md](docs/vector-engine.md) and [docs/embedding-model.md](docs/embedding-model.md).

## This session’s job

Research first. Read [docs/context.md](docs/context.md), [docs/harnesses.md](docs/harnesses.md), and [docs/research.md](docs/research.md). Answer the questions in the research doc against current docs and repos. Record the gap. Do not adopt a project by default.

## Do not

- Swap the vector engine or the embedding model. They are Qdrant server and `BAAI/bge-small-en-v1.5`. A different model means deleting the collection and embedding again.
- Add a dependency for capture or review until section 1 of [docs/research.md](docs/research.md) is answered.
- Install hooks into `~/.cursor`, `~/.claude`, `~/.codex`, or Pi.
- Commit transcripts, inbox dumps, anything under `memories/`, or anything under `data/`.
- Reopen the decisions in [docs/context.md](docs/context.md). Those are locked.
- Build retrieval. The first build stops at capture, review, and embed.

## When research is done

Propose one vector engine and one embedding model that a setup script can create locally and that can later run as a container. Then implement capture, review, and embed. Leave session-start retrieval for a later phase.
