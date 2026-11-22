"""agent-memory migrate: copy the laptop's memories.sqlite files to the server.

Each row keeps its id, person and updated_at, and goes to /v1/admin/import in
batches. The server keeps a row only when its updated_at is newer than the one
it holds, so a rerun sends everything again and changes nothing. Rejected
commits go along, so land and the hooks keep skipping them.

The laptop files are read through store_core.snapshot, which never writes to
them. After the import, every id is read back and compared, every rejection is
checked with /v1/commits/status, and the server's own count is printed.

Standard library only, like the rest of the CLI.
"""

import sys

import memory_rules
import memory_store
import server_client
import store_core

BATCH = 50
STATUS_BATCH = 500


def sources(mode):
    """The laptop's memories.sqlite files for this mode, those that exist."""
    if mode == "shared":
        candidates = [memory_store.store_root() / "data" / "memories.sqlite"]
    else:
        base = memory_store.per_repo_root("x").parent
        candidates = sorted(base.glob("*/memories.sqlite")) if base.is_dir() else []
    return [path for path in candidates if path.is_file()]


def gather(paths, config):
    """Rows and rejections grouped by the server each one goes to.

    Returns {url: {"url", "token", "memories", "rejections"}}, in a stable
    order. In per-repo mode a row goes where its repo maps; with no server
    configured everything is grouped under None so dry-run can still count.
    """
    groups = {}

    def group_for(repo):
        found = server_client.target(repo, config) if config else None
        url, token = found if found else (None, None)
        return groups.setdefault(url, {"url": url, "token": token, "memories": [], "rejections": []})

    seen = set()
    for path in paths:
        documents, rejections = store_core.snapshot(path)
        for document in documents:
            if document["id"] in seen:
                raise store_core.StoreError(f"{document['id']} is in more than one local file; keep one and rerun")
            seen.add(document["id"])
            group_for(document["repo"])["memories"].append(document)
        for rejection in rejections:
            group_for(rejection["repo"])["rejections"].append(rejection)
    return groups


def counts(group):
    memories = group["memories"]
    return {
        "memories": len(memories),
        "rejected commits": len(group["rejections"]),
        "repos": sorted({document["repo"] for document in memories} | {item["repo"] for item in group["rejections"]}),
        "people": sorted({document["person"] for document in memories}),
    }


def print_counts(group, out):
    found = counts(group)
    print(f"{group['url'] or '(no server configured)'}:", file=out)
    print(f"  memories: {found['memories']}", file=out)
    print(f"  rejected commits: {found['rejected commits']}", file=out)
    print(f"  repos: {', '.join(found['repos']) or '-'}", file=out)
    print(f"  people: {', '.join(found['people']) or '-'}", file=out)


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def expected_hit(document):
    """What GET /v1/memories/{id} should return for a row sent from this document."""
    hit = {
        "document_id": document["id"],
        "kind": document["kind"],
        "domain": document["domain"],
        "repo": document["repo"],
        "tool": document["tool"],
        "session_id": document["session_id"],
        # The store fills an empty commit from the last of commits.
        "commit": document.get("commit") or (document.get("commits") or [""])[-1],
        "commits": list(document.get("commits", [])),
        "pull_request": document.get("pull_request"),
        "text": document["text"],
        "org": document["org"],
        "team": document["team"],
        "person": document["person"],
        "occurred_at": None,
    }
    if document.get("occurred_at"):
        hit["occurred_at"] = memory_rules.utc_time(document["occurred_at"], "occurred_at", allow_date=True)
    for name in store_core.OPTIONAL_FIELDS:
        if document.get(name):
            hit[name] = document[name]
    return hit


def send(client, group, out):
    """Import one group in batches. Returns the server's summed counts."""
    total = {"written": 0, "skipped": 0, "rejections": 0}
    for batch in _chunks(group["memories"], BATCH):
        answer = client.import_rows(batch, [])
        for key in ("written", "skipped"):
            total[key] += int(answer.get(key, 0))
    for batch in _chunks(group["rejections"], BATCH):
        answer = client.import_rows([], batch)
        total["rejections"] += int(answer.get("rejections", 0))
    print(
        f"  sent: {total['written']} written, {total['skipped']} already there or newer, "
        f"{total['rejections']} rejections new",
        file=out,
    )
    return total


def verify(client, group, out):
    """Read every id back and check every rejection. Returns the problems, one line each."""
    problems = []
    for document in group["memories"]:
        found = client.get_memory(document["id"])
        if found is None:
            problems.append(f"missing on the server: {document['id']}")
        elif found != expected_hit(document):
            problems.append(f"differs on the server (it may hold a newer edit): {document['id']}")
    by_repo = {}
    for rejection in group["rejections"]:
        by_repo.setdefault(rejection["repo"], []).append(rejection["sha"])
    for repo, shas in sorted(by_repo.items()):
        for batch in _chunks(shas, STATUS_BATCH):
            status = client.commits_status(repo, batch)
            for sha in batch:
                if not (status.get(sha) or {}).get("rejected"):
                    problems.append(f"rejection not on the server: {repo} {sha}")
    return problems


def migrate(dry_run=False, out=None, client_for=None):
    """Run migrate. Returns the exit code: 0 when everything arrived, 1 otherwise."""
    out = out or sys.stdout
    client_for = client_for or (lambda url, token: server_client.Client(url, token))
    config = server_client.read_config()
    mode = config["mode"] if config else server_client.legacy_mode()
    paths = sources(mode)
    if not paths:
        print(f"no local memories.sqlite to migrate ({mode} mode)", file=out)
        return 0
    for path in paths:
        print(f"reading {path}", file=out)
    groups = gather(paths, config)
    if dry_run:
        for group in groups.values():
            print_counts(group, out)
        if config is None:
            print("connect first: agent-memory connect --url http://127.0.0.1:8765", file=out)
        return 0
    if config is None:
        raise server_client.ConfigError("no server configured; run agent-memory connect --url <server url> first")
    failed = False
    for group in groups.values():
        print_counts(group, out)
        client = client_for(group["url"], group["token"])
        send(client, group, out)
        problems = verify(client, group, out)
        for problem in problems:
            print(f"  {problem}", file=out)
        if problems:
            failed = True
        else:
            print(f"  verified: all {len(group['memories'])} memories and {len(group['rejections'])} rejections are there", file=out)
        print(f"  the server holds {client.health().get('memories')} memories in all", file=out)
    return 1 if failed else 0
