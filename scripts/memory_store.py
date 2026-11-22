"""Laptop side of the approved-memory store.

The laptop file is the source of truth. Qdrant is only the vector index.
A second file on the container volume is the shared copy flush writes.

This module decides which SQLite file to open, from ~/.config/agent-memory and
AGENT_MEMORY_DIR, and hands it to store_core.Store. The schema and the queries
live in store_core.py.
"""

import sqlite3
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import store_core
from store_core import (  # noqa: F401  re-exported for callers of memory_store
    CONTENT_COLUMNS,
    COPY_COLUMNS,
    OPTIONAL_FIELDS,
    SCHEMA,
    SCHEMA_VERSION,
    Store,
    StoreError,
    commit_list,
    connect,
    document_from_row,
    hit_from_row,
    normalize_document,
    utc_now,
    _commits_by_id,
    _content,
    _pending_flush,
    _reject_blank,
    _sha,
    _text,
    _upsert_shared,
    _write_commits,
)

DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"


def repo_root():
    return Path(__file__).resolve().parents[1]


def store_root():
    import os

    override = os.environ.get("AGENT_MEMORY_DIR")
    return Path(override) if override else repo_root()


def config_dir():
    return Path.home() / ".config" / "agent-memory"


def _parse_lines(text, lower_keys):
    found = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        if lower_keys:
            key = key.lower().replace("-", "_")
        value = rest.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        found[key] = value
    return found


def database_config():
    path = config_dir() / "database"
    parsed = {}
    if path.is_file():
        parsed = _parse_lines(path.read_text(encoding="utf-8"), lower_keys=True)
    mode = (parsed.get("mode") or "shared").strip().lower().replace("_", "-")
    if mode not in {"shared", "per-repo"}:
        raise StoreError(f"{path} has mode {mode!r}; use shared or per-repo")
    url = (parsed.get("qdrant_url") or DEFAULT_QDRANT_URL).strip().rstrip("/")
    if not url:
        url = DEFAULT_QDRANT_URL
    return {"mode": mode, "qdrant_url": url, "path": path}


def remote_urls():
    path = config_dir() / "databases"
    if not path.is_file():
        return {}
    return _parse_lines(path.read_text(encoding="utf-8"), lower_keys=False)


def remote_dirname(remote_id):
    return remote_id.replace("/", "--").replace("\\", "--")


def per_repo_root(remote_id):
    return (
        Path.home()
        / ".local"
        / "share"
        / "agent-memory"
        / "databases"
        / remote_dirname(remote_id)
    )


def local_db_path(remote_id=None):
    if database_config()["mode"] == "shared":
        return store_root() / "data" / "memories.sqlite"
    if not remote_id:
        raise StoreError("per-repo mode needs a remote id")
    return per_repo_root(remote_id) / "memories.sqlite"


def shared_db_path(remote_id=None):
    if database_config()["mode"] == "shared":
        return store_root() / "data" / "shared" / "memories.sqlite"
    if not remote_id:
        raise StoreError("per-repo mode needs a remote id")
    return per_repo_root(remote_id) / "shared" / "memories.sqlite"


def qdrant_url(remote_id=None):
    """URL embed and flush use. Loopback when no company host is configured."""
    config = database_config()
    if config["mode"] == "shared":
        return config["qdrant_url"]
    if not remote_id:
        raise StoreError("per-repo mode needs a remote id")
    urls = remote_urls()
    url = (urls.get(remote_id) or "").strip().rstrip("/")
    if not url:
        path = config_dir() / "databases"
        raise StoreError(f"no Qdrant URL for {remote_id} in {path}")
    return url



def _selected(remote_id):
    """The remote id local_db_path needs: only in per-repo mode."""
    return remote_id if database_config()["mode"] == "per-repo" else None


def local_store(remote_id=None):
    """The Store for this laptop's file. Raises StoreError for per-repo mode without an id."""
    return Store(local_db_path(_selected(remote_id)))


def save_approved(document):
    """Insert or replace one approved memory. Does not touch the inbox or Qdrant."""
    return local_store(_text(document.get("repo"))).save_approved(document)


def memory_exists_for_commit(sha, remote_id=None):
    """True when a kept row explains this commit. False when there is no store to ask."""
    try:
        store = local_store(remote_id)
    except StoreError:
        return False
    return store.memory_exists_for_commit(sha)


def reject_commits(remote_id, shas):
    """Remember commits a person rejected in review, so land and hooks skip them."""
    return local_store(_text(remote_id)).reject_commits(remote_id, shas)


def commit_rejected(sha, remote_id=None):
    """True when this commit of this remote was rejected in review."""
    try:
        store = local_store(remote_id)
    except StoreError:
        return False
    return store.commit_rejected(sha, remote_id)


def query_memories(remote_id=None, **filters):
    """Metadata query on the laptop file. See store_core.Store.query_memories."""
    if database_config()["mode"] == "shared" and remote_id:
        raise ValueError("database is only used in per-repo mode")
    return local_store(remote_id).query_memories(**filters)


def get_memory(document_id, remote_id=None):
    """One approved memory by id, or None. Reads only."""
    return local_store(remote_id).get_memory(document_id)


def pending_documents(remote_id=None, repo_filter=None, rebuild=False):
    return local_store(remote_id).pending_documents(repo_filter=repo_filter, rebuild=rebuild)


def mark_embedded(pairs, remote_id=None):
    if not pairs:
        return
    local_store(remote_id).mark_embedded(pairs)


def container_ready(url):
    target = url.rstrip("/") + "/readyz"
    try:
        with urlopen(target, timeout=3) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def flush(remote_id=None):
    """Copy pending rows into the container SQLite file when /readyz answers.

    The laptop file is unchanged when the container is down.
    """
    mode = database_config()["mode"]
    selected = remote_id if mode == "per-repo" else None
    try:
        url = qdrant_url(selected)
        local_path = local_db_path(selected)
        shared_path = shared_db_path(selected)
    except StoreError as error:
        print(str(error))
        return 1
    if not container_ready(url):
        print(f"container unreachable at {url}")
        return 0
    if not local_path.is_file():
        print("no local memories")
        return 0
    local = connect(local_path)
    try:
        pending = _pending_flush(local)
        if not pending:
            print("nothing to flush")
            return 0
        shared = connect(shared_path)
        applied = []
        conflicts = []
        try:
            shared.execute("BEGIN IMMEDIATE")
            for row in pending:
                current = shared.execute(
                    f"SELECT {', '.join(COPY_COLUMNS)} FROM memory WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if current is None or current["updated_at"] < row["updated_at"]:
                    _upsert_shared(shared, row, _commits_by_id(local, [row["id"]])[row["id"]])
                    applied.append(row)
                elif _content(current) == _content(row):
                    applied.append(row)
                else:
                    conflicts.append(row["id"])
            shared.execute("COMMIT")
        except sqlite3.Error as error:
            if shared.in_transaction:
                shared.execute("ROLLBACK")
            raise StoreError(f"could not flush: {error}") from error
        finally:
            shared.close()
        local.execute("BEGIN IMMEDIATE")
        for row in applied:
            local.execute(
                """
                UPDATE memory
                SET flushed_at = updated_at
                WHERE id = ? AND updated_at = ?
                """,
                (row["id"], row["updated_at"]),
            )
        local.execute("COMMIT")
    except StoreError:
        raise
    finally:
        local.close()
    for document_id in conflicts:
        print(document_id)
    print(f"flushed {len(applied)}")
    return 0

