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
- 409: move the item to outbox/failed/ with the stored text alongside.
- Any other 4xx: move the item to outbox/failed/ with the error.

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
    rejected; with "all": True, only when it reports every one of them. A hook
    that must not wait on the network leaves that check to sync.
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
        if unless_settled.get("all"):
            item["unless_settled"]["all"] = True
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
    if not check.get("all"):
        return any(isinstance(info, dict) and (info.get("kept") or info.get("rejected")) for info in answer.values())
    infos = [answer.get(sha) for sha in check["shas"]]
    return all(isinstance(info, dict) and (info.get("kept") or info.get("rejected")) for info in infos)


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
            out.append(f"{left['failed']} failed: fix them, then run agent-memory sync --retry-failed")
        return out or ["nothing to send"]


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
