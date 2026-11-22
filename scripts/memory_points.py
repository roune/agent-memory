"""What a memory looks like in Qdrant: its point id, payload, and payload indexes.

scripts/embed.py and the server both build points with these, so a point means
the same thing whichever of them wrote it. Standard library only.
"""

import uuid

KEYWORD_FIELDS = (
    "repo",
    "tool",
    "session_id",
    "kind",
    "domain",
    "commit",
    "commits",
    "org",
    "team",
    "person",
    "record",
    "subject",
    "alarm",
    "look_at",
    "supersedes",
    "superseded_by",
    "updates",
)

# Every payload index a collection gets: field name -> Qdrant field schema.
PAYLOAD_INDEXES = {
    **{name: "keyword" for name in KEYWORD_FIELDS},
    "pull_request": "integer",
    "occurred_at": "datetime",
}

OPTIONAL_FIELDS = (
    "record",
    "subject",
    "alarm",
    "look_at",
    "supersedes",
    "superseded_by",
    "updates",
)


def point_id(document_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-memory:{document_id}"))


def payload_for(document, model_name, revision, provider=None):
    """The payload of one memory's point. provider is added when given."""
    payload = {
        "document_id": document["id"],
        "kind": document["kind"],
        "domain": document["domain"],
        "repo": document["repo"],
        "tool": document["tool"],
        "session_id": document["session_id"],
        "commit": document.get("commit") or "",
        "commits": list(document.get("commits") or []),
        "text": document["text"],
        "org": document.get("org") or "",
        "team": document.get("team") or "",
        "person": document.get("person") or "",
        "embedding_model": model_name,
        "embedding_revision": revision,
    }
    if provider:
        payload["embedding_provider"] = provider
    pull_request = document.get("pull_request")
    if isinstance(pull_request, int):
        payload["pull_request"] = pull_request
    occurred = document.get("occurred_at")
    if isinstance(occurred, str) and occurred.strip():
        payload["occurred_at"] = occurred.strip()
    for optional in OPTIONAL_FIELDS:
        val = document.get(optional)
        if val:
            payload[optional] = val
    return payload
