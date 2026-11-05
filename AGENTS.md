# Agent brief

This repo is a brief, not a product yet. There is no application code and no database.

## This session’s job

Research first. Read [docs/context.md](docs/context.md), [docs/harnesses.md](docs/harnesses.md), and [docs/research.md](docs/research.md). Answer the questions in the research doc against current docs and repos. Record the gap. Do not adopt a project by default.

## Do not

- Add a dependency, a vector engine, or an embedding model until the questions in [docs/research.md](docs/research.md) are answered.
- Install hooks into `~/.cursor`, `~/.claude`, `~/.codex`, or Pi.
- Commit transcripts, inbox dumps, or anything under `data/`.
- Reopen the decisions in [docs/context.md](docs/context.md). Those are locked.
- Build retrieval. The first build stops at capture, review, and embed.

## When research is done

Propose one vector engine and one embedding model that a setup script can create locally and that can later run as a container. Then implement capture, review, and embed. Leave session-start retrieval for a later phase.
