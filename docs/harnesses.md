# Harnesses

One capture command, called from user-level hooks. Nothing here is installed yet. The research session still has to confirm Pi’s API and Cursor’s `sessionEnd` stdin fields. See the open items in [research.md](research.md).

The capture command copies what it needs into the local inbox and returns. It does not prompt, and it does not embed.

## Cursor

Config: `~/.cursor/hooks.json`. Scripts live under `~/.cursor/hooks/`.

| Event | Role |
| --- | --- |
| `stop` | The capture. The transcript is still available here. |
| `sessionEnd` | A final marker only. Do not rely on it as the only reader of the transcript. |

`stop` can follow up into the session. That is not the review step. Review stays a separate command so the same flow works in every harness.

Confirm the `sessionEnd` stdin fields against current Cursor docs before writing the hook. Do not invent the payload.

## Claude Code

Config: `~/.claude/settings.json`.

| Event | Role |
| --- | --- |
| `Stop` | The capture. This event receives `transcript_path`. |
| `SessionEnd` | Not the capture. It cannot block, and `transcript_path` has been missing or already deleted when the hook runs. There is a known race where the file is gone, and a known worktree bug where `transcript_path` hashes `cwd` instead of the git root. |

Hooks that need the final assistant text of the current turn should use `last_assistant_message` on `Stop` when the transcript lags. Copy the transcript during `Stop`, while the file still exists.

## Codex

Config: `~/.codex/hooks.json` or inline hooks in `~/.codex/config.toml`. Prefer one representation. Codex loads both if both exist and warns.

This machine has no `~/.codex` directory yet. The hook is still specified here so the capture command has one shape.

| Event | Role |
| --- | --- |
| `Stop` | The capture. Read the transcript here. |
| `SessionEnd` | Only if copying a path fits in the limit. It is advisory, does not run for subagents, and defaults to a 1 second timeout with a maximum of about 3 seconds. `reason` is currently always `other`. It does not fire just because you switched away from the thread. |

`SessionEnd` runs when the main thread is archived or deleted while open, when Codex closes normally, or after the thread has been idle and closed for 30 minutes. Treat it as a late marker, not as the moment the transcript is known to be readable.

## Pi

Pi’s extension events are unverified. Do not invent hook names, payloads, or config paths. The research session checks the current Pi docs and records the real event that can read a finished session. Until that note exists, Pi is a target, not an integration.

## Inbox bundle

Whatever the harness, the capture command writes one bundle:

- `tool` — `cursor`, `claude-code`, `codex`, or `pi`
- `session_id`
- `repo` — git root when there is one, otherwise `cwd`
- `ended_at`
- a copy of the transcript, or a path that was verified readable at capture time
- a short git summary (status, diff stat), not the full patch, unless the review step asks for it later

The bundle stays under `inbox/` and is gitignored. Approval copies a redacted slice into a memory document. The transcript itself is never committed and never embedded.
