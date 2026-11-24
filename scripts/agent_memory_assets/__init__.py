"""Files the installed CLI carries, so it needs no clone.

`agent-memory setup` writes a compose.yaml for a person who installed the CLI
with uv or pipx and has no checkout. The wheel can only hold files inside a
package, and the root compose.yaml sits outside every package, so this folder
holds a copy. scripts/release-check fails unless the copy is byte-for-byte the
root file and pins the same image version.
"""

from importlib import resources

COMPOSE = "compose.yaml"


def compose_text():
    return resources.files(__package__).joinpath(COMPOSE).read_text(encoding="utf-8")
