# Decisions already made

Do not reopen these. The research session picks an engine and a model. It does not redesign the product.

## What this is

A tool that files a coding-agent session (or a slice of default-branch history), lets a person choose what to keep, and embeds only that choice.

The two jobs, in order:

1. Capture, then choose.
2. Index only what was kept.

## The store

The vector database belongs to this tool. A setup script creates it in this repo’s `data/` directory, which is gitignored. Other repositories are tags on a memory, not places that each grow their own index.

Tags on every memory:

- `repo` — normalized remote project id (`host/owner/name`, e.g. `github.com/roune/agent-memory` from `origin`, or `repo=` in `.agent-memory.toml` when there is no `origin`)
- `tool` — `cursor`, `claude-code`, `codex`, `pi`, or `git` (for bundles filed by `land`)
- `session_id` — the harness session, a `note` id, or a stable id from `land`
- `domain` — `code`, `architecture`, or `operations`
- `org`, `team`, `person` — who is keeping the note (defaults from `~/.config/agent-memory/identity`; overridable per document on the review form)
- `occurred_at` — when the work applies (from the bundle’s `ended_at` by default; overridable on the form)

Optional **linked-record** fields (for standing explanations, architecture history, and runbooks; omit for a plain note):

- `record` — empty, `explanation`, `history`, or `runbook`
- `subject` or `alarm` — stable name for the system, flow, or alert
- `look_at` — short names of signals or places to check (often for operations)
- `supersedes`, `superseded_by`, `updates` — links between documents (when you file a newer explanation or runbook, stamp `superseded_by` on the old file; `approve` can also set `supersedes` to patch the old file in place)

Approved memory documents are the source of truth. The vector index is derived from them, so the engine can change after research without losing what was kept. All three domains share the one `memories` collection. Search filters on `domain`. A second collection is for a different embedding model only.

## Repo policy file (`.agent-memory.toml`)

Optional file at the root of a git working tree. Keys are simple `key = value` lines (same style as today). The tool reads:

- `capture` — when `false`, `capture`, `land`, and git post-commit/post-merge hooks skip that repo (default: true)
- `repo` — optional override for the normalized remote id when there is no usable `origin` (same meaning as today)
- `git_hooks` — when `true`, post-commit and post-merge run `agent-memory git-hook` for that clone. Each new commit files a `tool: git` inbox bundle with a draft from the **commit message only** (no diff). A merge or fast-forward files every new single-parent commit, not only the tip. An existing hook is kept and run after this one. Hooks are written where git runs them (`core.hooksPath`, or the common git dir for a linked worktree).
- `auto_approve` — when `true`, those git hooks approve the commit-message draft (requires a complete `~/.config/agent-memory/identity`). A session-end hook does that only for a commit made after this session’s first capture, and only from the commit message. It does not file the last assistant reply against whatever `HEAD` is. Otherwise the inbox bundle stays for review (default: false). `agent-memory land` always files bundles for the review UI only

Change `git_hooks` or `auto_approve` later with `agent-memory config <path-to-repo> [--git-hooks true|false] [--auto-approve true|false]` without reinstalling editor hooks.

## What a kept item is

Each approved item has one kind:

- `change` — the context of one commit: what it was for. The diff stays in git. The document records the commit.
- `learning` — what the session found out
- `decision` — what was chosen, and the reason if it was stated

Kind is the shape of the note. Domain is which job it is for:

- `code` — what was implemented. The text is the context of the work. A change names the commit. The repository holds the source.
- `architecture` — how a design or a flow works, and the reason when it was stated.
- `operations` — what an incident, an alert, or a recurring situation means, and what to look at.

Review is also the redaction step. Secrets and anything that should not be retrieved are removed before a document is approved. Raw transcripts are never embedded and never committed.

## How work becomes a memory

**Agent sessions (hooks):**

```
session end hook
  -> local inbox bundle
agent-memory review
  -> keep nothing, or one document (change, learning, or decision)
setup and embed script
  -> vectors in data/
```

**Same clone, after each commit or merge (optional git hooks):**

```
post-commit / post-merge
  -> inbox bundle (tool: git), draft = commit message only; or auto-approve into memories/ when configured
```

**Default-branch commits in another repo:**

```
agent-memory land <path-to-repo> [--since …]
  -> inbox bundles under inbox/git/ (one per PR group or orphan commit), draft prefilled
agent-memory review
  -> same approve/reject flow; nothing is written to memories/ until Approve
```

`land` lists commits on that clone’s local default branch since `--since` (default: 24 hours before now). The branch name comes from `origin/HEAD` when that local branch exists; otherwise `main` or `master`. It does not read the remote-tracking ref, so unpushed local commits are included. It skips merge commits and groups single-parent commits by pull request when it can tell the PR (merge/squash message lines, or `gh pr list` when `gh` is available and authenticated). It uses file names across the group to add a short “unusual vs the rest” hint — it does not store diffs. PR title/body and each commit message seed `draft.text`. Bundles use `tool` `git` and a stable `session_id` derived from the group.

The hook writes the inbox bundle and returns. It does not prompt. End hooks cannot reliably stop the session and wait for a choice:

- Claude Code `SessionEnd` cannot block.
- Codex `SessionEnd` is advisory and capped at about 3 seconds.
- Transcript files are sometimes missing or stale by the time `SessionEnd` runs.

The review command is where the person decides to keep nothing, the change summary, the learnings, or specific slices. Approve stamps `org`, `team`, `person`, `occurred_at`, and the remote `repo` id, then writes `memories/<person>-<kind>-<session_id>.json` and deletes the inbox folder. If you set `supersedes` to an existing document id, the old file gets `superseded_by` set to this document’s id.

## Hooks, once they exist

User-level hooks call one capture command. Locations and which event does the real capture are in [harnesses.md](harnesses.md).

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
