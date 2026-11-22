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


TEXT_FIELDS = (
    ("kind", "kind"),
    ("domain", "domain"),
    ("text", "text"),
    ("repo", "repo"),
    ("tool", "tool"),
    ("session_id", "session id"),
    ("org", "org"),
    ("team", "team"),
    ("person", "person"),
    ("occurred_at", "occurred_at"),
    ("commit", "commit"),
    ("record", "record"),
) + tuple((name, name) for name in LINK_FIELDS)


def _strip(value):
    return "" if value is None else value.strip()


def check_types(values):
    """Every text field is a string or missing. A list or a number is a caller bug."""
    for key, label in TEXT_FIELDS:
        value = values.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{label} must be text")


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


def _is_sha(value):
    return isinstance(value, str) and len(value.strip()) >= 7 and all(char in _HEX for char in value.strip())


def check_commits(commit, commits=(), pull_request=None):
    """What a change stores: the commit it explains, every commit it covers, its PR.

    commits lists each commit once, oldest first, and includes commit. An empty
    list means just commit. pull_request is a positive number or None. Returns
    (commit, commits, pull_request).
    """
    commit = check_commit(commit)
    if not isinstance(commits, (list, tuple)) or not all(_is_sha(item) for item in commits):
        raise ValueError("commits must be a list of commit hashes")
    listed = []
    for item in commits:
        item = item.strip()
        if item.lower() not in (seen.lower() for seen in listed):
            listed.append(item)
    if not listed:
        listed = [commit]
    elif commit.lower() not in (item.lower() for item in listed):
        raise ValueError("commits must include the commit the change explains")
    if pull_request is not None and (
        isinstance(pull_request, bool) or not isinstance(pull_request, int) or pull_request < 1
    ):
        raise ValueError("pull_request must be a positive number")
    return commit, listed, pull_request


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

    commit, commits and pull_request count only for a change, and follow
    check_commits. Every text field must be a string or None.
    """
    check_types(locals())
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
        commit, commits, pull_request = check_commits(commit, commits, pull_request)
        document["commit"] = commit
        document["commits"] = commits
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
