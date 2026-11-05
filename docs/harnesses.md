# Harnesses

One capture command, called from user-level hooks. Nothing here is installed yet. Facts below were checked on 2026-11-05 against current docs. Sources are linked on each tool.

The capture command copies what it needs into the local inbox and returns. It does not prompt, and it does not embed. It exits 0 and does not ask the agent to continue.

`stop`-style events fire when a turn finishes, not only when the app quits. That is the capture. The session-end event is a marker that the session closed. The same `session_id` is overwritten in the inbox so review sees the latest copy.

## Cursor

Docs: [cursor.com/docs/hooks](https://cursor.com/docs/hooks).

Config: `~/.cursor/hooks.json`. Scripts live under `~/.cursor/hooks/`. User-hook paths are relative to `~/.cursor/`.

Every agent hook, including `stop` and `sessionEnd`, also receives `conversation_id`, `generation_id`, `model`, `hook_event_name`, `cursor_version`, `workspace_roots`, `user_email`, and `transcript_path` (`string | null`). `session_id` on `sessionStart` and `sessionEnd` is the same value as `conversation_id`. Use `conversation_id` as the bundle’s `session_id`.

| Event | Role |
| --- | --- |
| `stop` | The capture. Extra fields: `status` (`completed`, `aborted`, `error`) and `loop_count`. `transcript_path` is on the common payload. A `followup_message` would send another user message. The capture must not return one. |
| `sessionEnd` | A final marker only. Extra fields: `session_id`, `reason` (`completed`, `aborted`, `error`, `window_close`, `user_close`), `duration_ms`, `is_background_agent`, `final_status`, and optional `error_message`. Fire-and-forget. The response is logged and unused. |

`sessionEnd` is tied to the IDE composer session. Cloud agents do not fire it. Do not rely on it as the only reader of the transcript.

## Claude Code

Docs: [code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks).

Config: `~/.claude/settings.json`, under `hooks`.

Common fields include `session_id`, `transcript_path`, `cwd`, and `hook_event_name`. The transcript file is written asynchronously and can lag the in-memory conversation.

| Event | Role |
| --- | --- |
| `Stop` | The capture. Also receives `stop_hook_active`, `last_assistant_message`, `background_tasks`, and `session_crons`. Copy `transcript_path` here. When the file lags, keep `last_assistant_message` with the bundle. Exit 0. `decision: "block"` or exit 2 would continue the agent. |
| `SessionEnd` | Not the capture. It cannot block, and Claude Code discards its JSON output. The schema example includes `transcript_path` and `reason` (`clear`, `resume`, `logout`, `prompt_input_exit`, `other`). The default budget is 1.5 seconds, raisable to 60. |

Worktree bug, still reported: [anthropics/claude-code#44450](https://github.com/anthropics/claude-code/issues/44450). In a git worktree, `transcript_path` is hashed from the worktree `cwd`, while the file is stored under the git-root hash, so the advertised path does not exist. The capture command must stat the path. If it is missing, look up the same `session_id` under the project directory derived from the git root. `${CLAUDE_PROJECT_DIR}` stays at the directory where the session started. `cwd` follows the worktree. The bundle’s `repo` is still the git root when `git rev-parse` succeeds.

## Codex

Docs: [learn.chatgpt.com/docs/hooks](https://learn.chatgpt.com/docs/hooks).

Config: `~/.codex/hooks.json` or inline hooks in `~/.codex/config.toml`. Prefer `hooks.json` only. Codex loads both if both exist and warns.

This machine has no `~/.codex` directory yet. The hook is still specified here so the capture command has one shape.

Common fields include `session_id`, `transcript_path`, `cwd`, and `hook_event_name`. The transcript format is not a stable interface.

| Event | Role |
| --- | --- |
| `Stop` | The capture. Also receives `turn_id`, `stop_hook_active`, and `last_assistant_message`. Default timeout is 600 seconds. A matcher on `Stop` is ignored. Exit 0 with no `decision`. `decision: "block"` would continue the turn. |
| `SessionEnd` | A late marker. It runs for the main thread when a conversation is archived or deleted while open, when Codex closes normally, or after the thread has been idle and closed for 30 minutes. It does not run for subagents, and switching away from the thread does not fire it. `reason` is currently always `other`. Codex flushes the transcript first, so a copy can succeed, but the hook is advisory, output does not steer the thread, the default timeout is 1 second, and the maximum is about 3 seconds. |

## Pi

Docs: [pi.dev/docs/latest/extensions](https://pi.dev/docs/latest/extensions).

Pi has no `hooks.json`. Capture is a user-level extension. Global files live in `~/.pi/agent/extensions/` as a `.ts` or `.js` file, or a directory with `index.ts`. Project-local `.pi/extensions/` loads only after the project is trusted. A package path can also be listed in `~/.pi/agent/settings.json`.

The extension reads the session through `ctx.sessionManager.getSessionFile()`, `getSessionId()`, and `getCwd()`, then shells out to the same capture command.

| Event | Role |
| --- | --- |
| `agent_settled` | The capture. The turn is finished and Pi will not continue on its own. `event.aborted` is true when the run was aborted. `agent_end` is too early: retries, compaction, and queued messages can still follow. |
| `session_shutdown` | End marker. `event.reason` is `quit`, `reload`, `new`, `resume`, or `fork`. `quit` covers Ctrl+C, Ctrl+D, SIGHUP, and SIGTERM. The other reasons mean this session is being replaced. `event.targetSessionFile` is the destination on replacement. |

`ctx.ui.confirm()` can ask the person a question. The capture extension must not call it. Review stays a separate command.

## Inbox bundle

Whatever the harness, the capture command writes one bundle:

- `tool` — `cursor`, `claude-code`, `codex`, or `pi`
- `session_id`
- `repo` — git root when there is one, otherwise `cwd`
- `ended_at`
- `event` — the harness event that wrote this copy
- a copy of the transcript, or a path that was verified readable at capture time
- `last_assistant_message` when the harness provides it and the transcript file lags
- a short git summary (status, diff stat), not the full patch, unless the review step asks for it later

The bundle stays under `inbox/` and is gitignored. Approval copies a redacted slice into a memory document. The transcript itself is never committed and never embedded.

A fifth harness is another adapter that maps its event onto this bundle. It is not a new capture command.
