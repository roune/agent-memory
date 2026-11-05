# Agent memory

Open-source memory for coding agents. A session ends in Cursor, Claude Code, Codex, or Pi. A hook files what happened. You choose what is worth keeping. A script embeds only that.

## Two jobs

1. **Capture, then choose.** When a session ends, a hook writes an inbox bundle: which tool, which repo, the session id, and a pointer at the transcript and the diff. A later review command is where you keep nothing, the change summary, the learnings, or specific slices. The end hook does not ask. Those hooks cannot reliably stop the session and wait for an answer.
2. **Index only what was kept.** Approved memory documents are the source of truth. A setup script creates the vector database in this repo’s `data/` directory and an embedding model turns those documents into vectors. Raw transcripts are never embedded.

The database belongs to this tool. Other repositories are tags on a memory (`repo`, `tool`, `session_id`). They do not each grow their own index.

## Privacy

Inbox bundles and transcripts stay on disk and out of git. Review is also the redaction step: secrets and anything you do not want retrieved come out before a document is approved. Only approved documents are embedded.

## Out of scope for now

- No automatic save. Nothing is embedded without a review.
- No retrieval hook at the start of the next session. The first build stops at capture, review, and embed.
- No cloud deploy. A shared container is a later run mode of the same stack: hooks still write a local spool, and a sync step pushes only approved documents when a remote is configured.

## Where to read next

- [docs/context.md](docs/context.md) — decisions already made.
- [docs/harnesses.md](docs/harnesses.md) — where each tool’s hook will live, and the capture constraints.
- [docs/research.md](docs/research.md) — questions the next session has to answer before any dependency is added.
