# Decisions already made

Do not reopen these. The research session picks an engine and a model. It does not redesign the product.

## What this is

A tool that files a coding-agent session, lets a person choose what to keep, and embeds only that choice.

The two jobs, in order:

1. Capture, then choose.
2. Index only what was kept.

## The store

The vector database belongs to this tool. A setup script creates it in this repo’s `data/` directory, which is gitignored. Other repositories are tags on a memory, not places that each grow their own index.

Tags on every memory:

- `repo` — the project the session was working in
- `tool` — `cursor`, `claude-code`, `codex`, or `pi`
- `session_id` — the harness session that produced it

Approved memory documents are the source of truth. The vector index is derived from them, so the engine can change after research without losing what was kept.

## What a kept item is

Each approved item has one kind:

- `change` — what the session changed
- `learning` — what the session found out
- `decision` — what was chosen, and the reason if it was stated

Review is also the redaction step. Secrets and anything that should not be retrieved are removed before a document is approved. Raw transcripts are never embedded and never committed.

## How a session becomes a memory

```
session end hook
  -> local inbox bundle
review command
  -> keep nothing, or one or more documents (change, learning, decision)
setup and embed script
  -> vectors in data/
```

The hook writes the inbox bundle and returns. It does not prompt. End hooks cannot reliably stop the session and wait for a choice:

- Claude Code `SessionEnd` cannot block.
- Codex `SessionEnd` is advisory and capped at about 3 seconds.
- Transcript files are sometimes missing or stale by the time `SessionEnd` runs.

The review command is where the person decides to keep nothing, the change summary, the learnings, or specific slices.

## Hooks, once they exist

User-level hooks call one capture command. They are not installed yet. Locations and which event does the real capture are in [harnesses.md](harnesses.md).

Hooks always write a local spool first. A sync step, later, pushes only approved documents when a remote is configured.

## Later, not now

Enterprise sharing is a later run mode of the same stack, not a second design. Locally the script brings the database up on disk or in a local container. Later, the same service can be deployed so more than one person can query it.

Retrieval at the start of the next agent session is a later phase. The first build stops at capture, review, and embed.

## Explicitly out

- No automatic save.
- No embedding of raw transcripts.
- No per-repo vector database.
- No cloud deploy in the first build.
- No retrieval hook in the first build.
