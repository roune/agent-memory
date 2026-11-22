"""Request bodies -> what the store takes. Standard library only.

Every function raises ValueError with a message for a 400. A body may carry
only the fields of an approved document: any other key, transcript_path
included, is refused, so nothing but reviewed text reaches the server.
"""

import memory_rules

# The fields of the document approve() builds.
MEMORY_KEYS = frozenset(
    {
        "id",
        "kind",
        "domain",
        "text",
        "repo",
        "tool",
        "session_id",
        "org",
        "team",
        "person",
        "occurred_at",
        "commit",
        "commits",
        "pull_request",
        "record",
        *memory_rules.LINK_FIELDS,
    }
)

# A full row as /v1/admin/import takes it: the shape store_core.document_from_row returns.
IMPORT_KEYS = MEMORY_KEYS | {"updated_at"}
IMPORT_REQUIRED = ("id", "kind", "domain", "text", "repo", "tool", "session_id", "org", "team", "person", "updated_at")

MAX_SHAS = 1000
MAX_IMPORT = 500


def _object(body, allowed, what="the body"):
    if not isinstance(body, dict):
        raise ValueError(f"{what} must be a JSON object")
    unknown = sorted(set(body) - allowed)
    if "transcript_path" in unknown:
        raise ValueError("unknown field: transcript_path (transcripts never leave the laptop)")
    if unknown:
        raise ValueError(f"unknown field: {', '.join(unknown)}")
    return body


def memory_from_body(body):
    """POST /v1/memories -> the document build_document makes. With auth off, identity comes from the body."""
    body = _object(body, MEMORY_KEYS)
    fields = {key: body.get(key) for key in MEMORY_KEYS if key != "id"}
    fields["commits"] = body.get("commits", ())
    document = memory_rules.build_document(**fields)
    if "id" in body and body["id"] != document["id"]:
        raise ValueError(f"id must be {document['id']}, built from person, kind and session_id")
    return document


def _shas(value):
    if not isinstance(value, list) or not value or len(value) > MAX_SHAS:
        raise ValueError(f"shas must be a list of 1 to {MAX_SHAS} commit hashes")
    found = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("shas must be a list of commit hashes")
        try:
            found.append(memory_rules.check_commit(item))
        except ValueError:
            raise ValueError(f"not a commit hash: {item!r}") from None
    return found


def _repo(body):
    repo = body.get("repo")
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("repo is required")
    return repo.strip()


def commits_from_body(body):
    """POST /v1/rejections and /v1/commits/status: {repo, shas}."""
    body = _object(body, frozenset({"repo", "shas"}))
    return _repo(body), _shas(body.get("shas"))


def _import_memory(item):
    item = _object(item, IMPORT_KEYS, "each memory")
    memory_rules.check_types(item)
    for key in IMPORT_REQUIRED:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{item.get('id') or 'a memory'}: {key} is required")
    try:
        memory_rules.check_shape(item["kind"], item["domain"], item["text"].strip())
        memory_rules.check_record((item.get("record") or "").strip())
        # Rows already kept on a laptop: check the types, not how approve() picked them.
        commits = item.get("commits", [])
        if not isinstance(commits, list) or not all(memory_rules.is_sha(sha) for sha in commits):
            raise ValueError("commits must be a list of commit hashes")
        if item.get("commit"):
            memory_rules.check_commit(item["commit"])
        memory_rules.check_pull_request(item.get("pull_request"))
    except ValueError as error:
        raise ValueError(f"{item['id']}: {error}") from None
    return item


def _import_rejection(item):
    item = _object(item, frozenset({"repo", "sha", "rejected_at"}), "each rejection")
    repo = _repo(item)
    sha = item.get("sha")
    if not isinstance(sha, str):
        raise ValueError("each rejection needs a sha")
    rejected_at = item.get("rejected_at", "")
    if not isinstance(rejected_at, str):
        raise ValueError("rejected_at must be text")
    return {"repo": repo, "sha": memory_rules.check_commit(sha), "rejected_at": rejected_at}


def import_from_body(body):
    """POST /v1/admin/import: {memories: [...], rejections: [...]}, up to MAX_IMPORT of each."""
    body = _object(body, frozenset({"memories", "rejections"}))
    memories = body.get("memories", [])
    rejections = body.get("rejections", [])
    for name, value in (("memories", memories), ("rejections", rejections)):
        if not isinstance(value, list) or len(value) > MAX_IMPORT:
            raise ValueError(f"{name} must be a list of at most {MAX_IMPORT}")
    return [_import_memory(item) for item in memories], [_import_rejection(item) for item in rejections]


QUERY_KEYS = frozenset(
    {"team", "org", "repo", "person", "kind", "domain", "tool", "session_id", "since", "until", "limit", "offset"}
)


def query_from_params(params):
    """GET /v1/memories query parameters -> Store.query_memories keywords."""
    unknown = sorted(set(params) - QUERY_KEYS)
    if unknown:
        raise ValueError(f"unknown parameter: {', '.join(unknown)}")
    filters = {key: params[key] for key in QUERY_KEYS - {"limit", "offset"} if key in params}
    for key in ("limit", "offset"):
        if key in params:
            try:
                filters[key] = int(params[key])
            except ValueError:
                raise ValueError(f"{key} must be a number") from None
    return filters
