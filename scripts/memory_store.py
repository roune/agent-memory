"""Local SQLite store for approved memories.

The laptop file is the source of truth. Qdrant is only the vector index.
A second file on the container volume is the shared copy flush writes.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"

COPY_COLUMNS = (
    "id",
    "kind",
    "domain",
    "repo",
    "tool",
    "session_id",
    "text",
    "commit_sha",
    "org",
    "team",
    "person",
    "occurred_at",
    "record",
    "subject",
    "alarm",
    "look_at",
    "supersedes",
    "superseded_by",
    "updates",
    "updated_at",
)

CONTENT_COLUMNS = tuple(name for name in COPY_COLUMNS if name != "updated_at")

OPTIONAL_FIELDS = (
    "record",
    "subject",
    "alarm",
    "look_at",
    "supersedes",
    "superseded_by",
    "updates",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  domain TEXT NOT NULL,
  repo TEXT NOT NULL,
  tool TEXT NOT NULL,
  session_id TEXT NOT NULL,
  text TEXT NOT NULL,
  commit_sha TEXT NOT NULL DEFAULT '',
  org TEXT NOT NULL,
  team TEXT NOT NULL,
  person TEXT NOT NULL,
  occurred_at TEXT,
  record TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  alarm TEXT NOT NULL DEFAULT '',
  look_at TEXT NOT NULL DEFAULT '',
  supersedes TEXT NOT NULL DEFAULT '',
  superseded_by TEXT NOT NULL DEFAULT '',
  updates TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  embedded_at TEXT,
  flushed_at TEXT
);
CREATE INDEX IF NOT EXISTS memory_team_occurred ON memory (team, occurred_at);
CREATE INDEX IF NOT EXISTS memory_org_occurred ON memory (org, occurred_at);
CREATE INDEX IF NOT EXISTS memory_repo_occurred ON memory (repo, occurred_at);
CREATE INDEX IF NOT EXISTS memory_commit_sha ON memory (commit_sha);
"""


class StoreError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso_from_timestamp(stamp):
    return datetime.fromtimestamp(stamp, tz=timezone.utc).replace(microsecond=0).isoformat()


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


def connect(path, create=True):
    if not create and not path.is_file():
        raise FileNotFoundError(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA user_version=1")
    return connection


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize_document(document, updated_at):
    occurred = document.get("occurred_at")
    if isinstance(occurred, str) and occurred.strip():
        occurred_at = occurred.strip()
    elif occurred is None or occurred == "":
        occurred_at = None
    else:
        occurred_at = str(occurred).strip() or None
    row = {
        "id": _text(document.get("id")),
        "kind": _text(document.get("kind")),
        "domain": _text(document.get("domain")),
        "repo": _text(document.get("repo")),
        "tool": _text(document.get("tool")),
        "session_id": _text(document.get("session_id")),
        "text": _text(document.get("text")),
        "commit_sha": _text(document.get("commit") if "commit" in document else document.get("commit_sha")),
        "org": _text(document.get("org")),
        "team": _text(document.get("team")),
        "person": _text(document.get("person")),
        "occurred_at": occurred_at,
        "updated_at": updated_at,
    }
    for name in OPTIONAL_FIELDS:
        row[name] = _text(document.get(name))
    return row


def save_approved(document):
    """Insert or replace one approved memory. Does not touch the inbox or Qdrant."""
    remote_id = _text(document.get("repo"))
    try:
        path = local_db_path(remote_id if database_config()["mode"] == "per-repo" else None)
    except StoreError:
        raise
    updated_at = utc_now()
    row = normalize_document(document, updated_at)
    columns = ", ".join(COPY_COLUMNS)
    placeholders = ", ".join("?" for _ in COPY_COLUMNS)
    assignments = ", ".join(
        f"{name}=excluded.{name}" for name in COPY_COLUMNS if name != "id"
    )
    sql = f"""
        INSERT INTO memory ({columns}, embedded_at, flushed_at)
        VALUES ({placeholders}, NULL, NULL)
        ON CONFLICT(id) DO UPDATE SET
          {assignments},
          embedded_at=NULL,
          flushed_at=NULL
    """
    values = [row[name] for name in COPY_COLUMNS]
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(sql, values)
        if row["supersedes"]:
            connection.execute(
                "UPDATE memory SET superseded_by = ?, updated_at = ? WHERE id = ?",
                (row["id"], row["updated_at"], row["supersedes"]),
            )
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        connection.execute("ROLLBACK")
        raise StoreError(f"could not store memory: {error}") from error
    finally:
        connection.close()
    return row


def memory_exists_for_commit(sha, remote_id=None):
    needle = (sha or "").strip().lower()
    if len(needle) < 7:
        return False
    try:
        path = local_db_path(remote_id if database_config()["mode"] == "per-repo" else None)
    except StoreError:
        return False
    if not path.is_file():
        return False
    connection = connect(path, create=False)
    try:
        rows = connection.execute(
            "SELECT commit_sha FROM memory WHERE length(commit_sha) >= 7"
        ).fetchall()
    finally:
        connection.close()
    for stored_row in rows:
        stored = (stored_row["commit_sha"] or "").strip().lower()
        if len(stored) < 7:
            continue
        if needle.startswith(stored) or stored.startswith(needle):
            return True
    return False


def _reject_blank(name, value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} filter is blank")
    return text


def query_memories(
    remote_id=None,
    team=None,
    org=None,
    repo=None,
    person=None,
    since=None,
    until=None,
    kind=None,
    domain=None,
    tool=None,
    session_id=None,
    limit=20,
    offset=0,
):
    """Metadata query. Team, org, and repo each use their composite index.

    team and org together are an AND. Asking both populations is two calls.
    A missing occurred_at does not match since or until.
    """
    if database_config()["mode"] == "shared" and remote_id:
        raise ValueError("database is only used in per-repo mode")
    clauses = []
    params = []
    for name, value in (
        ("team", team),
        ("org", org),
        ("repo", repo),
        ("person", person),
        ("kind", kind),
        ("domain", domain),
        ("tool", tool),
        ("session_id", session_id),
    ):
        text = _reject_blank(name, value)
        if text is None:
            continue
        clauses.append(f"{name} = ?")
        params.append(text)
    since_text = _reject_blank("since", since)
    if since_text is not None:
        clauses.append("occurred_at >= ?")
        params.append(since_text)
    until_text = _reject_blank("until", until)
    if until_text is not None:
        clauses.append("occurred_at <= ?")
        params.append(until_text)
    limit = min(max(int(limit or 20), 1), 100)
    offset = max(int(offset or 0), 0)
    path = local_db_path(remote_id if database_config()["mode"] == "per-repo" else None)
    if not path.is_file():
        return []
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT id, kind, domain, repo, tool, session_id, commit_sha, text,
               org, team, person, occurred_at, record, subject, alarm, look_at,
               supersedes, superseded_by, updates
        FROM memory
        {where}
        ORDER BY occurred_at DESC
        LIMIT ? OFFSET ?
    """
    connection = connect(path, create=False)
    try:
        rows = connection.execute(sql, [*params, limit, offset]).fetchall()
    finally:
        connection.close()
    return [hit_from_row(row) for row in rows]


def hit_from_row(row):
    hit = {
        "document_id": row["id"],
        "kind": row["kind"],
        "domain": row["domain"],
        "repo": row["repo"],
        "tool": row["tool"],
        "session_id": row["session_id"],
        "commit": row["commit_sha"] or "",
        "text": row["text"],
        "org": row["org"] or "",
        "team": row["team"] or "",
        "person": row["person"] or "",
        "occurred_at": row["occurred_at"],
    }
    for name in OPTIONAL_FIELDS:
        value = row[name]
        if value:
            hit[name] = value
    return hit


def document_from_row(row):
    """Shape scripts/embed.py already upserts, plus updated_at for the bookkeeping."""
    document = {
        "id": row["id"],
        "kind": row["kind"],
        "domain": row["domain"],
        "repo": row["repo"],
        "tool": row["tool"],
        "session_id": row["session_id"],
        "text": row["text"],
        "commit": row["commit_sha"] or "",
        "org": row["org"] or "",
        "team": row["team"] or "",
        "person": row["person"] or "",
        "updated_at": row["updated_at"],
    }
    if row["occurred_at"]:
        document["occurred_at"] = row["occurred_at"]
    for name in OPTIONAL_FIELDS:
        if row[name]:
            document[name] = row[name]
    return document


def pending_documents(remote_id=None, repo_filter=None, rebuild=False):
    mode = database_config()["mode"]
    path = local_db_path(remote_id if mode == "per-repo" else None)
    if not path.is_file():
        return []
    clauses = []
    params = []
    if not rebuild:
        clauses.append("(embedded_at IS NULL OR embedded_at < updated_at)")
    if repo_filter:
        clauses.append("repo = ?")
        params.append(repo_filter)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    connection = connect(path, create=False)
    try:
        rows = connection.execute(f"SELECT * FROM memory{where}", params).fetchall()
    finally:
        connection.close()
    return [document_from_row(row) for row in rows]


def mark_embedded(pairs, remote_id=None):
    if not pairs:
        return
    path = local_db_path(remote_id if database_config()["mode"] == "per-repo" else None)
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for document_id, updated_at in pairs:
            connection.execute(
                """
                UPDATE memory
                SET embedded_at = updated_at
                WHERE id = ? AND updated_at = ?
                """,
                (document_id, updated_at),
            )
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        connection.execute("ROLLBACK")
        raise StoreError(f"could not mark embedded: {error}") from error
    finally:
        connection.close()


def container_ready(url):
    target = url.rstrip("/") + "/readyz"
    try:
        with urlopen(target, timeout=3) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _content(row):
    values = []
    for name in CONTENT_COLUMNS:
        value = row[name]
        values.append("" if value is None else value)
    return tuple(values)


def _pending_flush(connection):
    return connection.execute(
        f"""
        SELECT {", ".join(COPY_COLUMNS)}
        FROM memory
        WHERE flushed_at IS NULL OR flushed_at < updated_at
        """
    ).fetchall()


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
                    _upsert_shared(shared, row)
                    applied.append(row)
                elif _content(current) == _content(row):
                    applied.append(row)
                else:
                    conflicts.append(row["id"])
            shared.execute("COMMIT")
        except sqlite3.Error as error:
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


def _upsert_shared(connection, row):
    columns = ", ".join(COPY_COLUMNS)
    placeholders = ", ".join("?" for _ in COPY_COLUMNS)
    assignments = ", ".join(f"{name}=excluded.{name}" for name in COPY_COLUMNS if name != "id")
    connection.execute(
        f"""
        INSERT INTO memory ({columns})
        VALUES ({placeholders})
        ON CONFLICT(id) DO UPDATE SET {assignments}
        """,
        [row[name] for name in COPY_COLUMNS],
    )


def memories_json_dir():
    return store_root() / "memories"


def import_json():
    """Load memories/*.json once. Existing ids stay as the SQL row."""
    source = memories_json_dir()
    if not source.is_dir():
        print("no memories directory")
        return 0
    files = sorted(source.glob("*.json"))
    if not files:
        print("no json files")
        return 0
    mode = database_config()["mode"]
    grouped = {}
    skipped = []
    notes = []
    for path in files:
        try:
            import json

            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            skipped.append(path.name)
            continue
        if not isinstance(document, dict):
            skipped.append(path.name)
            continue
        missing = [
            field
            for field in ("id", "kind", "domain", "repo", "tool", "session_id", "text")
            if document.get(field) is None or (
                isinstance(document.get(field), str) and not document.get(field).strip()
            )
        ]
        if missing:
            skipped.append(path.name)
            continue
        if document["kind"] not in {"change", "learning", "decision"}:
            skipped.append(path.name)
            continue
        if document["domain"] not in {"code", "architecture", "operations"}:
            skipped.append(path.name)
            continue
        remote_id = str(document["repo"]).strip()
        if mode == "per-repo" and remote_id not in remote_urls():
            skipped.append(path.name)
            notes.append(f"{path.name}: no database configured for {remote_id}")
            continue
        if path.stem != str(document["id"]).strip():
            notes.append(f"{path.name}: id is {document['id']}")
        occurred = document.get("occurred_at")
        if isinstance(occurred, str) and occurred.strip():
            updated_at = occurred.strip()
        else:
            updated_at = iso_from_timestamp(path.stat().st_mtime)
            document["occurred_at"] = None
        row = normalize_document(document, updated_at)
        key = remote_id if mode == "per-repo" else ""
        grouped.setdefault(key, []).append((path.name, row))

    for note in notes:
        print(note)
    for name in skipped:
        if not any(note.startswith(name + ":") for note in notes):
            print(f"skipped {name}")

    for key, items in grouped.items():
        remote_id = key if mode == "per-repo" else None
        path = local_db_path(remote_id)
        connection = connect(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = 0
            kept = 0
            for _name, row in items:
                existing = connection.execute(
                    "SELECT id FROM memory WHERE id = ?",
                    (row["id"],),
                ).fetchone()
                if existing is not None:
                    kept += 1
                    continue
                columns = ", ".join(COPY_COLUMNS)
                placeholders = ", ".join("?" for _ in COPY_COLUMNS)
                connection.execute(
                    f"INSERT INTO memory ({columns}) VALUES ({placeholders})",
                    [row[name] for name in COPY_COLUMNS],
                )
                inserted += 1
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise StoreError(f"could not import: {error}") from error
        finally:
            connection.close()
        print(f"imported {inserted}, kept {kept}")

    if skipped:
        return 0
    imported = store_root() / "memories.imported"
    if imported.exists():
        print(f"{imported} already exists; left {source}")
        return 0
    source.rename(imported)
    print(f"renamed {source.name} to {imported.name}")
    return 0
