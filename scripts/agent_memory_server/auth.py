"""Bearer tokens: who is calling, what they may do, and whose name they write under. Standard library only.

A token is a random secret, `am_…`, shown once when it is minted. The server
keeps only its sha256. Each token has scopes:

  read   GET /v1/memories, POST /v1/commits/status, and /mcp
  write  POST /v1/memories, /v1/rejections
  admin  POST /v1/admin/import
  POST /v1/tokens needs read and write, since the child it mints reads.
  GET /v1/whoami takes any valid token.

Scopes don't imply each other. A laptop's token is read,write. Give admin to
a separate token for `migrate` (PR 4) and revoke it afterwards: import keeps
each row's own person, so admin can write as anyone.

A write may supersede a memory in any org. The stamp only links the old row
to the new one; its text never changes, and both stay readable. That is a
decision (2026-11-22): each person answers for what they write, and controls
can come later.

With auth on, a token pins `person`, and `org` and `team` default to the
token's own and may only be overridden with values the token allows. With
auth off (local compose only), every caller has every scope and the identity
comes from the request body, as it does on the laptop today.
"""

import hashlib
import json
import secrets
import unicodedata
from dataclasses import dataclass

SCOPES = ("read", "write", "admin")
PREFIX = "am_"
LOCAL = "local"
MAX_LABEL = 64


class AuthError(Exception):
    """A request the token can't make: 401 (no or bad token) or 403 (not allowed)."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def new_secret():
    return PREFIX + secrets.token_urlsafe(32)


def new_token_id():
    return "t_" + secrets.token_hex(6)


def hash_secret(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_scopes(value):
    """'read,write' or a list -> ('read', 'write') in SCOPES order. Unknown or none -> ValueError."""
    items = value.split(",") if isinstance(value, str) else list(value)
    found = {item.strip().lower() for item in items if str(item).strip()}
    unknown = sorted(found - set(SCOPES))
    if unknown:
        raise ValueError(f"unknown scope: {', '.join(unknown)} (scopes: {', '.join(SCOPES)})")
    if not found:
        raise ValueError("a token needs at least one scope")
    return tuple(scope for scope in SCOPES if scope in found)


def clean_label(value, required=False):
    """A token label: trimmed, at most MAX_LABEL characters, no control characters.

    `token list` prints one token per line, so a newline or tab in a label
    would break it. Empty is allowed unless required.
    """
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError("label must be text")
    label = value.strip()
    if required and not label:
        raise ValueError(f"label must be 1 to {MAX_LABEL} characters")
    if len(label) > MAX_LABEL:
        raise ValueError(f"label must be at most {MAX_LABEL} characters")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in label):
        raise ValueError("label can't contain control characters or line breaks")
    return label


def bearer(header):
    """The secret in `Authorization: Bearer am_…`, or None."""
    if not header:
        return None
    kind, _, value = header.strip().partition(" ")
    if kind.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True)
class Caller:
    token_id: str
    person: str | None
    org: str | None
    team: str | None
    allowed_orgs: tuple = ()
    allowed_teams: tuple = ()
    scopes: tuple = SCOPES
    label: str = ""
    auth: bool = True

    @classmethod
    def local(cls):
        """Auth off: every scope, identity from the request body."""
        return cls(token_id=LOCAL, person=None, org=None, team=None, auth=False)

    @classmethod
    def from_row(cls, row):
        return cls(
            token_id=row["id"],
            person=row["person"],
            org=row["org"],
            team=row["team"],
            allowed_orgs=tuple(json.loads(row["allowed_orgs"])),
            allowed_teams=tuple(json.loads(row["allowed_teams"])),
            scopes=tuple(json.loads(row["scopes"])),
            label=row["label"],
        )

    def require(self, scope):
        if scope not in self.scopes:
            raise AuthError(403, f"this token lacks the {scope} scope (it has {', '.join(self.scopes)})")
        return self

    def describe(self):
        return {
            "auth": "on" if self.auth else "off",
            "token": self.token_id if self.auth else None,
            "label": self.label or None,
            "person": self.person,
            "org": self.org,
            "team": self.team,
            "allowed_orgs": [self.org, *self.allowed_orgs] if self.auth else [],
            "allowed_teams": [self.team, *self.allowed_teams] if self.auth else [],
            "scopes": list(self.scopes),
            "identity": "from the token" if self.auth else "from the request body",
        }


def pin_identity(caller, body):
    """Fill person, org and team from the token, or refuse a body that names someone else.

    person must be the token's (or absent). org and team default to the
    token's, and may be any value the token allows. With auth off, the body is
    returned as it is.
    """
    if not caller.auth or not isinstance(body, dict):
        return body
    person = body.get("person")
    if person not in (None, "") and person != caller.person:
        raise AuthError(403, f"this token writes as {caller.person}, not {person!r}")
    chosen = {}
    for field, own, allowed in (
        ("org", caller.org, caller.allowed_orgs),
        ("team", caller.team, caller.allowed_teams),
    ):
        value = body.get(field)
        if value in (None, ""):
            value = own
        if value != own and value not in allowed:
            listed = ", ".join([own, *allowed])
            raise AuthError(403, f"this token can't write {field} {value!r} (allowed: {listed})")
        chosen[field] = value
    return {**body, "person": caller.person, **chosen}
