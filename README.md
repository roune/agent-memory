# Agent memory

Agent memory files a coding-agent session, or a slice of git history, and lets you choose what to keep. Only the notes you approve are saved, and only those notes are embedded for search.

## Install

From a clone of this repo:

```
./scripts/install
```

That links `agent-memory` into `~/.local/bin`. It leaves Cursor, Claude Code, Codex, and Pi alone. Connecting those is a second command, and it asks before it writes:

```
agent-memory install
```

Pass `--yes` to skip that prompt. To set up one git repository in the same step, pass its path. The command then asks whether to install post-commit and post-merge hooks, and whether those hooks should approve the commit message without the review page:

```
agent-memory install --git-repo /path/to/repo
```

With `--yes` and `--git-repo`, also pass `--install-git-hooks true` or `false`. Add `--install-auto-approve true` to approve those git drafts immediately. Omitting it leaves auto-approve off.

## Features

- **Session capture.** Hooks for Cursor, Claude Code, Codex, and Pi write an inbox bundle (which tool, which repo, the session id, and a pointer at the transcript) and return. The choice happens later, in review.
- **Review.** `agent-memory review` opens an approve/reject page on `127.0.0.1`, starting at port 4173. You keep one note per bundle: a `change`, a `learning`, or a `decision`, in the domain `code`, `architecture`, or `operations`. Edit the text before you approve. Reject drops the bundle.
- **Land.** `agent-memory land` files recent commits on a clone’s local default branch, grouped by pull request when it can tell the PR. It skips merge commits. The draft comes from the PR title, body, and commit messages. Diffs stay in git. These bundles always wait for review.
- **Git hooks.** With `git_hooks` on, post-commit and post-merge run `agent-memory git-hook`. Each new commit becomes an inbox bundle whose draft is the commit message. A merge or fast-forward files every new single-parent commit, not only the tip. `auto_approve` keeps that draft when `~/.config/agent-memory/identity` has org, team, and person.
- **Repo policy.** `.agent-memory.toml` holds `capture`, `repo`, `git_hooks`, and `auto_approve`. `agent-memory config` shows them and can turn the git hooks and auto-approve on or off without reinstalling editor hooks. `capture = false` skips capture, `land`, and the git hooks for that clone.
- **Index.** Approved memories live in a local SQLite file, `data/memories.sqlite`. `scripts/setup` starts Qdrant, which is only the vector index. `scripts/embed` reads that SQLite file and embeds with `BAAI/bge-small-en-v1.5`. Raw transcripts are never embedded.

## Use

Work in Cursor, Claude Code, Codex, or Pi. When a session ends, the hook files the inbox. Open the queue:

```
agent-memory review
```

The page lists sessions whose work is on the default branch, plus anything you pinned. A link on the page shows the rest of the inbox. If the server is already running, the command only opens its URL.

Approve writes a row in the local SQLite database and removes the inbox folder. It needs org, team, and person, from the form or from `~/.config/agent-memory/identity`, and a remote repo id from `origin` or from `repo` in `.agent-memory.toml`. A change also needs the commit it explains. Capture, review, and land do not need Qdrant.

`~/.config/agent-memory/database` sets `mode` (`shared` or `per-repo`) and `qdrant_url`. The default is shared mode and `http://127.0.0.1:6333`, so a laptop works with no company host. A company deployment sets `qdrant_url` to its own container. Embed and flush use that URL. In per-repo mode, `~/.config/agent-memory/databases` maps each remote id to that remote's URL, and each remote has its own SQLite file.

```
agent-memory list --team platform --since 2026-11-01T00:00:00+00:00
agent-memory list --org acme --since 2026-11-01T00:00:00+00:00
agent-memory list --repo github.com/owner/name
agent-memory flush
agent-memory import-json
```

`import-json` loads an existing `memories/*.json` folder once. `flush` copies pending rows into the SQLite file mounted in the container when that URL answers `/readyz`, and leaves the laptop file unchanged when it does not.

File commits that are already on the local default branch. The window defaults to the last 24 hours. `--since` takes an ISO-8601 time or a number of hours ago. The branch name comes from `origin/HEAD` when that local branch exists, otherwise `main` or `master`, and unpushed local commits are included:

```
agent-memory land /path/to/repo
agent-memory land /path/to/repo --since 72
```

Turn git hooks on or off later:

```
agent-memory config /path/to/repo
agent-memory config /path/to/repo --git-hooks true --auto-approve false
```

Pin a draft, force a captured session into the default queue, drop a bundle, or remove the editor hooks this tool added:

```
agent-memory note --session-id my-note --kind learning --domain code --text "what to keep"
agent-memory keep <session-id>
agent-memory drop <session-id>
agent-memory uninstall
```

After you approve, index what you kept:

```
./scripts/setup
./scripts/embed
```

## License

Noncommercial use is free under the [PolyForm Noncommercial License 1.0.0](LICENSE).

Required Notice: Copyright (c) 2026 Miguel Porcar (https://github.com/roune/agent-memory)

## Where to read next

- [docs/context.md](docs/context.md) — decisions already made.
- [docs/harnesses.md](docs/harnesses.md) — where each tool’s hook will live, and the capture constraints.
- [docs/integration.md](docs/integration.md) — how a person installs capture, and where the vector index stays out of the hook.
- [docs/vector-engine.md](docs/vector-engine.md) — why the engine is Qdrant server, and how `scripts/setup` starts it.
- [docs/embedding-model.md](docs/embedding-model.md) — the model `scripts/embed` runs, and the pinned checkpoint.
- [docs/research.md](docs/research.md) — the “what already does this” question, still open.
