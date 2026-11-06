# Integration plan

How someone installs capture into Cursor, Claude Code, Codex, and Pi, and how that stays separate from the vector index. Decisions in [context.md](context.md) stay as they are. Hook payloads are in [harnesses.md](harnesses.md). The engine is Qdrant server, started by `scripts/setup`. The embedding model is `BAAI/bge-small-en-v1.5`, run by `scripts/embed`. See [embedding-model.md](embedding-model.md).

## The seam

The hook never opens the database. A later embed script reads approved documents and writes `data/`. Those documents are the source of truth, so the engine chosen in the other session can change without a new hook.

```
user-level hook or Pi extension
  -> one capture command
  -> inbox bundle on disk
review command
  -> keep nothing, or change / learning / decision documents
embed script, after the engine is chosen
  -> vectors in data/
```

Retrieval at the next session start is still out.

## What a person installs

One executable, four thin adapters, and an installer that asks once.

| Piece | Job |
| --- | --- |
| `agent-memory capture` | Reads the adapter’s JSON on stdin, writes the inbox bundle, exits 0. |
| `agent-memory review` | Opens the browser UI on `127.0.0.1`. Approve and reject happen there. |
| `agent-memory install` | Writes the user-level adapter for each tool the person selects. |
| `agent-memory uninstall` | Removes only the entries this tool added. |

The adapters are a few lines each. They do not embed, and they do not import a vector client. Cursor, Claude Code, and Codex call a shell snippet. Pi calls the same executable from an extension, because Pi’s API is an event listener rather than `hooks.json`.

`install` is idempotent. It merges into an existing hooks file and leaves every other hook alone. It refuses to write both `~/.codex/hooks.json` and a `[hooks]` table in `config.toml`.

Suggested first-run shape:

```
./scripts/install
agent-memory install
```

`./scripts/install` links the command into `~/.local/bin`. `agent-memory install` lists the tools it found (`~/.cursor`, `~/.claude`, `~/.codex`, `~/.pi/agent`) and waits for a yes. That yes is the permission to turn capture on. After that, session hooks stay silent.

## Hook map

Capture runs when a turn finishes, because that is when the transcript is readable and the hook is allowed to take more than a couple of seconds. The session-end event only marks the bundle closed.

| Tool | Capture event | End marker | Config the installer writes |
| --- | --- | --- | --- |
| Cursor | `stop` | `sessionEnd` | `~/.cursor/hooks.json` and `~/.cursor/hooks/` |
| Claude Code | `Stop` | `SessionEnd` | `~/.claude/settings.json` |
| Codex | `Stop` | `SessionEnd` | `~/.codex/hooks.json` |
| Pi | `agent_settled` | `session_shutdown` | `~/.pi/agent/extensions/agent-memory/` |

Each adapter passes `--tool` and `--event` so the bundle records who wrote it. The capture command normalizes `session_id`, `cwd`, and `transcript_path` from that tool’s payload. For Claude Code worktrees it stats `transcript_path` and, if the file is missing, looks up the same session id under the git-root project directory.

The capture command fails open. A missing transcript, a short timeout, or a crash logs locally and still exits 0. It must not return Cursor’s `followup_message`, Claude Code or Codex `decision: "block"`, or a Pi `continue`.

## Permission

Two different questions, two different moments.

The installer asks once: which tools, and whether to edit those home files. That is the opt-in.

The hook does not ask. Cursor `sessionEnd` ignores the response. Claude Code `SessionEnd` cannot block and discards output. Codex `SessionEnd` is advisory and capped at about 3 seconds. Pi could call `ctx.ui.confirm()` and must not. A prompt there would not work the same way in every tool.

Review opens in the browser. `agent-memory review` starts a process bound to `127.0.0.1` and a chosen port, then opens `http://127.0.0.1:<port>`. If that process is already running, the command only opens the URL. Cursor, Claude Code, Codex, and Pi do the same: a command or a link to that address. They do not each draw their own form.

The page lists each offered session. A `change` asks for the context of one commit and stores that commit. The diff stays in git. A `learning` or `decision` asks for the note itself. The text can be edited before approval, which is the redaction step. Approve writes that document. Reject drops the bundle. The transcript stays out of the index either way.

The end hook does not start this process and does not open the browser. Capture still writes the inbox and returns.

## Which sessions become memories

Company sharing stays a later run mode. This section is the local personal store only.

The inbox keeps every captured session on disk, gitignored. The memory is the approved document. `agent-memory review` is the default view of sessions worth approving. A session reaches that view in two ways.

| Way in | What it means |
| --- | --- |
| Landed on the default branch | A commit that overlaps this session is now contained in the repo’s default branch. This is the default. |
| Manual | `agent-memory keep` pins a session that never landed. Learnings and decisions with no commit use this path. |

`agent-memory drop` removes a bundle from the inbox. `agent-memory review --all` shows sessions the default view hides.

The landing check is local git, run when the page loads. The bundle stores `started_at`, `ended_at`, the git root, the branch, `HEAD`, and the paths touched. The page asks, in that clone:

```
git merge-base --is-ancestor <sha> <default-branch>
```

The default branch is `origin/HEAD` when that ref exists, otherwise `main` or `master`. A commit overlaps the session when its files intersect the paths the session touched and its time is at or after `started_at`. People often commit after the agent stops, so the check looks forward from `started_at` until review time, and the path overlap is what ties the commit to the session. The queue shows the commit subject next to the session so a loose match can be dropped.

At session end the work is usually still on a feature branch. The hook only writes the inbox. The session appears in the default review view later, after that commit is in the default branch locally. A `git fetch` the person already does is enough for a remote merge to count.

Scratch repos stay out of the inbox with a repo file, `.agent-memory.toml`, containing `capture = false`. The capture command reads it and returns. A global ignore list in the user config covers the same need across machines. Those files are policy. They do not create a second index.

Review still approves every document. The landing rule chooses what is offered. Approval is also the redaction step, so a commit on the default branch does not by itself embed the transcript. Embed runs only after that approval. An empty tree with an unchanged transcript can skip a new inbox write.

## Repo layout for the first build

```
agent-memory          capture, review, install, uninstall
adapters/             cursor, claude-code, codex shell snippets
adapters/pi/          extension that shells out to capture
inbox/                gitignored bundles
memories/             approved documents, the source of truth
data/                 gitignored vector index, created by the embed script
```

`capture` and `review` can land on their own. `scripts/embed` writes vectors for approved documents to the Qdrant server `scripts/setup` starts. Hooks stay pointed at `capture` either way.

## Out of this plan

- No retrieval hook at session start.
- No embedding inside the hook.
- No per-repo index.
- No cloud sync. A later sync step pushes approved documents only.
- No fifth product. Another agent is a new adapter on the same command.
