# Agent memory

Memory for coding agents, free for noncommercial use. A session ends in Cursor, Claude Code, Codex, or Pi. A hook files what happened. You choose what is worth keeping. A script embeds only that.

## Install

From a clone of this repo:

```
./scripts/install
```

That links `agent-memory` into `~/.local/bin`. It does not change Cursor, Claude Code, Codex, or Pi. Connecting those is a second command, and it asks before it writes:

```
agent-memory install
```

`agent-memory review` opens the approve/reject page at `http://127.0.0.1:4173`.

## Two jobs

1. **Capture, then choose.** When a session ends, a hook writes an inbox bundle: which tool, which repo, the session id, and a pointer at the transcript and the diff. A later review command is where you keep nothing, the change summary, the learnings, or specific slices. The end hook does not ask. Those hooks cannot reliably stop the session and wait for an answer.
2. **Index only what was kept.** Approved memory documents are the source of truth. `scripts/setup` starts Qdrant server 1.20.0 with its storage in this repo’s `data/qdrant/` directory. `scripts/embed` runs `BAAI/bge-small-en-v1.5` and writes vectors for those documents. Raw transcripts are never embedded.

The database belongs to this tool. Other repositories are tags on a memory (`repo`, `tool`, `session_id`). They do not each grow their own index.

## Privacy

Inbox bundles and transcripts stay on disk and out of git. Review is also the redaction step: secrets and anything you do not want retrieved come out before a document is approved. Only approved documents are embedded.

## Out of scope for now

- No automatic save. Nothing is embedded without a review.
- No retrieval hook at the start of the next session. The first build stops at capture, review, and embed.
- No cloud deploy. A shared container is a later run mode of the same stack: hooks still write a local spool, and a sync step pushes only approved documents when a remote is configured.

## License

Noncommercial use is free under the [PolyForm Noncommercial License 1.0.0](LICENSE). You may use, change, and share this software for a noncommercial purpose. That includes personal projects, research, study, hobby work, and use by charities, schools, public research organizations, and government institutions.

Commercial use is not granted. That includes selling this software, offering it as a service, building it into a paid product, and using it inside a company for that company’s business. Any of those uses needs a separate license from the copyright holder. That license is discretionary and may include a fee.

Required Notice: Copyright (c) 2026 Miguel Porcar (https://github.com/roune/agent-memory)

## Where to read next

- [docs/context.md](docs/context.md) — decisions already made.
- [docs/harnesses.md](docs/harnesses.md) — where each tool’s hook will live, and the capture constraints.
- [docs/integration.md](docs/integration.md) — how a person installs capture, and where the vector index stays out of the hook.
- [docs/vector-engine.md](docs/vector-engine.md) — why the engine is Qdrant server, and how `scripts/setup` starts it.
- [docs/embedding-model.md](docs/embedding-model.md) — the model `scripts/embed` runs, and the pinned checkpoint.
- [docs/research.md](docs/research.md) — the “what already does this” question, still open.
