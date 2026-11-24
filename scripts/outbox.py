"""The outbox: approved documents and rejections waiting for the server.

Standard library only, like the rest of the CLI.

Each item is one JSON file in outbox/, next to inbox/, named
<utc stamp>-<uuid>.json, so sorting the names gives the order they were queued
in. An item is {server_url, op, body, created_at, attempts, last_error}. body is
the request exactly as it would have been sent: approved text and tags, never a
transcript path. Files are written with mode 0600.

sync sends the queue in order. What each answer does:

- 2xx: the item is deleted.
- No answer, or a 5xx: stop for that server, keep the item and every later one,
  and record attempts and last_error. A server that is down costs one timeout.
- 401, 403, or a redirect: stop for that server and keep its whole queue.
  `agent-memory connect` fixes the token or the URL; nothing approved is set aside.
  A 403 with code "identity" is about that one item (a person, org or team the
  token may not write as), so it goes to failed/ like another 4xx.
- 409: move the item to outbox/failed/ with the stored text alongside. A
  resend gets 409 again, because a kept memory is never edited, so resolve()
  settles it: supersede sends the queued text as a new memory that supersedes
  the stored one, and drop keeps the stored one.
- Any other 4xx: move the item to outbox/failed/ with the error.

Nothing leaves outbox/failed/ on its own: the approved text stays there until
the person picks supersede or drop, or a retry gets through.

Items for another server keep going when one server stops. A lock file makes
sure only one sync runs at a time; a second one waits, then finds the queue
already sent.
"""

import contextlib
import fcntl
import json
import os
import uuid
from datetime import datetime, timezone

import memory_rules
import server_client

OPS = {"memory": "/v1/memories", "rejection": "/v1/rejections"}
# Stop for that server and keep its queue: a token or URL to fix, not a bad item.
KEEP_QUEUE = frozenset({401, 403})


def root(store_root):
    return store_root / "outbox"


def failed_root(store_root):
    return root(store_root) / "failed"


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_private(path, value):
    """Write JSON with mode 0600, atomically, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2) + "\n")
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def add(store_root, server_url, op, body, unless_settled=None):
    """Queue one request. Returns the item's path.

    unless_settled, {"repo", "shas"}, makes sync ask /v1/commits/status first
    and drop the item when the server reports one of those commits kept or
    rejected. A hook that must not wait on the network leaves that check to sync.
    """
    if op not in OPS:
        raise ValueError(f"unknown outbox op {op!r}")
    item = {
        "server_url": server_client.clean_url(server_url),
        "op": op,
        "body": body,
        "created_at": _now(),
        "attempts": 0,
        "last_error": None,
    }
    if unless_settled:
        item["unless_settled"] = {"repo": unless_settled["repo"], "shas": list(unless_settled["shas"])}
    path = root(store_root) / f"{_stamp()}-{uuid.uuid4().hex}.json"
    write_private(path, item)
    return path


def _items(folder):
    """(path, item or None) for every item in folder, oldest first. None is a file that isn't JSON."""
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            item = None
        if item is not None and not isinstance(item, dict):
            item = None
        found.append((path, item))
    return found


def queued(store_root):
    return _items(root(store_root))


def failed(store_root):
    return _items(failed_root(store_root))


def age(created_at, now=None):
    """'3 min', '2 h', '4 days': how long ago created_at was. '' when it isn't a time."""
    try:
        then = datetime.fromisoformat(str(created_at))
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = max(0, int(((now or datetime.now(timezone.utc)) - then).total_seconds()))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{seconds // 60} min"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def summary(store_root):
    """{"queued": N, "oldest": age of the oldest queued item, "failed": M}."""
    items = queued(store_root)
    oldest = ""
    for _, item in items:
        if item is not None:
            oldest = age(item.get("created_at"))
            break
    return {"queued": len(items), "oldest": oldest, "failed": len(failed(store_root))}


@contextlib.contextmanager
def lock(store_root):
    """Only one sync at a time. A second caller waits for the first to finish."""
    folder = root(store_root)
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / ".lock", "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def settled_already(client, item):
    """True when the item's unless_settled commits are kept or rejected on the server."""
    check = item.get("unless_settled")
    if not isinstance(check, dict) or not check.get("repo") or not check.get("shas"):
        return False
    answer = client.commits_status(check["repo"], check["shas"])
    return any(isinstance(info, dict) and (info.get("kept") or info.get("rejected")) for info in answer.values())


def send(client, item):
    """Send one item. Returns the server's JSON, or raises ServerError / ServerUnreachable."""
    return client.request("POST", OPS[item["op"]], item["body"])


def _keeps_queue(error):
    """True when this error means: stop for this server and keep its queue."""
    if isinstance(error, server_client.ServerUnreachable):
        return True
    status = error.status
    if status == 403 and error.body.get("code") == "identity":
        return False
    return status >= 500 or status in KEEP_QUEUE or 300 <= status < 400


def _move_to_failed(store_root, path, item, error):
    record = dict(item)
    record["failed_at"] = _now()
    record["status"] = error.status
    record["error"] = error.message
    if error.status == 409:
        record["stored"] = (error.body.get("stored") or {}).get("text")
        record["stored_id"] = error.body.get("id")
    write_private(failed_root(store_root) / path.name, record)
    path.unlink()


class Report:
    """What one sync did."""

    def __init__(self):
        self.sent = 0
        self.settled = 0  # dropped: the server had already settled their commits
        self.failed = []  # (item, status, message)
        self.stopped = {}  # server url -> the error that stopped it

    def lines(self, store_root):
        out = []
        if self.sent:
            out.append(f"sent {self.sent}")
        if self.settled:
            out.append(f"dropped {self.settled}: the server already kept or rejected their commits")
        for item, status, message in self.failed:
            out.append(f"failed ({status}): {message}. Kept in {failed_root(store_root)}")
        for url, error in self.stopped.items():
            out.append(f"stopped for {url}: {server_client.explain(error)}")
        left = summary(store_root)
        if left["queued"]:
            out.append(f"{left['queued']} waiting, the oldest for {left['oldest']}")
        if left["failed"]:
            out.append(f"{left['failed']} failed: {RESOLVE_HINT}")
        return out or ["nothing to send"]


RESOLVE_HINT = (
    "agent-memory sync lists each one. For a 409, choose with "
    "agent-memory sync --resolve <item> supersede|drop; for another error, fix the cause, "
    "then run agent-memory sync --retry-failed"
)


def drain(store_root, client_for, report=None):
    """Send the queue in order. The caller holds the lock. Returns the Report.

    client_for(url) returns a server_client.Client for that url.
    """
    report = report or Report()
    while True:
        progressed = False
        for path, item in queued(store_root):
            if not path.exists():
                continue
            if item is None or item.get("op") not in OPS or not item.get("server_url"):
                bad = server_client.ServerError(400, "the outbox item is not one agent-memory wrote")
                _move_to_failed(store_root, path, item or {"raw": path.read_text(encoding="utf-8", errors="replace")}, bad)
                report.failed.append((item, 400, bad.message))
                continue
            url = item["server_url"]
            if url in report.stopped:
                continue
            try:
                client = client_for(url)
                if settled_already(client, item):
                    path.unlink()
                    report.settled += 1
                    progressed = True
                    continue
                send(client, item)
            except (server_client.ServerError, server_client.ServerUnreachable) as error:
                if _keeps_queue(error):
                    item["attempts"] = int(item.get("attempts") or 0) + 1
                    item["last_error"] = server_client.explain(error)
                    item["last_attempt_at"] = _now()
                    write_private(path, item)
                    report.stopped[url] = error
                    continue
                _move_to_failed(store_root, path, item, error)
                report.failed.append((item, error.status, error.message))
                progressed = True
                continue
            path.unlink()
            report.sent += 1
            progressed = True
        # Approve or a hook may queue more while this runs: go round again until
        # a pass sends nothing.
        if not progressed:
            return report


def retry_failed(store_root):
    """Move every item in outbox/failed/ back into the queue, in its old place. Returns the count."""
    moved = 0
    for path, item in failed(store_root):
        if item is None or "server_url" not in item:
            continue
        again = {key: item[key] for key in ("server_url", "op", "body", "created_at", "attempts") if key in item}
        again["last_error"] = item.get("error") or item.get("last_error")
        write_private(root(store_root) / path.name, again)
        path.unlink()
        moved += 1
    return moved


def sync(store_root, client_for, retry=False):
    """Take the lock, optionally bring failed items back, and send the queue."""
    with lock(store_root):
        if retry:
            retry_failed(store_root)
        return drain(store_root, client_for)


CHOICES = ("supersede", "drop")
# How many ids resolve tries after the stored one: <session id>-2, -3, ...
SUFFIXES = 20
# How far resolve follows superseded_by to find the newest memory.
CHAIN_LIMIT = 50


class ResolveError(ValueError):
    """resolve can't do what was asked. The item stays in outbox/failed/."""


def _failed_path(store_root, name):
    """The file in outbox/failed/ that name means: its file name, with or without .json."""
    name = str(name or "").strip()
    if name.endswith(".json"):
        name = name[: -len(".json")]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ResolveError(f"{name or 'that'} is not an item name; agent-memory sync lists them")
    path = failed_root(store_root) / f"{name}.json"
    if not path.is_file():
        raise ResolveError(f"no item {name} in {failed_root(store_root)}; agent-memory sync lists them")
    return path


def _read(path):
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return item if isinstance(item, dict) else None


def is_conflict(item):
    """A memory the server refused with a 409: that id already holds different content."""
    return (
        isinstance(item, dict)
        and item.get("op") == "memory"
        and item.get("status") == 409
        and isinstance(item.get("body"), dict)
        and bool(item.get("stored_id"))
        and bool(item.get("server_url"))
    )


def _newest(client, stored_id, text):
    """(id to supersede, id already holding text or None), following superseded_by from stored_id.

    Superseding the newest memory keeps the chain in one line: superseding an
    old one again would move its superseded_by off the memory that replaced it.
    When a newer memory in the chain already has the queued text, an earlier
    resolve got through and nothing more is sent. A token without the read
    scope can't follow the chain, so the stored id is used as it is.
    """
    chain = []
    current = stored_id
    while len(chain) < CHAIN_LIMIT:
        try:
            found = client.get_memory(current)
        except server_client.ServerError as error:
            if error.status == 403 and not chain:
                return stored_id, None
            raise
        if found is None:
            if not chain:
                raise ResolveError(
                    f"the server no longer keeps {stored_id}; run agent-memory sync --retry-failed "
                    "to send the item as it was"
                )
            # superseded_by names a memory this server doesn't have: stop at the last one it has.
            return chain[-1], None
        if chain and memory_rules.clean_text(str(found.get("text") or "")) == text:
            return current, current
        chain.append(current)
        newer = str(found.get("superseded_by") or "").strip()
        if not newer or newer in chain:
            return current, None
        current = newer
    # Too long to follow: supersede the last memory that was read.
    return chain[-1], None


def _label(item):
    op = (item or {}).get("op")
    return op if op in OPS else "item"


def resolve(store_root, name, choice, client_for, replace_link=False):
    """Settle one item in outbox/failed/. Returns what was done, as a dict.

    drop deletes the item: for a 409 the server keeps the memory it has. It
    works for any failed item, an unreadable one included.

    supersede is for a 409 only. A kept memory is never edited, so the queued
    text goes to the server as a new memory under <session id>-2 (or the next
    free number) whose supersedes names the stored one, or the newest memory
    that replaces it. The server stamps superseded_by on that one. The item is
    deleted only once the server has kept the new memory; when the server
    doesn't answer, it stays where it is.

    The queue for that server goes first, so the order is kept: a note approved
    after the 409 that supersedes the same memory lands before this one. While
    items still wait for that server, supersede refuses.

    A memory has one supersedes. When the queued memory already names another
    one there, supersede refuses unless replace_link says to replace that link.
    """
    if choice not in CHOICES:
        raise ResolveError(f"choose {' or '.join(CHOICES)}, not {choice!r}")
    with lock(store_root):
        path = _failed_path(store_root, name)
        item = _read(path)
        if choice == "drop":
            raw = None if item is not None else path.read_text(encoding="utf-8", errors="replace")
            path.unlink()
            return {"choice": "drop", "item": item, "name": path.stem, "raw": raw}
        if not is_conflict(item):
            status = (item or {}).get("status") or "unreadable"
            raise ResolveError(
                f"supersede is for a memory that got a 409, and {path.stem} failed with {status}: "
                "fix the cause and run agent-memory sync --retry-failed, or drop it"
            )
        url = item["server_url"]
        body = dict(item["body"])
        replaced = str(body.get("supersedes") or "").strip()
        if replaced and not replace_link:
            raise ResolveError(
                f"{path.stem} already supersedes {replaced}, and a memory has one supersedes. "
                f"Add --replace-link to supersede {item['stored_id']} instead, so {replaced} stays without "
                "superseded_by, or drop it and approve a new note"
            )
        drain(store_root, client_for)
        waiting = pending_for(store_root, url)
        if waiting:
            raise ResolveError(
                f"{waiting} item{'s' if waiting != 1 else ''} for {url} still wait in the outbox, and they go first; "
                "run agent-memory sync once the server takes them"
            )
        client = client_for(url)
        text = memory_rules.clean_text(str(body.get("text") or ""))
        target, already = _newest(client, item["stored_id"], text)
        done = {"choice": "supersede", "item": item, "name": path.stem, "supersedes": None, "dropped_link": None, "already": True}
        if already:
            path.unlink()
            return {**done, "id": already}
        base = str(body.get("session_id") or "").strip()
        for number in range(2, 2 + SUFFIXES):
            body["session_id"] = f"{base}-{number}"
            body["supersedes"] = target
            try:
                answer = send(client, {"op": "memory", "body": body})
            except server_client.ServerError as error:
                if error.status == 409:
                    stored = memory_rules.clean_text(str((error.body.get("stored") or {}).get("text") or ""))
                    if stored == text:
                        # That id already holds this text: an earlier resolve the chain walk didn't reach.
                        path.unlink()
                        return {**done, "id": error.body.get("id") or body["session_id"]}
                    continue
                raise
            path.unlink()
            return {
                "choice": "supersede",
                "item": item,
                "name": path.stem,
                "id": (answer or {}).get("id"),
                "supersedes": target,
                "dropped_link": replaced if replaced and replaced != target else None,
                "already": False,
            }
        raise ResolveError(
            f"every id from {base}-2 to {base}-{1 + SUFFIXES} already holds other text; "
            "make a new note with Supersedes instead, then drop this item"
        )


def _indent(text, prefix="    "):
    return "\n".join(prefix + line for line in (str(text or "").splitlines() or [""]))


def describe_failed(store_root):
    """Lines for every item in outbox/failed/: its error, and for a 409 both texts and the two choices."""
    out = []
    for path, item in failed(store_root):
        name = path.stem
        if item is None:
            out.append(f"{name}: not readable. Drop it with agent-memory sync --resolve {name} drop")
            continue
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        what = _label(item)
        out.append(f"{name}: {what} for {body.get('repo') or '?'}, queued {item.get('created_at') or '?'}")
        if not is_conflict(item):
            out.append(f"  failed ({item.get('status')}): {item.get('error') or ''}")
            if item.get("op") == "memory":
                out.append("  queued text:")
                out.append(_indent(body.get("text")))
            out.append(
                "  fix the cause, then run agent-memory sync --retry-failed, "
                f"or drop it: agent-memory sync --resolve {name} drop"
            )
            continue
        stored_id = item["stored_id"]
        out.append(f"  409: the server already keeps {stored_id} with other content.")
        out.append("  queued text (yours, approved offline):")
        out.append(_indent(body.get("text")))
        out.append(f"  stored text ({stored_id}):")
        out.append(_indent(item.get("stored")))
        if memory_rules.clean_text(str(body.get("text") or "")) == memory_rules.clean_text(str(item.get("stored") or "")):
            out.append("  The texts are the same; another field differs (kind, domain, commits, links or the time).")
        linked = str(body.get("supersedes") or "").strip()
        if linked:
            out.append(
                f"  It supersedes {linked}. A memory has one supersedes, so supersede needs --replace-link, "
                f"and {linked} then stays without superseded_by."
            )
        out.append("  Choose one:")
        out.append(
            f"    agent-memory sync --resolve {name} supersede   "
            f"send the queued text as a new memory that supersedes {stored_id}"
        )
        out.append(f"    agent-memory sync --resolve {name} drop        keep {stored_id} as it is, and delete the queued text")
    return out


def pending_for(store_root, url):
    """How many queued items go to url."""
    return sum(1 for _, item in queued(store_root) if item and item.get("server_url") == url)


def _shas_of(item, repo):
    body = item.get("body") if isinstance(item.get("body"), dict) else {}
    if item.get("op") == "memory":
        listed = body.get("commits") or [body.get("commit")]
        return [sha for sha in listed if memory_rules.is_sha(sha)]
    if item.get("op") == "rejection" and body.get("repo") == repo:
        return [sha for sha in body.get("shas") or [] if memory_rules.is_sha(sha)]
    return []


def same_commit(left, right):
    """Abbreviated hashes match both ways, as the server's commits/status does."""
    left, right = left.strip().lower(), right.strip().lower()
    return left.startswith(right) or right.startswith(left)


def waiting_commits(store_root, repo):
    """Commits a queued or failed item already settles: kept by a memory (any repo), or rejected in repo."""
    found = []
    for _, item in queued(store_root) + failed(store_root):
        if item:
            found.extend(_shas_of(item, repo))
    return found
