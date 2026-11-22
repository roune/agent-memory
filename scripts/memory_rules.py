"""What an approved memory must look like.

agent-memory's approve() and the server's POST /v1/memories both build a
document with build_document(), so the two can't drift apart. The rules raise
ValueError with a message a person can act on. Nothing here reads git, the
inbox, or ~/.config: the caller fills in defaults first.
"""

KINDS = ("change", "learning", "decision")
DOMAINS = ("code", "architecture", "operations")
RECORDS = ("", "explanation", "history", "runbook")
LINK_FIELDS = ("subject", "alarm", "look_at", "supersedes", "superseded_by", "updates")

_HEX = frozenset("0123456789abcdefABCDEF")


def _strip(value):
    return "" if value is None else str(value).strip()


def clean_text(text):
    """A browser submits a textarea's line breaks as CRLF. Store LF, trimmed."""
    return _strip(text).replace("\r\n", "\n").replace("\r", "\n").strip()


def check_shape(kind, domain, text):
    if kind not in KINDS or domain not in DOMAINS or not text:
        raise ValueError("kind, domain, and text are required")


def check_identity(org, team, person):
    if not org or not team or not person:
        raise ValueError("org, team, and person are required (identity file or form)")
    if "/" in person or person in {".", ".."}:
        raise ValueError("person must be a single path segment")


def check_record(record):
    if record not in RECORDS:
        raise ValueError("record must be empty, explanation, history, or runbook")


def check_commit(commit):
    """The commit a change explains: at least 7 hex digits. Returns it stripped."""
    commit = _strip(commit)
    if len(commit) < 7 or any(char not in _HEX for char in commit):
        raise ValueError("a change needs the commit it explains")
    return commit


def document_id(person, kind, session_id):
    return f"{person}-{kind}-{session_id}"


def build_document(
    *,
    kind,
    domain,
    text,
    repo,
    tool,
    session_id,
    org,
    team,
    person,
    occurred_at,
    commit="",
    commits=(),
    pull_request=None,
    record="",
    subject="",
    alarm="",
    look_at="",
    supersedes="",
    superseded_by="",
    updates="",
):
    """Check every rule and return the document memory_store saves.

    commit, commits and pull_request count only for a change. An empty commits
    list is stored as just the commit the change names.
    """
    text = clean_text(text)
    check_shape(kind, domain, text)
    repo, tool, session_id = _strip(repo), _strip(tool), _strip(session_id)
    for name, value in (("repo", repo), ("tool", tool), ("session id", session_id)):
        if not value:
            raise ValueError(f"{name} is required")
    org, team, person = _strip(org), _strip(team), _strip(person)
    check_identity(org, team, person)
    occurred_at = _strip(occurred_at)
    if not occurred_at:
        raise ValueError("occurred_at is required")
    record = _strip(record)
    check_record(record)

    document = {
        "id": document_id(person, kind, session_id),
        "kind": kind,
        "domain": domain,
        "repo": repo,
        "tool": tool,
        "session_id": session_id,
        "text": text,
        "org": org,
        "team": team,
        "person": person,
        "occurred_at": occurred_at,
    }
    if kind == "change":
        commit = check_commit(commit)
        document["commit"] = commit
        document["commits"] = list(commits)
        if pull_request is not None:
            document["pull_request"] = pull_request
    if record:
        document["record"] = record
    links = {
        "subject": subject,
        "alarm": alarm,
        "look_at": look_at,
        "supersedes": supersedes,
        "superseded_by": superseded_by,
        "updates": updates,
    }
    for name in LINK_FIELDS:
        value = _strip(links[name])
        if value:
            document[name] = value
    return document
