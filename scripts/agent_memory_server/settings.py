"""The server's settings, all from environment variables. Standard library only.

AGENT_MEMORY_SERVER_DATA      directory for memories.sqlite and server.lock (/data)
AGENT_MEMORY_MODELS_DIR       where a local model's checkpoint is (<data>/models;
                              the image sets /opt/agent-memory/models)
AGENT_MEMORY_QDRANT_URL       Qdrant's URL (http://qdrant:6333, the compose network)
AGENT_MEMORY_QDRANT_API_KEY   sent as api-key when Qdrant has one (optional)
AGENT_MEMORY_AUTH             "off" turns auth off (local compose only); anything
                              else, or unset, means bearer tokens (auth.py)
AGENT_MEMORY_ALLOWED_HOSTS    Host headers to answer (127.0.0.1:8765,localhost:8765)
AGENT_MEMORY_ALLOWED_ORIGINS  browser Origins to answer (none)
AGENT_MEMORY_EMBED_RETRY      seconds between embed retries (30)

The embedder comes from AGENT_MEMORY_EMBEDDER and AGENT_MEMORY_EMBED_MODEL,
read by embedder.config_from_env().
"""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOSTS = ("127.0.0.1:8765", "localhost:8765")


def _csv(value, default=()):
    if value is None:
        return tuple(default)
    return tuple(item.strip().lower() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    models_dir: Path
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    auth: str = "on"
    allowed_hosts: tuple = DEFAULT_HOSTS
    allowed_origins: tuple = ()
    retry_seconds: float = 30.0

    @property
    def db_path(self):
        return self.data_dir / "memories.sqlite"

    @property
    def lock_path(self):
        return self.data_dir / "server.lock"

    @property
    def auth_off(self):
        return self.auth == "off"

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        data_dir = Path(environ.get("AGENT_MEMORY_SERVER_DATA") or "/data")
        models = environ.get("AGENT_MEMORY_MODELS_DIR")
        retry = environ.get("AGENT_MEMORY_EMBED_RETRY")
        return cls(
            data_dir=data_dir,
            models_dir=Path(models) if models else data_dir / "models",
            qdrant_url=(environ.get("AGENT_MEMORY_QDRANT_URL") or "http://qdrant:6333").rstrip("/"),
            qdrant_api_key=environ.get("AGENT_MEMORY_QDRANT_API_KEY") or "",
            auth=(environ.get("AGENT_MEMORY_AUTH") or "on").strip().lower(),
            allowed_hosts=_csv(environ.get("AGENT_MEMORY_ALLOWED_HOSTS"), DEFAULT_HOSTS),
            allowed_origins=_csv(environ.get("AGENT_MEMORY_ALLOWED_ORIGINS")),
            retry_seconds=float(retry) if retry else 30.0,
        )
