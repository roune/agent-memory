"""The server's memories.sqlite: store_core.Store plus what only the server does.

- write_memory: insert a new approved memory, or say it is an identical resend
  or a conflict. A kept memory is never edited through the API.
- memory_embedding: which rows each collection holds, so each collection knows
  what it is missing.
- embedding_state: the one active embedder and its collection.
- import_rows: the bulk upsert behind /v1/admin/import, newer updated_at wins.
- token: hashed bearer tokens, their scopes and identity (auth.py).

Standard library only.
"""

import json
import sqlite3
from datetime import datetime, timedelta

from store_core import (
    COPY_COLUMNS,
    CONTENT_COLUMNS,
    Store,
    StoreError,
    _commits_by_id,
    _sha,
    _text,
    _write_commits,
    document_from_row,
    hit_from_row,
    normalize_document,
    utc_now,
)

from agent_memory_server import auth

# Compared for an identical resend. superseded_by is left out: the server sets
# it on the old row when a newer memory supersedes it, so a resend of the old
# memory still matches.
_SAME_COLUMNS = tuple(name for name in CONTENT_COLUMNS if name != "superseded_by")

STATE_FIELDS = ("provider", "model", "revision", "dimension", "distance", "collection")


def _value(value):
    return "" if value is None else value


def later_than(stamp, previous):
    """stamp, or a microsecond after previous when stamp isn't later.

    Timestamps have one-second resolution, so a supersede in the same second
    as the old row's last write would leave its updated_at unchanged. Then an
    embed that read the old row just before could still mark its stale
    payload as current. A changed updated_at makes that mark miss.
    """
    if not previous or stamp > previous:
        return stamp
    try:
        return (datetime.fromisoformat(previous) + timedelta(microseconds=1)).isoformat()
    except ValueError:
        return stamp


class ServerStore(Store):
    def _transaction(self, work, message):
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = work(connection)
            connection.execute("COMMIT")
            return result
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise StoreError(f"{message}: {error}") from error
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def write_memory(self, document, written_by):
        """Keep a new approved memory. Returns (outcome, stored).

        outcome is "created", "unchanged" (an identical resend: nothing is
        written), or "conflict" (that id holds different content, which stays).
        stored is the row as it now is, in the shape get_memory returns, plus
        updated_at.
        """
        row = normalize_document(document, utc_now())

        def work(connection):
            current = connection.execute("SELECT * FROM memory WHERE id = ?", (row["id"],)).fetchone()
            if current is not None:
                commits = _commits_by_id(connection, [row["id"]])[row["id"]]
                same = commits == row["commits"] and all(
                    _value(current[name]) == _value(row[name]) for name in _SAME_COLUMNS
                )
                return ("unchanged" if same else "conflict"), current, commits
            columns = ", ".join((*COPY_COLUMNS, "written_by"))
            marks = ", ".join("?" for _ in range(len(COPY_COLUMNS) + 1))
            connection.execute(
                f"INSERT INTO memory ({columns}) VALUES ({marks})",
                [*(row[name] for name in COPY_COLUMNS), written_by],
            )
            _write_commits(connection, row["id"], row["commits"])
            old = None
            if row["supersedes"]:
                old = connection.execute(
                    "SELECT updated_at FROM memory WHERE id = ?", (row["supersedes"],)
                ).fetchone()
            if old is not None:
                connection.execute(
                    "UPDATE memory SET superseded_by = ?, updated_at = ? WHERE id = ?",
                    (row["id"], later_than(row["updated_at"], old["updated_at"]), row["supersedes"]),
                )
                # Its payload changed, so every collection embeds it again.
                connection.execute("DELETE FROM memory_embedding WHERE memory_id = ?", (row["supersedes"],))
            current = connection.execute("SELECT * FROM memory WHERE id = ?", (row["id"],)).fetchone()
            return "created", current, row["commits"]

        outcome, current, commits = self._transaction(work, "could not store memory")
        stored = hit_from_row(current, commits)
        stored["updated_at"] = current["updated_at"]
        return outcome, stored

    def import_rows(self, documents, rejections, written_by):
        """Upsert full rows that keep their id, person and updated_at. A newer updated_at wins.

        Returns {"written": n, "skipped": n, "rejections": n, "newer": [ids]}. newer lists
        the skipped rows the server holds at a later updated_at than the one sent,
        such as a row superseded since an earlier import, so migrate can tell
        them from rows that differ.
        """
        columns = ", ".join((*COPY_COLUMNS, "written_by"))
        marks = ", ".join("?" for _ in range(len(COPY_COLUMNS) + 1))
        assignments = ", ".join(
            f"{name}=excluded.{name}" for name in (*COPY_COLUMNS, "written_by") if name != "id"
        )
        sql = f"""
            INSERT INTO memory ({columns}) VALUES ({marks})
            ON CONFLICT(id) DO UPDATE SET {assignments}
            WHERE excluded.updated_at > memory.updated_at
        """

        def work(connection):
            written = 0
            newer = []
            for document in documents:
                row = normalize_document(document, _text(document.get("updated_at")))
                cursor = connection.execute(sql, [*(row[name] for name in COPY_COLUMNS), written_by])
                if cursor.rowcount:
                    _write_commits(connection, row["id"], row["commits"])
                    written += 1
                    continue
                held = connection.execute("SELECT updated_at FROM memory WHERE id = ?", (row["id"],)).fetchone()
                if held is not None and held[0] > row["updated_at"] and row["id"] not in newer:
                    newer.append(row["id"])
            added = 0
            for rejection in rejections:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO rejected_commit (repo, sha, rejected_at) VALUES (?, ?, ?)",
                    (_text(rejection["repo"]), _sha(rejection["sha"]), _text(rejection.get("rejected_at")) or utc_now()),
                )
                added += cursor.rowcount
            return {"written": written, "skipped": len(documents) - written, "rejections": added, "newer": newer}

        return self._transaction(work, "could not import")

    def pending_for(self, collection, limit=None):
        """Rows the collection lacks, or holds at an older updated_at, oldest first."""
        connection = self._open_existing()
        if connection is None:
            return []
        sql = """
            SELECT m.* FROM memory m
            LEFT JOIN memory_embedding e ON e.memory_id = m.id AND e.collection = ?
            WHERE e.memory_id IS NULL OR e.updated_at < m.updated_at
            ORDER BY m.updated_at, m.id
        """
        params = [collection]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            rows = connection.execute(sql, params).fetchall()
            commits = _commits_by_id(connection, [row["id"] for row in rows])
        finally:
            connection.close()
        return [document_from_row(row, commits[row["id"]]) for row in rows]

    def count_pending(self, collection):
        connection = self._open_existing()
        if connection is None:
            return 0
        try:
            return connection.execute(
                """
                SELECT count(*) FROM memory m
                LEFT JOIN memory_embedding e ON e.memory_id = m.id AND e.collection = ?
                WHERE e.memory_id IS NULL OR e.updated_at < m.updated_at
                """,
                (collection,),
            ).fetchone()[0]
        finally:
            connection.close()

    def mark_embedded_in(self, collection, pairs):
        """Record that the collection holds these rows, unless a row changed since it was read."""
        if not pairs:
            return

        def work(connection):
            for memory_id, updated_at in pairs:
                connection.execute(
                    """
                    INSERT INTO memory_embedding (memory_id, collection, updated_at)
                    SELECT id, ?, updated_at FROM memory WHERE id = ? AND updated_at = ?
                    ON CONFLICT(memory_id, collection) DO UPDATE SET updated_at = excluded.updated_at
                    """,
                    (collection, memory_id, updated_at),
                )

        self._transaction(work, "could not mark embedded")

    def is_embedded(self, memory_id, collection):
        connection = self._open_existing()
        if connection is None:
            return False
        try:
            found = connection.execute(
                """
                SELECT 1 FROM memory m JOIN memory_embedding e
                  ON e.memory_id = m.id AND e.collection = ? AND e.updated_at >= m.updated_at
                WHERE m.id = ?
                """,
                (collection, memory_id),
            ).fetchone()
        finally:
            connection.close()
        return found is not None

    def forget_collection(self, collection):
        """Drop the bookkeeping of a collection that was deleted from Qdrant."""
        self._transaction(
            lambda connection: connection.execute(
                "DELETE FROM memory_embedding WHERE collection = ?", (collection,)
            ),
            "could not forget collection",
        )

    def count_memories(self):
        connection = self._open_existing()
        if connection is None:
            return 0
        try:
            return connection.execute("SELECT count(*) FROM memory").fetchone()[0]
        finally:
            connection.close()

    def memories_by_ids(self, ids):
        """{id: row in get_memory's shape} for the ids that exist. Reads only.

        Search reads its hits back from here, so text and superseded_by come
        from SQLite, not from a point that may still be waiting to be embedded.
        """
        wanted = [memory_id for memory_id in dict.fromkeys(ids) if memory_id]
        if not wanted:
            return {}
        connection = self._open_existing()
        if connection is None:
            return {}
        try:
            marks = ", ".join("?" for _ in wanted)
            rows = connection.execute(f"SELECT * FROM memory WHERE id IN ({marks})", wanted).fetchall()
            commits = _commits_by_id(connection, [row["id"] for row in rows])
        finally:
            connection.close()
        return {row["id"]: hit_from_row(row, commits[row["id"]]) for row in rows}

    def embedding_state(self):
        """The active embedder and its collection, or None before the first one."""
        connection = self._open_existing()
        if connection is None:
            return None
        try:
            row = connection.execute("SELECT * FROM embedding_state WHERE id = 1").fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return {name: row[name] for name in (*STATE_FIELDS, "activated_at")}

    def set_embedding_state(self, description):
        """Make this embedder and collection the active one."""
        values = [description[name] for name in STATE_FIELDS]
        self._transaction(
            lambda connection: connection.execute(
                f"""
                INSERT INTO embedding_state (id, {", ".join(STATE_FIELDS)}, activated_at)
                VALUES (1, {", ".join("?" for _ in STATE_FIELDS)}, ?)
                ON CONFLICT(id) DO UPDATE SET
                  {", ".join(f"{name}=excluded.{name}" for name in STATE_FIELDS)},
                  activated_at=excluded.activated_at
                """,
                [*values, utc_now()],
            ),
            "could not record the embedder",
        )

    # Tokens. Secrets are never stored: only their sha256 (auth.hash_secret).

    def create_token(
        self,
        person,
        org,
        team,
        scopes,
        allowed_orgs=(),
        allowed_teams=(),
        label="",
        parent_id=None,
        same_person=False,
    ):
        """Mint a token. Returns (token id, secret); the secret is not kept.

        A top-level token for a person who already has an active one is
        refused unless same_person, so two people can't share a name by
        accident. A child with the label of an active sibling revokes it.
        """
        secret = auth.new_secret()
        token_id = auth.new_token_id()
        stamp = utc_now()

        def work(connection):
            if parent_id is None and not same_person:
                taken = connection.execute(
                    "SELECT id FROM token WHERE person = ? AND parent_id IS NULL AND revoked_at IS NULL LIMIT 1",
                    (person,),
                ).fetchone()
                if taken is not None:
                    raise auth.AuthError(
                        409,
                        f"{person} already has an active token ({taken['id']}). Pass --same-person if this is "
                        "the same person's second token, or choose another name.",
                    )
            if parent_id is not None and label:
                replaced = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM token WHERE parent_id = ? AND label = ? AND revoked_at IS NULL",
                        (parent_id, label),
                    )
                ]
                self._revoke_ids(connection, replaced, stamp)
            connection.execute(
                """
                INSERT INTO token (id, hash, parent_id, label, person, org, team,
                                   allowed_orgs, allowed_teams, scopes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    auth.hash_secret(secret),
                    parent_id,
                    label,
                    person,
                    org,
                    team,
                    json.dumps(list(allowed_orgs)),
                    json.dumps(list(allowed_teams)),
                    json.dumps(list(scopes)),
                    stamp,
                ),
            )

        self._transaction(work, "could not create the token")
        return token_id, secret

    @staticmethod
    def _revoke_ids(connection, ids, stamp):
        """Revoke these tokens and every descendant. Returns how many were active."""
        count = 0
        pending = list(ids)
        while pending:
            token_id = pending.pop()
            cursor = connection.execute(
                "UPDATE token SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (stamp, token_id)
            )
            count += cursor.rowcount
            pending.extend(row["id"] for row in connection.execute("SELECT id FROM token WHERE parent_id = ?", (token_id,)))
        return count

    def revoke_token(self, token_id):
        """Revoke a token and its children. Returns how many were revoked; 0 when none was active."""
        return self._transaction(
            lambda connection: self._revoke_ids(connection, [token_id], utc_now()), "could not revoke the token"
        )

    def token_for_secret(self, secret):
        """The active token with this secret, or None. A token whose parent is revoked is not active."""
        connection = self._open_existing()
        if connection is None:
            return None
        try:
            row = connection.execute(
                """
                SELECT t.* FROM token t LEFT JOIN token p ON p.id = t.parent_id
                WHERE t.hash = ? AND t.revoked_at IS NULL
                  AND (t.parent_id IS NULL OR (p.id IS NOT NULL AND p.revoked_at IS NULL))
                """,
                (auth.hash_secret(secret),),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else dict(row)

    def list_tokens(self):
        """Every token, oldest first, without hashes."""
        connection = self._open_existing()
        if connection is None:
            return []
        try:
            rows = connection.execute(
                """
                SELECT id, parent_id, label, person, org, team, allowed_orgs, allowed_teams,
                       scopes, created_at, revoked_at
                FROM token ORDER BY created_at, id
                """
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def count_active_tokens(self):
        connection = self._open_existing()
        if connection is None:
            return 0
        try:
            return connection.execute("SELECT count(*) FROM token WHERE revoked_at IS NULL").fetchone()[0]
        finally:
            connection.close()
