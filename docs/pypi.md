# roune-agent-memory

Agent memory files a coding-agent session, or a slice of git history, and lets you choose what to keep. Only the notes you approve are saved, and only those notes are embedded for search. Transcripts stay on your laptop.

This package is the `agent-memory` command. It uses only the Python standard library. The server it talks to runs in Docker, and `agent-memory setup` starts it for you.

## Install

You need [uv](https://docs.astral.sh/uv/) and Docker with Compose.

```
uv tool install roune-agent-memory
agent-memory setup
agent-memory install --git-repo /path/to/repo
agent-memory land /path/to/repo --since 2026-01-01
agent-memory review
agent-memory connect --editor claude-code
```

`pipx install roune-agent-memory` works too. To try it without installing, run `uvx --from roune-agent-memory agent-memory setup`. Install the CLI before `agent-memory install`, which refuses a run that won't last.

agent-memory runs on macOS, Linux and WSL 2.

## More

- [README](https://github.com/roune/agent-memory/blob/main/README.md): how it works and the features.
- [Deploying](https://github.com/roune/agent-memory/blob/main/docs/deploy.md): one laptop, a team server, upgrades.
- [Editor hooks](https://github.com/roune/agent-memory/blob/main/docs/harnesses.md): Cursor, Claude Code, Codex and Pi.
- [Issues](https://github.com/roune/agent-memory/issues)

Noncommercial use is free under the [PolyForm Noncommercial License 1.0.0](https://github.com/roune/agent-memory/blob/main/LICENSE).
