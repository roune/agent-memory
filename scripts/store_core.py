"""The approved-memory store, on one SQLite file at a path you give it.

This module knows nothing about ~/.config, per-repo mode, or Qdrant. The
laptop's memory_store.py resolves which file to open and calls Store. The
server opens its own file with the same class, so both use one schema and one
set of queries.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

COPY_COLUMNS = (
    "id",
    "kind",
    "domain",
    "repo",
    "tool",
    "session_id",
    "text",
    "commit_sha",
    "pull_request",
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
  pull_request INTEGER,
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
  flushed_at TEXT,
  written_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS memory_team_occurred ON memory (team, occurred_at);
CREATE INDEX IF NOT EXISTS memory_org_occurred ON memory (org, occurred_at);
CREATE INDEX IF NOT EXISTS memory_repo_occurred ON memory (repo, occurred_at);
CREATE INDEX IF NOT EXISTS memory_commit_sha ON memory (commit_sha);
CREATE TABLE IF NOT EXISTS memory_commit (
  memory_id TEXT NOT NULL,
  sha TEXT NOT NULL,
  position INTEGER NOT NULL,
  PRIMARY KEY (memory_id, sha)
);
CREATE INDEX IF NOT EXISTS memory_commit_by_sha ON memory_commit (sha);
CREATE TABLE IF NOT EXISTS rejected_commit (
  repo TEXT NOT NULL,
  sha TEXT NOT NULL,
  rejected_at TEXT NOT NULL,
  PRIMARY KEY (repo, sha)
);
CREATE TABLE IF NOT EXISTS embedding_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  revision TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  distance TEXT NOT NULL,
  collection TEXT NOT NULL,
  activated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_embedding (
  memory_id TEXT NOT NULL,
  collection TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (memory_id, collection)
);
CREATE INDEX IF NOT EXISTS memory_embedding_by_collection ON memory_embedding (collection);
"""


# 2: memory.pull_request and memory_commit.
# 3: memory.written_by, embedding_state and memory_embedding, for the server.
SCHEMA_VERSION = 3


class StoreError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path, create=True):
    if not create and not path.is_file():
        raise FileNotFoundError(path)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    _migrate(connection)
    return connection


def _migrate(connection):
    """Bring an older file up to date. Safe to run every time.

    Version 2 adds memory.pull_request and the memory_commit table, filled from
    each row's commit_sha. Version 3 adds memory.written_by; its tables come
    from SCHEMA. The laptop never writes them, so a v3 file still works with
    the v2 code.
    """
    if connection.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(memory)")}
            if version < 2:
                if "pull_request" not in columns:
                    connection.execute("ALTER TABLE memory ADD COLUMN pull_request INTEGER")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO memory_commit (memory_id, sha, position)
                    SELECT id, lower(trim(commit_sha)), 0 FROM memory
                    WHERE length(trim(commit_sha)) >= 7
                    """
                )
            if "written_by" not in columns:
                connection.execute("ALTER TABLE memory ADD COLUMN written_by TEXT NOT NULL DEFAULT ''")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except sqlite3.Error:
        connection.execute("ROLLBACK")
        raise


_HEX = frozenset("0123456789abcdef")


def _sha(value):
    text = _text(value).lower()
    if len(text) < 7 or any(char not in _HEX for char in text):
        return ""
    return text


def commit_list(document):
    """Every commit a document explains, oldest first, tip last.

    `commits` wins. A document with only `commit` (or `commit_sha`) has that one.
    """
    raw = document.get("commits")
    items = list(raw) if isinstance(raw, (list, tuple)) else []
    if not items:
        items = [document.get("commit") if "commit" in document else document.get("commit_sha")]
    found = []
    for item in items:
        sha = _sha(item)
        if sha and sha not in found:
            found.append(sha)
    return found


def _pull_request(value):
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _write_commits(connection, memory_id, commits):
    connection.execute("DELETE FROM memory_commit WHERE memory_id = ?", (memory_id,))
    connection.executemany(
        "INSERT INTO memory_commit (memory_id, sha, position) VALUES (?, ?, ?)",
        [(memory_id, sha, position) for position, sha in enumerate(commits)],
    )


def _commits_by_id(connection, ids):
    found = {memory_id: [] for memory_id in ids}
    if not found:
        return found
    for start in range(0, len(ids), 500):
        chunk = list(ids[start:start + 500])
        marks = ", ".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT memory_id, sha FROM memory_commit
            WHERE memory_id IN ({marks})
            ORDER BY memory_id, position
            """,
            chunk,
        ).fetchall()
        for row in rows:
            found[row["memory_id"]].append(row["sha"])
    return found


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
        "pull_request": _pull_request(document.get("pull_request")),
        "org": _text(document.get("org")),
        "team": _text(document.get("team")),
        "person": _text(document.get("person")),
        "occurred_at": occurred_at,
        "updated_at": updated_at,
    }
    for name in OPTIONAL_FIELDS:
        row[name] = _text(document.get(name))
    row["commits"] = commit_list(document)
    if not row["commit_sha"] and row["commits"]:
        row["commit_sha"] = row["commits"][-1]
    return row


def _reject_blank(name, value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} filter is blank")
    return text


def hit_from_row(row, commits=None):
    hit = {
        "document_id": row["id"],
        "kind": row["kind"],
        "domain": row["domain"],
        "repo": row["repo"],
        "tool": row["tool"],
        "session_id": row["session_id"],
        "commit": row["commit_sha"] or "",
        "commits": list(commits or []),
        "pull_request": row["pull_request"],
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


def document_from_row(row, commits=None):
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
        "commits": list(commits or []),
        "org": row["org"] or "",
        "team": row["team"] or "",
        "person": row["person"] or "",
        "updated_at": row["updated_at"],
    }
    if row["pull_request"] is not None:
        document["pull_request"] = row["pull_request"]
    if row["occurred_at"]:
        document["occurred_at"] = row["occurred_at"]
    for name in OPTIONAL_FIELDS:
        if row[name]:
            document[name] = row[name]
    return document


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


def _upsert_shared(connection, row, commits):
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
    _write_commits(connection, row["id"], commits)


class Store:
    """One memories.sqlite file. Reads of a missing file return nothing; writes create it."""

    def __init__(self, path):
        self.path = Path(path)

    def _open(self):
        return connect(self.path)

    def _open_existing(self):
        """A connection, or None when the file is missing."""
        if not self.path.is_file():
            return None
        return connect(self.path, create=False)

    def save_approved(self, document):
        """Insert or replace one approved memory. Does not touch the inbox or Qdrant."""
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
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(sql, values)
            _write_commits(connection, row["id"], row["commits"])
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

    def memory_exists_for_commit(self, sha):
        """True when a kept row explains this commit.

        A stored abbreviated hash matches the full id, and the other way around.
        Both lookups use the memory_commit index.
        """
        return self._commit_in("memory_commit", sha)

    def commit_rejected(self, sha, repo):
        """True when this commit of this remote was rejected in review."""
        return self._commit_in("rejected_commit", sha, repo=_text(repo))

    def _commit_in(self, table, sha, repo=None):
        """True when `table` has this commit, matching abbreviated hashes either way."""
        needle = _sha(sha)
        if not needle:
            return False
        connection = self._open_existing()
        if connection is None:
            return False
        prefixes = [needle[:size] for size in range(7, len(needle) + 1)]
        marks = ", ".join("?" for _ in prefixes)
        where_repo = "" if repo is None else "repo = ? AND "
        params = ([] if repo is None else [repo]) + [*prefixes, needle, needle + "g"]
        try:
            # Every hex string that starts with needle sorts between needle and
            # needle + "g", because "g" is above every hex digit.
            found = connection.execute(
                f"""
                SELECT 1 FROM {table}
                WHERE {where_repo}(sha IN ({marks}) OR (sha > ? AND sha < ?))
                LIMIT 1
                """,
                params,
            ).fetchone()
        finally:
            connection.close()
        return found is not None

    def reject_commits(self, repo, shas):
        """Remember commits a person rejected in review, so land and hooks skip them."""
        repo = _text(repo)
        found = [sha for sha in (_sha(item) for item in shas) if sha]
        if not found:
            return 0
        stamp = utc_now()
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT OR REPLACE INTO rejected_commit (repo, sha, rejected_at) VALUES (?, ?, ?)",
                [(repo, sha, stamp) for sha in found],
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            connection.execute("ROLLBACK")
            raise StoreError(f"could not record rejection: {error}") from error
        finally:
            connection.close()
        return len(found)

    def query_memories(
        self,
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
        connection = self._open_existing()
        if connection is None:
            return []
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT id, kind, domain, repo, tool, session_id, commit_sha, pull_request,
                   text, org, team, person, occurred_at, record, subject, alarm,
                   look_at, supersedes, superseded_by, updates
            FROM memory
            {where}
            ORDER BY occurred_at DESC
            LIMIT ? OFFSET ?
        """
        try:
            rows = connection.execute(sql, [*params, limit, offset]).fetchall()
            commits = _commits_by_id(connection, [row["id"] for row in rows])
        finally:
            connection.close()
        return [hit_from_row(row, commits[row["id"]]) for row in rows]

    def get_memory(self, document_id):
        """One approved memory by id, in the shape query_memories returns.

        None when the file or the row is missing. Reads only.
        """
        wanted = _reject_blank("document_id", document_id)
        if wanted is None:
            raise ValueError("document_id is required")
        connection = self._open_existing()
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM memory WHERE id = ?", (wanted,)).fetchone()
            if row is None:
                return None
            commits = _commits_by_id(connection, [row["id"]])[row["id"]]
        finally:
            connection.close()
        return hit_from_row(row, commits)

    def pending_documents(self, repo_filter=None, rebuild=False):
        """Rows to embed: changed since they were last embedded, or all of them with rebuild."""
        clauses = []
        params = []
        if not rebuild:
            clauses.append("(embedded_at IS NULL OR embedded_at < updated_at)")
        if repo_filter:
            clauses.append("repo = ?")
            params.append(repo_filter)
        connection = self._open_existing()
        if connection is None:
            return []
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            rows = connection.execute(f"SELECT * FROM memory{where}", params).fetchall()
            commits = _commits_by_id(connection, [row["id"] for row in rows])
        finally:
            connection.close()
        return [document_from_row(row, commits[row["id"]]) for row in rows]

    def mark_embedded(self, pairs):
        """Stamp embedded_at, but only on rows that have not changed since they were read."""
        if not pairs:
            return
        connection = self._open()
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
