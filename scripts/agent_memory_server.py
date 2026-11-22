"""agent-memory-server: the server that runs in Docker next to Qdrant.

docs/server-plan.md builds it in PR 2b. For now this command only checks that
the server extra is installed, so a plain `uv tool install` of the CLI gets a
clear hint instead of an ImportError.
"""

import importlib.util
import sys

REQUIRED = ("mcp", "fastembed")
INSTALL_HINT = (
    "this command needs the server extra: "
    "uv tool install 'agent-memory[server] @ git+https://github.com/roune/agent-memory'"
)


def missing_modules(find_spec=importlib.util.find_spec):
    """The server's dependencies that can't be imported here."""
    return [name for name in REQUIRED if find_spec(name) is None]


def main(argv=None, find_spec=importlib.util.find_spec):
    missing = missing_modules(find_spec)
    if missing:
        print(f"agent-memory-server: {', '.join(missing)} not installed; {INSTALL_HINT}", file=sys.stderr)
        return 2
    print("agent-memory-server: the server is not built yet (docs/server-plan.md, PR 2b)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
