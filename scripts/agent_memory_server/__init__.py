"""agent-memory-server: the server that runs in Docker next to Qdrant.

It owns memories.sqlite, takes approved memories over HTTP, and embeds them on
write. docs/server-plan.md describes it. This module only checks that the
server extra is installed, so a plain `uv tool install` of the CLI gets a clear
hint instead of an ImportError, then hands over to cli.py.

Modules:
  settings   environment variables
  store      the server's queries on top of store_core
  qdrant     a small Qdrant HTTP client with an injectable transport
  embedding  collection names, the startup checks, and embed-on-write
  reindex    building a collection for the configured embedder
  rules      request bodies -> documents, with memory_rules
  app        the Starlette app and the browser rules (needs the extra)
  cli        `serve` and `reindex`
"""

import importlib.metadata
import importlib.util
import sys
import tomllib
from pathlib import Path

# The name on PyPI. This package can't import agent_memory, so it keeps its own
# copy of agent_memory.DIST; a test checks that the two and pyproject agree.
DIST = "roune-agent-memory"

REQUIRED = ("mcp", "fastembed")
INSTALL_HINT = (
    "this command needs the server extra: "
    f"uv tool install '{DIST}[server] @ git+https://github.com/roune/agent-memory'"
)


def package_version():
    """The version of this package: its metadata, or pyproject.toml in a clone with no install."""
    try:
        return importlib.metadata.version(DIST)
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        with open(Path(__file__).resolve().parents[2] / "pyproject.toml", "rb") as handle:
            return tomllib.load(handle)["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "unknown"


def missing_modules(find_spec=importlib.util.find_spec):
    """The server's dependencies that can't be imported here."""
    return [name for name in REQUIRED if find_spec(name) is None]


def main(argv=None, find_spec=importlib.util.find_spec):
    missing = missing_modules(find_spec)
    if missing:
        print(f"agent-memory-server: {', '.join(missing)} not installed; {INSTALL_HINT}", file=sys.stderr)
        return 2
    from agent_memory_server import cli

    return cli.run(argv, setup_logging=True)


if __name__ == "__main__":
    raise SystemExit(main())
