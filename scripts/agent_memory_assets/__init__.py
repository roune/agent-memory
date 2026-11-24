"""Files the installed CLI carries, so it needs no clone.

`agent-memory setup` writes a compose.yaml for a person who installed the CLI
with uv or pipx and has no checkout. The wheel can only hold files inside a
package, and the root compose.yaml sits outside every package, so this folder
holds a copy. scripts/release-check fails unless the copy is byte-for-byte the
root file and pins the same image version.

The pin's pattern lives here too, because both read it: release-check, to
check a release, and setup, to learn which version a compose.yaml runs.
"""

import re
from importlib import resources

COMPOSE = "compose.yaml"
IMAGE = "ghcr.io/roune/agent-memory-server"
# image: ${AGENT_MEMORY_IMAGE:-ghcr.io/roune/agent-memory-server:0.1.0}
PIN = re.compile(r"^\s*image:\s*\$\{AGENT_MEMORY_IMAGE:-" + re.escape(IMAGE) + r":([^}\s]+)\}\s*$", re.MULTILINE)


def compose_bytes():
    # Bytes, not text: setup writes and hashes exactly these, and decoding can change newlines.
    return resources.files(__package__).joinpath(COMPOSE).read_bytes()


def compose_text():
    return compose_bytes().decode("utf-8")


def pins(text):
    """The versions a compose file pins the server image at, in file order."""
    return PIN.findall(text)
