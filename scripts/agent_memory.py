#!/usr/bin/env python3
"""agent-memory command: capture, review, hook install, and repo config.

Editor hooks call `capture` and return. Git post-commit/post-merge call `git-hook`.
Review is a page on 127.0.0.1. Approve and Reject send to the agent-memory
server that `connect` (or ./scripts/setup) configured. When it doesn't answer,
the request waits in the outbox and `sync` sends it later.
`install` writes editor hooks only after a yes; with `--git-repo` it can also
install git hooks and repo policy under `.agent-memory.toml`.
Use `config` to change git hook policy and auto-approve without reinstalling.
`migrate` copies the old local memories.sqlite to the server, and `list` reads
from the server.
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import hashlib
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import memory_migrate
import memory_rules
import memory_store
import outbox
import server_client
HOME = Path.home()
BIN = ROOT / "bin" / "agent-memory"
PORT_START = 4173
KINDS = memory_rules.KINDS
DOMAINS = memory_rules.DOMAINS
RECORDS = memory_rules.RECORDS
END_EVENTS = {"session-end", "sessionend", "session_shutdown"}
MARKER = "agent-memory-hook"
GIT_HOOK_MARKER = "agent-memory-git-hook"

_CONFIG_BOOL_KEYS = frozenset({"capture", "git_hooks", "auto_approve"})


def store_root():
    override = os.environ.get("AGENT_MEMORY_DIR")
    return Path(override) if override else ROOT


def inbox_root():
    return store_root() / "inbox"


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(message):
    path = store_root() / "data" / "capture.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{now()} {message}\n")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} is not JSON ({error}). Left it unchanged.") from error


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def git(command, cwd):
    """Run a command and never raise for a missing program (gh is optional)."""
    try:
        return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def run(command, cwd=None):
    result = git(command, cwd)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def ok(command, cwd):
    return git(command, cwd).returncode == 0


def git_root(cwd):
    found = run(["git", "rev-parse", "--show-toplevel"], cwd)
    return found or cwd


def repo_policy_path(repo):
    return Path(repo) / ".agent-memory.toml"


def parse_repo_config(text):
    """Parse minimal key = value lines from .agent-memory.toml (TOML-like)."""
    cfg = {
        "capture": True,
        "repo": "",
        "git_hooks": False,
        "auto_approve": False,
    }
    if not text or not str(text).strip():
        return cfg
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip().lower().replace("-", "_")
        val = rest.strip()
        if val.startswith('"') and val.endswith('"') and len(val) >= 2:
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'") and len(val) >= 2:
            val = val[1:-1]
        if key == "repo":
            cfg["repo"] = val
        elif key in _CONFIG_BOOL_KEYS:
            low = val.lower()
            if low in {"true", "1", "yes"}:
                cfg[key] = True
            elif low in {"false", "0", "no"}:
                cfg[key] = False
    return cfg


def read_repo_config(repo):
    path = repo_policy_path(repo)
    if not path.is_file():
        return parse_repo_config("")
    return parse_repo_config(path.read_text(encoding="utf-8"))


def format_repo_config_lines(cfg):
    """Build TOML lines for known policy keys."""
    chunks = []
    repo_val = (cfg.get("repo") or "").strip()
    if repo_val:
        esc = str(repo_val).replace("\\", "\\\\").replace('"', '\\"')
        chunks.append(f'repo = "{esc}"\n')
    for bk in ("capture", "git_hooks", "auto_approve"):
        if bk in cfg:
            chunks.append(f"{bk} = {'true' if cfg[bk] else 'false'}\n")
    return chunks


def write_repo_config(repo, updates):
    """Merge updates into .agent-memory.toml, keeping unrecognized lines."""
    path = repo_policy_path(repo)
    merged = dict(read_repo_config(repo))
    merged.update(updates)
    preserved = []
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.split("#", 1)[0].strip()
            if not stripped or "=" not in stripped:
                preserved.append(raw)
                continue
            key = stripped.partition("=")[0].strip().lower().replace("-", "_")
            if key in {"repo", "capture", "git_hooks", "auto_approve"}:
                continue
            preserved.append(raw)
    while preserved and not preserved[-1].strip():
        preserved.pop()
    out = []
    out.extend(preserved)
    if preserved:
        out.append("")
    out.extend(format_repo_config_lines(merged))
    text = "\n".join(out)
    if text and not text.endswith("\n"):
        text += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sh_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def git_hooks_dir(repo):
    """Directory git actually runs hooks from.

    `core.hooksPath` wins. Otherwise this is `git rev-parse --git-path hooks`,
    which follows the common git dir (linked worktrees do not have their own).
    """
    repo = str(Path(repo).resolve())
    custom = run(["git", "config", "--get", "core.hooksPath"], repo).strip()
    if custom:
        path = Path(os.path.expanduser(custom))
        if not path.is_absolute():
            path = Path(repo) / path
        return path
    found = run(["git", "rev-parse", "--git-path", "hooks"], repo).strip()
    if not found:
        return None
    path = Path(found)
    if not path.is_absolute():
        path = Path(repo) / path
    return path


def git_hooks_installed(repo):
    """True if post-commit and post-merge in the active hooks dir are ours."""
    base = git_hooks_dir(repo)
    if base is None:
        return False
    for name in ("post-commit", "post-merge"):
        script = base / name
        if not script.is_file():
            return False
        try:
            txt = script.read_text(encoding="utf-8")
        except OSError:
            return False
        if GIT_HOOK_MARKER not in txt or "git-hook" not in txt:
            return False
    return True


def write_git_hooks(repo):
    """Install post-commit and post-merge hooks in the directory git will run.

    A hook we did not write is moved aside and called after ours. Reinstall
    rewrites only our script and leaves that saved hook in place.
    """
    repo = str(Path(repo).resolve())
    hooks_dir = git_hooks_dir(repo)
    if hooks_dir is None:
        raise SystemExit(f"could not locate the git hooks directory for {repo}")
    if hooks_dir.exists() and not hooks_dir.is_dir():
        raise SystemExit(f"git hooks path is not a directory: {hooks_dir}")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    bin_quoted = sh_quote(BIN)
    for name in ("post-commit", "post-merge"):
        path = hooks_dir / name
        user_path = hooks_dir / f"{name}.agent-memory-user"
        if path.is_file():
            try:
                txt = path.read_text(encoding="utf-8")
            except OSError as error:
                raise SystemExit(f"could not read {path}: {error}") from error
            if GIT_HOOK_MARKER not in txt:
                if user_path.exists():
                    raise SystemExit(
                        f"{path} is not an agent-memory hook and {user_path.name} already exists"
                    )
                path.rename(user_path)
        user_quoted = sh_quote(user_path)
        # Do not forward "$@" to git-hook. post-merge is invoked with a squash
        # flag, and git-hook has no positional arguments.
        body = f"""#!/bin/sh
# {GIT_HOOK_MARKER}
cd "$(git rev-parse --show-toplevel)" && {bin_quoted} git-hook --event {name}
_am_status=$?
if [ -f {user_quoted} ]; then
  if [ -x {user_quoted} ]; then
    {user_quoted} "$@"
  else
    sh {user_quoted} "$@"
  fi
  _user_status=$?
  if [ "$_user_status" -ne 0 ]; then
    exit "$_user_status"
  fi
fi
exit "$_am_status"
"""
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return hooks_dir


def remove_git_hooks(repo):
    """Remove only hooks previously written by write_git_hooks.

    A hook that was moved aside is restored to its original name.
    """
    base = git_hooks_dir(repo)
    if base is None:
        return
    for name in ("post-commit", "post-merge"):
        path = base / name
        user_path = base / f"{name}.agent-memory-user"
        if path.is_file():
            try:
                txt = path.read_text(encoding="utf-8")
            except OSError:
                txt = ""
            if GIT_HOOK_MARKER in txt:
                path.unlink()
        if not path.exists() and user_path.is_file():
            user_path.rename(path)


# A trailer token has a hyphen (Co-Authored-By, Signed-off-by, Claude-Session).
# One-word lines such as "Note: ..." are prose and stay.
_TRAILER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+:\s*\S")
_GENERATED_RE = re.compile(r"^\s*\W*\s*Generated with \[?Claude Code", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"^\s*<?https?://\S+>?\s*$")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SQUASH_SUFFIX_RE = re.compile(r"\s*\(#\d+\)\s*$")
# A paragraph that is only a rule, like the "---------" GitHub puts before squash trailers.
_RULE_RE = re.compile(r"^(?:[-=*_]{3,}\s*)+$")


def _paragraphs(text):
    text = (text or "").replace("\x00", "").replace("\r\n", "\n")
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _drop_generated_footer(paragraphs):
    """Remove a "Generated with Claude Code" line and a bare link right after it."""
    kept = []
    skip_url = False
    for part in paragraphs:
        lines = []
        for line in part.splitlines():
            if _GENERATED_RE.match(line):
                skip_url = True
                continue
            if skip_url and _BARE_URL_RE.match(line):
                skip_url = False
                continue
            skip_url = skip_url and not line.strip()
            lines.append(line)
        if any(line.strip() for line in lines):
            kept.append("\n".join(lines).strip())
    return kept


def clean_message(message):
    """A commit message without NUL, trailers, rule lines, or a generated-with footer."""
    paragraphs = [p for p in _drop_generated_footer(_paragraphs(message)) if not _RULE_RE.match(p)]
    if len(paragraphs) > 1 and all(_TRAILER_RE.match(line) for line in paragraphs[-1].splitlines()):
        paragraphs = paragraphs[:-1]
    return "\n\n".join(paragraphs)


def clean_pr_body(body):
    """A PR description without template comments or a generated-with footer."""
    return "\n\n".join(_drop_generated_footer(_paragraphs(_HTML_COMMENT_RE.sub("", body or ""))))


def draft_text_commit_only(commit_message):
    """Draft for git hook: the commit message only. The sha and repo are columns."""
    return clean_message(commit_message)


def single_commit_hook_group(repo, sha, message):
    ct_s = run(["git", "log", "-1", "--format=%ct", sha], repo)
    try:
        ct = int(ct_s) if ct_s else 0
    except ValueError:
        ct = 0
    return {
        "pr": None,
        "commits": [{"sha": sha, "committer_date": ct, "message": message or ""}],
    }


def draft_choice(bundle):
    """kind, domain, text and commit an auto-approve takes from a bundle's draft."""
    draft = bundle.get("draft") or {}
    kind = draft.get("kind") if draft.get("kind") in KINDS else "change"
    domain = draft.get("domain") if draft.get("domain") in DOMAINS else "code"
    return kind, domain, (draft.get("text") or "").strip(), (draft.get("commit") or "").strip()


def try_auto_approve_bundle(bundle, unless_settled=None):
    """Queue the bundle's draft as approved, and delete the bundle. Raises ValueError or ConfigError.

    Hooks never wait on the network: the document goes to the outbox, and a
    detached sync sends it.
    """
    kind, domain, text, commit = draft_choice(bundle)
    return approve(bundle, kind, domain, text, commit=commit, queue_only=True, unless_settled=unless_settled)


def is_merge_commit(repo, sha):
    parents = run(["git", "rev-parse", f"{sha}^@"], repo).split()
    return len(parents) > 1


def hook_commit_shas(repo, event, head):
    """Commits this hook invocation should file.

    post-commit files the new HEAD. post-merge files every single-parent commit
    reachable from HEAD but not from ORIG_HEAD, so a fast-forward or merge does
    not keep only the tip.
    """
    if event == "post-merge":
        orig = run(["git", "rev-parse", "--verify", "ORIG_HEAD"], repo)
        if orig and orig != head:
            listing = run(["git", "rev-list", "--reverse", f"{orig}..{head}"], repo)
            shas = [line.strip() for line in listing.splitlines() if line.strip()]
            return [sha for sha in shas if not is_merge_commit(repo, sha)]
    return [head]


def file_hook_commit(repo_s, sha, event, branch, tip, since, remote_id, auto_approve):
    """File one commit locally: an approved document in the outbox, or a bundle in the inbox.

    Returns the path written, or None when there was nothing to file.
    """
    msg = run(["git", "log", "-1", "--format=%B", sha], repo_s)
    group = single_commit_hook_group(repo_s, sha, msg)
    session_id = land_session_id(remote_id or repo_s, sha[:12], None, [sha])
    bundle_path = land_bundle_path("git", session_id)
    if bundle_path.is_file():
        return None
    draft_text = draft_text_commit_only(msg)
    bundle = {
        "tool": "git",
        "session_id": session_id,
        "repo": repo_s,
        "started_at": since,
        "ended_at": occurred_at_for_group(group),
        "event": "git-" + event.replace("_", "-"),
        "closed": True,
        "transcript_copy": None,
        "last_assistant_message": "",
        "git": {
            "branch": branch,
            "head": tip,
            "status": "",
            "diff_stat": "",
            "paths": commit_files(repo_s, sha),
        },
        "pinned": True,
        "draft": {
            "kind": "change",
            "domain": "code",
            "text": draft_text,
            "commit": sha,
        },
        "land": {
            "remote_repo": remote_id,
            "pull_request": None,
            "commits": [
                {
                    "sha": sha,
                    "subject": (msg or "").split("\n", 1)[0].strip(),
                }
            ],
        },
    }
    if auto_approve:
        try:
            kind, domain, text, commit = draft_choice(bundle)
            url, body = approved_request(bundle, kind, domain, text, commit=commit)
            return outbox.add(store_root(), url, "memory", body)
        except (ValueError, OSError, server_client.ConfigError) as error:
            log(f"git-hook: auto_approve failed, inbox bundle kept for review: {error}")
    write_json(bundle_path, bundle)
    return bundle_path


def git_hook(args):
    """File the new commits locally first, check them with one short call, then start a detached sync.

    The hook makes no other network call, so it returns within about two
    seconds even when the server doesn't answer.
    """
    repo = Path(args.repo or os.getcwd()).resolve()
    if run(["git", "rev-parse", "--is-inside-work-tree"], str(repo)) != "true":
        log("git-hook skipped: not a git repository")
        return 0
    repo_s = str(repo)
    if capture_disabled(repo_s):
        log("git-hook skipped: capture disabled for repo")
        return 0
    cfg = read_repo_config(repo_s)
    if not cfg.get("git_hooks"):
        return 0

    event = (args.event or "post-commit").lower()
    head = run(["git", "rev-parse", "HEAD"], repo_s)
    if not head:
        log("git-hook skipped: could not resolve HEAD")
        return 0
    shas = hook_commit_shas(repo_s, event, head)
    remote_id = git_remote_repo_id(repo_s)
    waiting = waiting_settled(remote_id, shas)
    shas = [sha for sha in shas if sha not in waiting]
    if not shas:
        return 0

    branch, tip = default_branch_tip(repo_s)
    since = parse_since("", 24)
    written = {}
    for sha in shas:
        path = file_hook_commit(repo_s, sha, event, branch, tip, since, remote_id, bool(cfg.get("auto_approve")))
        if path is not None:
            written[sha] = path
    # Commits pulled in by a merge may be settled already, by someone else or
    # by another laptop. On any failure the files stay.
    for sha in server_settled(remote_id, list(written), timeout=HOOK_STATUS_TIMEOUT):
        path = written[sha]
        if path.name == "bundle.json":
            shutil.rmtree(path.parent, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    spawn_sync()
    return 0


def capture_disabled(repo):
    cfg = read_repo_config(repo)
    if not cfg.get("capture", True):
        return True
    ignore = HOME / ".config" / "agent-memory" / "ignore"
    if ignore.is_file():
        repo_path = str(Path(repo).resolve())
        for line in ignore.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if item and (repo_path == item or repo_path.startswith(item.rstrip("/") + "/")):
                return True
    return False


def identity_path():
    return HOME / ".config" / "agent-memory" / "identity"


def read_identity():
    path = identity_path()
    if not path.is_file():
        return {"org": "", "team": "", "person": ""}
    org = team = person = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("org"):
            org = line.split("=", 1)[1].strip() if "=" in line else ""
        elif line.startswith("team"):
            team = line.split("=", 1)[1].strip() if "=" in line else ""
        elif line.startswith("person"):
            person = line.split("=", 1)[1].strip() if "=" in line else ""
    return {"org": org, "team": team, "person": person}


def policy_repo_id(repo):
    return (read_repo_config(repo).get("repo") or "").strip()


def normalize_remote_url(raw):
    if not raw or not str(raw).strip():
        return ""
    s = str(raw).strip()
    if s.startswith("git@"):
        rest = s[4:]
        if ":" in rest:
            host, path = rest.split(":", 1)
            host = host.strip().lower()
            path = path.strip().rstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return f"{host}/{path}"
        return ""
    try:
        parsed = urlparse(s)
        host = (parsed.hostname or "").lower()
        if not host:
            return ""
        path = (parsed.path or "").strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"{host}/{path}" if path else ""
    except Exception:
        return ""


def git_remote_repo_id(repo):
    policy = policy_repo_id(repo)
    if policy:
        return policy
    url = run(["git", "remote", "get-url", "origin"], repo)
    return normalize_remote_url(url)


def parse_since(value, default_hours):
    if not value or not str(value).strip():
        when = datetime.now(timezone.utc) - timedelta(hours=default_hours)
        return when.replace(microsecond=0).isoformat()
    s = str(value).strip()
    if re.match(r"^-?\d+$", s):
        when = datetime.now(timezone.utc) - timedelta(hours=int(s))
        return when.replace(microsecond=0).isoformat()
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.replace(microsecond=0).isoformat()
    except ValueError:
        raise SystemExit(
            f"--since expects ISO-8601 datetime or hours as integer (got {value!r})"
        ) from None


def commit_files(repo, sha):
    listing = run(["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha], repo)
    return [line.strip() for line in listing.splitlines() if line.strip()]


_GITHUB_MERGE_RE = re.compile(r"^Merge pull request #(\d+)", re.IGNORECASE)
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def pr_from_commit_message(message):
    if not message:
        return None
    first = message.strip().split("\n", 1)[0].strip()
    match = _GITHUB_MERGE_RE.match(first)
    if match:
        return int(match.group(1))
    match_sq = _SQUASH_PR_RE.search(first)
    if match_sq:
        return int(match_sq.group(1))
    return None


def gh_pr_description(repo, pr_number):
    out = run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "title,body",
        ],
        repo,
    )
    if not out:
        return "", ""
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return "", ""
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    return title, body


def gh_pr_for_sha(repo, sha):
    out = run(
        [
            "gh",
            "pr",
            "list",
            "--search",
            sha,
            "--state",
            "merged",
            "--json",
            "number,mergeCommit",
            "--limit",
            "20",
        ],
        repo,
    )
    if not out:
        return None
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return None
    for row in rows:
        mc = (row.get("mergeCommit") or {}).get("oid") or ""
        if mc == sha:
            num = row.get("number")
            return int(num) if num is not None else None
    return None


def land_session_id(repo_key, group_key, pr_number, shas):
    raw = f"{repo_key}|{group_key}|{pr_number or ''}|" + ",".join(sorted(shas))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"land-{digest}"


def land_bundle_path(tool, session_id):
    return session_dir(tool, session_id) / "bundle.json"


NO_SERVER = (
    "no agent-memory server is configured: run ./scripts/setup, "
    "or agent-memory connect --url http://127.0.0.1:8765"
)
# The commits/status check a git hook makes. The hook does no other network work.
HOOK_STATUS_TIMEOUT = 1.0
STATUS_BATCH = 500


class Refused(ValueError):
    """The server refused an Approve or Reject. The bundle is kept, and nothing is queued."""

    def __init__(self, error):
        super().__init__(server_client.explain(error))
        self.error = error
        self.stored = None
        if error.status == 409:
            self.stored = (error.body.get("stored") or {}).get("text")


_warned_legacy = False


def warn_legacy():
    """Say once that the legacy database files are no longer read."""
    global _warned_legacy
    if _warned_legacy:
        return
    _warned_legacy = True
    folder = server_client.config_dir()
    found = [name for name in ("database", "databases") if (folder / name).is_file()]
    if found:
        names = " and ".join(str(folder / name) for name in found)
        print(
            f"note: {names} {'is' if len(found) == 1 else 'are'} no longer read. The CLI uses "
            f"{folder / 'server'}; only agent-memory migrate and connect still read the old files.",
            file=sys.stderr,
        )


def server_config():
    """The server file, or ConfigError when there is none."""
    config = server_client.read_config()
    if config is None:
        raise server_client.ConfigError(NO_SERVER)
    return config


def server_for(remote_id):
    """(url, token) of the server that keeps remote_id's memories."""
    return server_client.target(remote_id or None, server_config())


def client_for(url, timeout=server_client.TIMEOUT):
    """A client for url, with the token the config files hold for it."""
    try:
        token = server_client.token_for(url)
    except server_client.ConfigError:
        token = None
    return server_client.Client(url, token, timeout)


def deliver(url, op, body, queue_only=False, unless_settled=None):
    """Send one request now, or queue it. Returns ("sent", answer) or ("queued", reason).

    Whatever is already queued for url goes first, so the order is kept. With
    queue_only (the hooks) nothing is sent here: a detached sync sends it.
    Raises Refused for a 4xx, a revoked token included, and then nothing is queued.
    """
    root = store_root()
    if queue_only:
        outbox.add(root, url, op, body, unless_settled=unless_settled)
        return "queued", None
    with outbox.lock(root):
        report = outbox.drain(root, client_for)
        stopped = report.stopped.get(url)
        if isinstance(stopped, server_client.ServerError) and stopped.status < 500:
            raise Refused(stopped)
        if stopped is None and not outbox.pending_for(root, url):
            try:
                return "sent", outbox.send(client_for(url), {"op": op, "body": body})
            except server_client.ServerError as error:
                if error.status < 500:
                    raise Refused(error) from None
                stopped = error
            except server_client.ServerUnreachable as error:
                stopped = error
        outbox.add(root, url, op, body)
        return "queued", server_client.explain(stopped) if stopped else None


def spawn_sync():
    """Start `agent-memory sync --quiet` detached, so a hook returns at once."""
    if not outbox.queued(store_root()):
        return
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "sync", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        log(f"could not start agent-memory sync: {error}")


def sync_first(quiet=True):
    """Send what the outbox holds before a command reads the server. Never raises."""
    root = store_root()
    if not outbox.queued(root):
        return None
    try:
        report = outbox.sync(root, client_for)
    except OSError as error:
        log(f"sync failed: {error}")
        return None
    if not quiet:
        for line in report.lines(root):
            print(f"sync: {line}", file=sys.stderr)
    return report


def waiting_settled(remote_id, shas):
    """The shas an item in the outbox already settles. No network."""
    waiting = outbox.waiting_commits(store_root(), remote_id)
    return {sha for sha in shas if any(outbox.same_commit(sha, other) for other in waiting)}


def server_settled(remote_id, shas, timeout=server_client.TIMEOUT):
    """The shas the server reports kept or rejected, in one call per batch.

    If the server can't be reached they count as not settled, so they are
    offered anyway: a duplicate costs one Reject, while a missed memory is lost.
    """
    shas = [sha for sha in dict.fromkeys(shas) if memory_rules.is_sha(sha)]
    if not shas or not remote_id:
        return set()
    try:
        url, token = server_for(remote_id)
        client = server_client.Client(url, token, timeout)
        settled = set()
        for start in range(0, len(shas), STATUS_BATCH):
            batch = shas[start : start + STATUS_BATCH]
            answer = client.commits_status(remote_id, batch)
            for sha in batch:
                info = answer.get(sha)
                if isinstance(info, dict) and (info.get("kept") or info.get("rejected")):
                    settled.add(sha)
        return settled
    except (server_client.ConfigError, server_client.ServerError, server_client.ServerUnreachable) as error:
        log(f"commits/status: {server_client.explain(error)}; the commits are offered anyway")
        return set()


def settled_commits(remote_id, shas, timeout=server_client.TIMEOUT):
    """The shas review already decided: waiting in the outbox, or kept or rejected on the server."""
    waiting = waiting_settled(remote_id, shas)
    return waiting | server_settled(remote_id, [sha for sha in shas if sha not in waiting], timeout)


def reject_bundle(bundle):
    """Send a git bundle's commits as rejected (or queue them), then delete the bundle.

    Returns "sent", "queued", or None when there was nothing to send. A refusal
    raises Refused and keeps the bundle.
    """
    land = bundle.get("land") or {}
    shas = [str(c.get("sha") or "") for c in land.get("commits") or [] if isinstance(c, dict)]
    shas = [sha for sha in shas if memory_rules.is_sha(sha)]
    status = None
    if bundle.get("tool") == "git" and shas:
        remote_id = (land.get("remote_repo") or "").strip() or git_remote_repo_id(str(bundle.get("repo") or ""))
        if not remote_id:
            raise ValueError("remote repo id is required (origin URL or .agent-memory.toml repo=)")
        url, _ = server_for(remote_id)
        status, _ = deliver(url, "rejection", {"repo": remote_id, "shas": shas})
    shutil.rmtree(session_dir(bundle["tool"], bundle["session_id"]))
    return status


def default_branch_tip(repo):
    branch = default_branch(repo)
    sha = run(["git", "rev-parse", branch], repo)
    return branch, sha


def commits_on_branch_since(repo, branch, since_iso):
    listing = run(
        [
            "git",
            "log",
            branch,
            f"--since={since_iso}",
            "--reverse",
            # Unit separator between fields, record separator after each commit.
            # A commit message cannot end up holding either one.
            "--pretty=format:%H%x1f%ct%x1f%B%x1e",
        ],
        repo,
    )
    commits = []
    for block in listing.split("\x1e"):
        block = block.strip()
        if not block:
            continue
        parts = block.split("\x1f", 2)
        if len(parts) < 3:
            continue
        sha, ct_s, message = parts[0], parts[1], parts[2]
        try:
            ct = int(ct_s)
        except ValueError:
            ct = 0
        commits.append({"sha": sha, "committer_date": ct, "message": message.strip()})
    return commits


def pr_numbers_from_merges(repo, merges):
    """Map each commit a "Merge pull request #N" commit brought in to N.

    The PR number is only on the merge commit, which land skips. The commits it
    merged are the ones reachable from its second parent and not from its first.
    """
    found = {}
    for item in merges:
        prn = pr_from_commit_message(item["message"])
        if prn is None:
            continue
        sha = item["sha"]
        listing = run(["git", "rev-list", f"{sha}^1..{sha}^2"], repo)
        for line in listing.splitlines():
            member = line.strip()
            if member:
                found.setdefault(member, prn)
    return found


def build_land_groups(repo, commits):
    """Split off merge commits; group the rest by PR number when present.

    A root commit has no parent. It is filed like any single-parent commit.
    """
    merges = []
    singles = []
    for item in commits:
        parents = run(["git", "rev-parse", f"{item['sha']}^@"], repo).split()
        if len(parents) > 1:
            merges.append(item)
            continue
        singles.append(item)

    merged_by_pr = pr_numbers_from_merges(repo, merges)

    groups = []
    by_pr = {}
    orphan_singles = []

    for item in singles:
        prn = merged_by_pr.get(item["sha"])
        if prn is None:
            prn = pr_from_commit_message(item["message"])
        if prn is None:
            prn = gh_pr_for_sha(repo, item["sha"])
        if prn is not None:
            by_pr.setdefault(prn, []).append(item)
        else:
            orphan_singles.append(item)

    for prn, members in sorted(by_pr.items()):
        groups.append({"pr": prn, "commits": sorted(members, key=lambda x: x["committer_date"])})

    for item in orphan_singles:
        groups.append({"pr": None, "commits": [item]})

    merge_notes = []
    for item in merges:
        prn = pr_from_commit_message(item["message"])
        if prn is not None:
            merge_notes.append(f"Merge commit {item['sha'][:10]} for PR #{prn} (PR group uses its squashed or direct commits).")
        else:
            merge_notes.append(
                f"Skipped merge commit {item['sha'][:10]}: {item['message'].splitlines()[0] if item['message'] else ''}".strip()
            )

    return groups, merge_notes


def draft_text_for_group(repo, group):
    """PR title and description, then each commit message, each said once.

    The PR number, shas, and repo id are stored beside the text, so
    they are not repeated in what gets embedded.
    """
    parts = []
    title = ""
    pr_number = group.get("pr")
    if pr_number is not None:
        title, body = gh_pr_description(repo, pr_number)
        if title:
            parts.append(title)
        body = clean_pr_body(body)
        if body:
            parts.append(body)
    for member in group["commits"]:
        message = clean_message(member["message"])
        subject, _, rest = message.partition("\n")
        if title and _SQUASH_SUFFIX_RE.sub("", subject).strip() == title:
            message = rest.strip()
        if message:
            parts.append(message)
    return "\n\n".join(parts)


def git_paths_for_group(repo, shas):
    paths = []
    for sha in shas:
        paths.extend(commit_files(repo, sha))
    out = []
    seen = set()
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def occurred_at_for_group(group):
    times = [m["committer_date"] for m in group["commits"] if m.get("committer_date")]
    if not times:
        return now()
    latest = max(times)
    return datetime.fromtimestamp(latest, tz=timezone.utc).replace(microsecond=0).isoformat()


def land(args):
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise SystemExit(f"not a directory: {repo}")
    if run(["git", "rev-parse", "--is-inside-work-tree"], str(repo)) != "true":
        raise SystemExit(f"not a git repository: {repo}")
    if capture_disabled(str(repo)):
        log("land skipped: capture disabled for repo")
        return 0
    warn_legacy()
    sync_first(quiet=False)

    since = parse_since(args.since, args.since_hours)
    branch = local_default_branch(str(repo))
    tip = run(["git", "rev-parse", branch], str(repo))
    remote_id = git_remote_repo_id(str(repo))
    commits = commits_on_branch_since(str(repo), branch, since)
    groups, merge_notes = build_land_groups(str(repo), commits)

    tool = "git"
    wrote = 0
    # One commits/status call for the whole window, plus what waits in the outbox.
    settled = settled_commits(remote_id, [m["sha"] for group in groups for m in group["commits"]])
    for group in groups:
        # Approve stores every commit of a group, so a commit already kept is
        # explained, and Reject remembers its commits. A PR that grew past an
        # earlier window files only the rest, under a new session id, so the
        # kept memory is not overwritten.
        members = [m for m in group["commits"] if m["sha"] not in settled]
        if not members:
            continue
        group = dict(group, commits=members)
        shas = [m["sha"] for m in members]

        prn = group.get("pr")
        group_key = f"pr-{prn}" if prn is not None else shas[0]
        session_id = land_session_id(remote_id or str(repo), group_key, prn, shas)
        bundle_path = land_bundle_path(tool, session_id)
        if bundle_path.is_file():
            continue

        paths = git_paths_for_group(str(repo), shas)
        draft_text = draft_text_for_group(str(repo), group)
        ended = occurred_at_for_group(group)
        bundle = {
            "tool": tool,
            "session_id": session_id,
            "repo": str(repo),
            "started_at": since,
            "ended_at": ended,
            "event": "land",
            "closed": True,
            "transcript_copy": None,
            "last_assistant_message": "",
            "git": {
                "branch": branch,
                "head": tip,
                "status": "",
                "diff_stat": "",
                "paths": paths,
            },
            "pinned": True,
            "draft": {
                "kind": "change",
                "domain": "code",
                "text": draft_text,
                "commit": shas[-1],
            },
            "land": {
                "remote_repo": remote_id,
                "pull_request": prn,
                "commits": [
                    {"sha": m["sha"], "subject": (m["message"] or "").split("\n", 1)[0].strip()}
                    for m in members
                ],
            },
        }
        write_json(bundle_path, bundle)
        wrote += 1

    for note in merge_notes:
        log(f"land: {note}")
    if wrote:
        print(f"land: wrote {wrote} bundle(s) under {inbox_root() / tool}")
    else:
        print("land: no new bundles (nothing to file or all commits already kept or rejected)")
    return 0


def git_summary(repo):
    status = run(["git", "status", "--porcelain"], repo)
    paths = []
    for line in status.splitlines():
        entry = line[3:] if len(line) > 3 else line
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        entry = entry.strip().strip('"')
        if entry:
            paths.append(entry)
    return {
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo),
        "head": run(["git", "rev-parse", "HEAD"], repo),
        "status": status,
        "diff_stat": run(["git", "diff", "--stat"], repo),
        "paths": paths,
    }


def claude_slug(path):
    return str(path).replace("/", "-")


def resolve_transcript(payload, repo, session_id):
    raw = payload.get("transcript_path") or payload.get("agent_transcript_path")
    if raw:
        candidate = Path(os.path.expanduser(str(raw)))
        if candidate.is_file():
            return candidate
    projects = HOME / ".claude" / "projects"
    if not projects.is_dir() or not session_id:
        return None
    roots = [repo]
    common = run(["git", "rev-parse", "--git-common-dir"], repo)
    if common:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = Path(repo) / common_path
        if common_path.name == ".git":
            roots.append(str(common_path.parent))
    seen = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        candidate = projects / claude_slug(root) / f"{session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def session_dir(tool, session_id):
    return inbox_root() / tool / session_id


def load_payload(args):
    if sys.stdin.isatty():
        raise SystemExit("capture reads the hook JSON on stdin")
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def commit_after_session_start(started_at, committer_epoch):
    if not started_at or not committer_epoch:
        return False
    try:
        parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return committer_epoch >= int(parsed.timestamp())


def maybe_auto_approve_session(repo, cwd, bundle_path, previous):
    """Approve a commit this session actually made. Otherwise leave the inbox.

    The text is the commit message, never the last assistant reply, and HEAD
    from before the session is not treated as already kept.
    """
    cfg = read_repo_config(repo)
    if not cfg.get("auto_approve"):
        return
    started = previous.get("started_at")
    if not started:
        log("capture session-end: auto_approve left inbox; session has no earlier capture")
        return
    if run(["git", "rev-parse", "--is-inside-work-tree"], cwd) != "true":
        log("capture session-end: auto_approve left inbox; not a git checkout")
        return
    head = run(["git", "rev-parse", "HEAD"], repo)
    if not head:
        log("capture session-end: auto_approve left inbox; could not resolve HEAD")
        return
    # No network here: Codex gives SessionEnd about a second. The outbox is
    # checked now; the server is asked by sync, before it sends (unless_settled).
    if waiting_settled(git_remote_repo_id(repo), [head]):
        log("capture session-end: commit already waiting in the outbox; inbox left for review")
        return
    if is_merge_commit(repo, head):
        log("capture session-end: HEAD is a merge commit; inbox left for review")
        return
    ct_s = run(["git", "log", "-1", "--format=%ct", head], repo)
    try:
        ct = int(ct_s) if ct_s else 0
    except ValueError:
        ct = 0
    if not commit_after_session_start(started, ct):
        log("capture session-end: HEAD is older than this session; inbox left for review")
        return
    remote_id = git_remote_repo_id(repo)
    body = run(["git", "log", "-1", "--format=%B", head], repo) or ""
    text = draft_text_commit_only(body)
    if not text.strip():
        log("capture session-end: empty commit message; inbox left for review")
        return
    fresh = read_json(bundle_path)
    group = single_commit_hook_group(repo, head, body)
    fresh["draft"] = {
        "kind": "change",
        "domain": "code",
        "text": text,
        "commit": head,
    }
    fresh["ended_at"] = occurred_at_for_group(group)
    land_meta = fresh.get("land") if isinstance(fresh.get("land"), dict) else {}
    land_meta["remote_repo"] = remote_id or land_meta.get("remote_repo") or ""
    land_meta["commits"] = [
        {"sha": head, "subject": (body or "").split("\n", 1)[0].strip()}
    ]
    fresh["land"] = land_meta
    write_json(bundle_path, fresh)
    check = {"repo": remote_id, "shas": [head]} if remote_id else None
    try:
        try_auto_approve_bundle(read_json(bundle_path), unless_settled=check)
    except (ValueError, OSError, server_client.ConfigError) as error:
        log(f"capture session-end auto_approve failed, inbox bundle kept: {error}")
        return
    spawn_sync()


def capture(args):
    try:
        payload = load_payload(args)
    except json.JSONDecodeError as error:
        log(f"capture ignored invalid json: {error}")
        return 0
    except SystemExit:
        raise
    except Exception as error:
        log(f"capture failed before a bundle: {error}")
        return 0

    try:
        write_capture(args, payload)
    except Exception as error:
        log(f"capture failed: {error}")
    return 0


def write_capture(args, payload):
    tool = args.tool
    event = args.event or str(payload.get("hook_event_name") or "capture")
    session_id = str(
        payload.get("session_id")
        or payload.get("conversation_id")
        or ""
    ).strip()
    if not session_id or "/" in session_id or session_id in {".", ".."}:
        log("capture skipped: missing session id")
        return

    cwd = payload.get("cwd")
    if not cwd:
        roots = payload.get("workspace_roots") or []
        cwd = roots[0] if roots else os.getcwd()
    cwd = os.path.expanduser(str(cwd))
    repo = git_root(cwd)
    if capture_disabled(repo):
        return

    summary = git_summary(repo) if run(["git", "rev-parse", "--is-inside-work-tree"], cwd) == "true" else {
        "branch": "",
        "head": "",
        "status": "",
        "diff_stat": "",
        "paths": [],
    }
    folder = session_dir(tool, session_id)
    bundle_path = folder / "bundle.json"
    previous = read_json(bundle_path) if bundle_path.is_file() else {}
    transcript = resolve_transcript(payload, repo, session_id)
    copy_name = previous.get("transcript_copy")
    if transcript is not None:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "transcript"
        same = target.is_file() and target.stat().st_size == transcript.stat().st_size
        quiet = not summary["status"] and not summary["diff_stat"] and same
        if quiet and event.lower() not in END_EVENTS:
            return
        if not same:
            shutil.copyfile(transcript, target)
            copy_name = "transcript"

    message = payload.get("last_assistant_message") or previous.get("last_assistant_message") or ""
    bundle = {
        "tool": tool,
        "session_id": session_id,
        "repo": repo,
        "started_at": previous.get("started_at") or now(),
        "ended_at": now(),
        "event": event,
        "closed": previous.get("closed", False) or event.lower() in END_EVENTS,
        "transcript_copy": copy_name,
        "last_assistant_message": message,
        "git": summary,
        "pinned": previous.get("pinned", False),
    }
    write_json(bundle_path, bundle)

    if event.lower() in END_EVENTS:
        maybe_auto_approve_session(repo, cwd, bundle_path, previous)


def bundles():
    root = inbox_root()
    if not root.is_dir():
        return []
    found = []
    for path in root.glob("*/*/bundle.json"):
        try:
            found.append(read_json(path))
        except json.JSONDecodeError:
            continue
    return found


def find_bundle(session_id, tool=None):
    matches = [item for item in bundles() if item.get("session_id") == session_id]
    if tool is not None:
        matches = [item for item in matches if item.get("tool") == tool]
    if not matches:
        raise SystemExit(f"no inbox bundle for {session_id}")
    if len(matches) > 1:
        tools = ", ".join(sorted(item.get("tool") or "" for item in matches))
        raise SystemExit(f"{session_id} matches more than one tool: {tools}. Pass --tool.")
    return matches[0]


def default_branch(repo):
    ref = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
    if ref:
        return ref
    for name in ("main", "master"):
        if ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo):
            return name
    return "main"


def local_default_branch(repo):
    """Local branch `land` should read.

    `origin/HEAD` only supplies the name (`main` or `master`). Logging the
    remote-tracking ref skips commits that are on the local branch and not
    pushed, and files commits that are not on the local branch.
    """
    ref = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
    if ref:
        name = ref.split("/", 1)[1] if "/" in ref else ref
        if ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo):
            return name
    for name in ("main", "master"):
        if ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo):
            return name
    head = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    if head and head != "HEAD":
        return head
    return ref or "main"


def candidate_commits(bundle):
    """Commits the session can explain. The diff itself stays in git."""
    repo = bundle.get("repo") or ""
    if not repo or not Path(repo).is_dir():
        return []
    since = bundle.get("started_at") or ""
    paths = (bundle.get("git") or {}).get("paths") or []
    command = ["git", "log", "--all", "-n", "20", "--pretty=format:%H\t%s"]
    if since:
        command.extend(["--since", since])
    if paths:
        command.append("--")
        command.extend(paths)
    listing = run(command, repo)
    commits = []
    seen = set()
    for line in listing.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        if sha in seen:
            continue
        seen.add(sha)
        commits.append({"sha": sha, "subject": subject})
    return commits


def landed(bundle):
    repo = bundle.get("repo") or ""
    if not repo or not Path(repo).is_dir():
        return []
    branch = default_branch(repo)
    since = bundle.get("started_at") or ""
    paths = (bundle.get("git") or {}).get("paths") or []
    command = ["git", "log", branch, "--since", since, "--pretty=format:%H\t%s"]
    if paths:
        command.append("--")
        command.extend(paths)
    listing = run(command, repo)
    commits = []
    for line in listing.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        if ok(["git", "merge-base", "--is-ancestor", sha, branch], repo):
            commits.append({"sha": sha, "subject": subject, "branch": branch})
    return commits


def offered(include_all):
    items = []
    for bundle in bundles():
        commits = landed(bundle)
        if include_all or bundle.get("pinned") or commits:
            item = dict(bundle)
            item["commits"] = commits
            items.append(item)
    items.sort(key=lambda item: item.get("ended_at") or "", reverse=True)
    return items


def note(args):
    text = (args.text or "").strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read().strip()
    if not text:
        raise SystemExit("pass --text, or pipe the memory text")
    session_id = args.session_id.strip()
    if not session_id or "/" in session_id or session_id in {".", ".."}:
        raise SystemExit("session id must be a single path segment")
    repo = git_root(os.getcwd())
    folder = session_dir("cursor", session_id)
    bundle_path = folder / "bundle.json"
    previous = read_json(bundle_path) if bundle_path.is_file() else {}
    iden = read_identity()
    rr = git_remote_repo_id(repo)
    bundle = {
        "tool": "cursor",
        "session_id": session_id,
        "repo": repo,
        "started_at": previous.get("started_at") or now(),
        "ended_at": now(),
        "event": "note",
        "closed": True,
        "transcript_copy": None,
        "last_assistant_message": "",
        "git": {"branch": "", "head": "", "status": "", "diff_stat": "", "paths": []},
        "pinned": True,
        "land": {"remote_repo": rr},
        "draft": {"kind": args.kind, "domain": args.domain, "text": text},
    }
    write_json(bundle_path, bundle)
    print(f"pinned {session_id}")
    print("Open it with: agent-memory review")


def keep(args):
    bundle = find_bundle(args.session_id, args.tool)
    path = session_dir(bundle["tool"], bundle["session_id"]) / "bundle.json"
    bundle["pinned"] = True
    write_json(path, bundle)
    print(f"pinned {args.session_id}")


def drop(args):
    bundle = find_bundle(args.session_id, args.tool)
    folder = session_dir(bundle["tool"], bundle["session_id"])
    shutil.rmtree(folder)
    print(f"dropped {args.session_id}")


def commits_for_change(bundle, commit):
    """Commits and PR number a change stores: the pointer a later reader follows.

    A land or git-hook bundle lists its commits. When the chosen commit is one of
    them, the change explains all of them and the bundle's pull request. A
    session bundle, or a commit picked from outside that list, stores just it.
    """
    land_meta = bundle.get("land") if isinstance(bundle.get("land"), dict) else {}
    listed = [
        str(item.get("sha") or "").strip()
        for item in land_meta.get("commits") or []
        if isinstance(item, dict) and str(item.get("sha") or "").strip()
    ]
    chosen = commit.lower()
    if any(sha.lower() == chosen for sha in listed):
        pull_request = land_meta.get("pull_request")
        numbered = isinstance(pull_request, int) and not isinstance(pull_request, bool) and pull_request > 0
        return listed, pull_request if numbered else None
    return [commit], None


def approve(
    bundle,
    kind,
    domain,
    text,
    commit="",
    org="",
    team="",
    person="",
    occurred_at="",
    remote_repo="",
    record="",
    subject="",
    alarm="",
    look_at="",
    supersedes="",
    superseded_by="",
    updates="",
    queue_only=False,
    unless_settled=None,
):
    """Send the approved document to the server, or queue it, then delete the bundle.

    Returns {"status": "sent" or "queued", "id", "reason"}. A refusal (a 4xx)
    raises Refused and keeps the bundle; so does any other ValueError.
    queue_only and unless_settled are for the hooks: see outbox.add.
    """
    url, body = approved_request(
        bundle,
        kind,
        domain,
        text,
        commit=commit,
        org=org,
        team=team,
        person=person,
        occurred_at=occurred_at,
        remote_repo=remote_repo,
        record=record,
        subject=subject,
        alarm=alarm,
        look_at=look_at,
        supersedes=supersedes,
        superseded_by=superseded_by,
        updates=updates,
    )
    status, answer = deliver(url, "memory", body, queue_only=queue_only, unless_settled=unless_settled)
    shutil.rmtree(session_dir(bundle["tool"], bundle["session_id"]))
    if status == "sent":
        return {"status": "sent", "id": (answer or {}).get("id"), "reason": None}
    return {"status": "queued", "id": None, "reason": answer}


def approved_request(
    bundle,
    kind,
    domain,
    text,
    commit="",
    org="",
    team="",
    person="",
    occurred_at="",
    remote_repo="",
    record="",
    subject="",
    alarm="",
    look_at="",
    supersedes="",
    superseded_by="",
    updates="",
):
    """(server url, POST /v1/memories body) for an approved bundle. Raises ValueError or ConfigError.

    The body is checked with the same rules the server uses. It has no id: the
    server builds it. With a token (auth on) the server takes person from the
    token, so the body leaves it out, and org and team stay out unless the form
    named them. With auth off they come from the form or the identity file.
    """
    text = memory_rules.clean_text(text)
    memory_rules.check_shape(kind, domain, text)
    repo_path = str(bundle.get("repo") or "").strip()
    tool = str(bundle.get("tool") or "").strip()
    session_id = str(bundle.get("session_id") or "").strip()
    if not repo_path or not tool or not session_id:
        raise ValueError("repo, tool, and session id are required")

    land = bundle.get("land") or {}
    remote_repo = (remote_repo or land.get("remote_repo") or "").strip()
    if not remote_repo:
        remote_repo = git_remote_repo_id(repo_path)
    if not remote_repo:
        raise ValueError("remote repo id is required (origin URL or .agent-memory.toml repo=)")
    url, token = server_for(remote_repo)
    pinned = bool(token)

    org = (org or "").strip()
    team = (team or "").strip()
    if pinned:
        # Placeholders only for the checks below; they are not sent.
        check_org, check_team, person = org or "-", team or "-", "-"
    else:
        iden = read_identity()
        org = org or (iden.get("org") or "").strip()
        team = team or (iden.get("team") or "").strip()
        person = ((person or "").strip() or (iden.get("person") or "").strip())
        memory_rules.check_identity(org, team, person)
        check_org, check_team = org, team

    occurred_at = (occurred_at or bundle.get("ended_at") or "").strip()
    if not occurred_at:
        occurred_at = now()
    memory_rules.check_record(record)

    commits = []
    pull_request = None
    if kind == "change":
        commit = memory_rules.check_commit(commit)
        commits, pull_request = commits_for_change(bundle, commit)

    document = memory_rules.build_document(
        kind=kind,
        domain=domain,
        text=text,
        repo=remote_repo,
        tool=tool,
        session_id=session_id,
        org=check_org,
        team=check_team,
        person=person,
        occurred_at=occurred_at,
        commit=commit,
        commits=commits,
        pull_request=pull_request,
        record=record,
        subject=subject,
        alarm=alarm,
        look_at=look_at,
        supersedes=supersedes,
        superseded_by=superseded_by,
        updates=updates,
    )
    body = {key: value for key, value in document.items() if key != "id"}
    if pinned:
        del body["person"]
        for field, chosen in (("org", org), ("team", team)):
            if not chosen:
                del body[field]
    return url, body


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Agent memory</title>
<style>
  body { font: 16px/1.45 ui-sans-serif, system-ui, sans-serif; margin: 2rem auto; max-width: 46rem; color: #1c1917; background: #fafaf9; }
  h1 { font-size: 1.4rem; }
  article { background: white; border: 1px solid #e7e5e4; border-radius: 8px; padding: 1rem 1.1rem; margin: 1rem 0; }
  .meta { color: #57534e; font-size: 0.92rem; }
  pre { white-space: pre-wrap; background: #f5f5f4; padding: 0.7rem; border-radius: 6px; }
  fieldset { border: 1px solid #e7e5e4; border-radius: 6px; margin: 0.8rem 0; }
  fieldset label { font-family: ui-monospace, monospace; font-size: 0.92rem; }
  label { display: block; margin-top: 0.7rem; }
  textarea { width: 100%; min-height: 7rem; font: inherit; }
  select { font: inherit; }
  button { font: inherit; margin-right: 0.4rem; margin-top: 0.8rem; }
  a { color: #1d4ed8; }
  code { font-family: ui-monospace, monospace; font-size: 0.88rem; overflow-wrap: anywhere; }
  ul.commits { margin: 0.3rem 0; padding-left: 1.2rem; }
  .banner { border-radius: 6px; padding: 0.6rem 0.8rem; margin: 0.6rem 0; background: #f5f5f4; }
  .banner.ok { background: #ecfdf5; }
  .banner.warn { background: #fffbeb; }
  .banner.bad { background: #fef2f2; }
  input[readonly] { background: #f5f5f4; }
</style>
</head>
<body>
<h1>Review</h1>
<p class="meta">__LEAD__</p>
__BANNER__
__ITEMS__
</body>
</html>
"""


# About 512 bge-small tokens of English prose.
LONG_TEXT_CHARS = 1800


def pointer_block(commits, draft, land_meta):
    """The commits a land or hook bundle stores, and how to read them in git.

    Approve stores all of them, so they are listed, not offered as a choice.
    """
    shas = [commit["sha"] for commit in commits]
    tip = (draft.get("commit") or "").strip()
    if tip not in shas:
        tip = shas[-1]
    items = "".join(
        f'<li><code>{html.escape(commit["sha"][:10])}</code> {html.escape(commit.get("subject") or "")}</li>'
        for commit in commits
    )
    pull_request = land_meta.get("pull_request")
    legend = f"Pull request #{pull_request}" if isinstance(pull_request, int) else "Commits"
    commands = ["git show --stat " + " ".join(sha[:12] for sha in shas)]
    if isinstance(pull_request, int):
        remote = (land_meta.get("remote_repo") or "").strip()
        commands.append(f"gh pr view {pull_request}" + (f" -R {remote}" if remote else ""))
    read = " · ".join(f"<code>{html.escape(command)}</code>" for command in commands)
    return (
        f'<fieldset><legend>{html.escape(legend)}</legend><ul class="commits">{items}</ul></fieldset>'
        f'<input type="hidden" name="commit" value="{html.escape(tip)}">'
        f'<p class="meta">The memory stores these commits, not their diff. Read them in git: {read}</p>'
    )


def select_options(choices, selected):
    return "".join(
        f'<option{" selected" if choice == selected else ""}>{html.escape(choice)}</option>'
        for choice in choices
    )


# whoami on each page load. Short, so an offline page still opens at once.
PAGE_TIMEOUT = 2.0

DONE = {
    "sent": ("ok", "Approved and sent to the server."),
    "queued": ("warn", "Approved and queued: the server didn't answer. agent-memory sync sends it."),
    "rejected": ("ok", "Rejected."),
    "rejected-queued": ("warn", "Rejected and queued: the server didn't answer. agent-memory sync sends it."),
}


def page_status():
    """What the banner and the identity fields need: the server, whoami, and the outbox."""
    root = store_root()
    status = {
        "configured": False,
        "url": None,
        "connected": False,
        "error": "",
        "who": None,
        "config": None,
        "outbox": outbox.summary(root),
        "failed": outbox.failed(root),
    }
    try:
        config = server_client.read_config()
    except server_client.ConfigError as error:
        status["error"] = str(error)
        return status
    if config is None:
        status["error"] = NO_SERVER
        return status
    status.update(configured=True, url=config["url"], config=config, whos={})
    try:
        who = server_client.Client(config["url"], config["token"], PAGE_TIMEOUT).whoami()
    except (server_client.ServerError, server_client.ServerUnreachable) as error:
        status["error"] = server_client.explain(error)
        status["whos"][config["url"]] = None
        return status
    if isinstance(who, dict):
        status.update(connected=True, who=who)
        status["whos"][config["url"]] = who
    return status


def server_view(status, remote):
    """(pinned, whoami) for the server that keeps remote's memories, as the page shows it.

    pinned means the server takes person from a token: there is a token, and
    whoami says auth is on (or didn't answer). In per-repo mode each server is
    asked once per page.
    """
    config = (status or {}).get("config")
    if not config:
        return False, None
    try:
        url, token = server_client.target(remote or None, config)
    except server_client.ConfigError:
        return False, None
    whos = status.setdefault("whos", {})
    if url not in whos:
        try:
            found = server_client.Client(url, token, PAGE_TIMEOUT).whoami()
        except (server_client.ServerError, server_client.ServerUnreachable):
            found = None
        whos[url] = found if isinstance(found, dict) else None
    who = whos[url]
    return bool(token) and (who is None or who.get("auth") == "on"), who


def _banner(kind, text):
    return f'<p class="banner {kind}">{text}</p>'


def render_banner(status, done=""):
    """Connected, offline or not configured; what waits in the outbox; and what failed for good."""
    parts = []
    if done in DONE:
        kind, text = DONE[done]
        parts.append(_banner(kind, html.escape(text)))
    if status is None:
        return "\n".join(parts)
    url = html.escape(status.get("url") or "")
    if not status["configured"]:
        parts.append(_banner("bad", html.escape(status["error"]) + ". Approve and Reject need a server."))
    elif status["connected"]:
        who = status["who"] or {}
        parts.append(_banner("ok", f"Connected to <code>{url}</code> as {html.escape(describe_caller(who))}."))
    else:
        parts.append(
            _banner(
                "warn",
                f"Offline: <code>{url}</code> didn't answer ({html.escape(status['error'])}). "
                "Approve and Reject wait in the outbox until <code>agent-memory sync</code> sends them.",
            )
        )
    waiting = status["outbox"]
    if waiting["queued"]:
        parts.append(
            _banner(
                "warn",
                f"Queued: {waiting['queued']} waiting in the outbox, the oldest for "
                f"{html.escape(waiting['oldest'] or 'a while')}. <code>agent-memory sync</code> sends them.",
            )
        )
    if status["failed"]:
        parts.append(render_failed(status["failed"]))
    return "\n".join(parts)


def render_failed(failed):
    """Items the server refused for good, each with its error, and the stored text for a 409."""
    rows = []
    for path, item in failed:
        if item is None:
            rows.append(f"<li><code>{html.escape(path.name)}</code>: not readable.</li>")
            continue
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        what = "Memory" if item.get("op") == "memory" else "Rejection"
        error = f"{item.get('status') or ''} {item.get('error') or ''}".strip()
        detail = ""
        if item.get("op") == "memory":
            detail = f"<pre>{html.escape(str(body.get('text') or '')[:2000])}</pre>"
        elif body.get("shas"):
            detail = f"<p class=\"meta\">{html.escape(body.get('repo') or '')}: {html.escape(', '.join(body['shas']))}</p>"
        if item.get("status") == 409:
            stored_id = html.escape(item.get("stored_id") or "that id")
            detail += (
                f'<p class="meta">The server already keeps {stored_id} with this text:</p>'
                f"<pre>{html.escape(str(item.get('stored') or ''))}</pre>"
                '<p class="meta">A newer note replaces it through Supersedes.</p>'
            )
        rows.append(
            f"<li>{what} for <code>{html.escape(body.get('repo') or '')}</code>, queued "
            f"{html.escape(item.get('created_at') or '')}: {html.escape(error)}"
            f"{detail}<p class=\"meta\">File: <code>{html.escape(path.name)}</code></p></li>"
        )
    return (
        '<div class="banner bad"><strong>Failed</strong>: the server refused these. Fix the cause, '
        "then run <code>agent-memory sync --retry-failed</code>, or delete the file from "
        "<code>outbox/failed/</code>."
        f'<ul>{"".join(rows)}</ul></div>'
    )


def identity_fields(status, iden, remote=""):
    """Organization, team and person. With a token the server pins person, and org and team to its lists."""
    pinned, who = server_view(status, remote)
    if pinned:
        who = who or {}
        if who.get("auth") == "on":
            orgs = [value for value in who.get("allowed_orgs") or [who.get("org")] if value]
            teams = [value for value in who.get("allowed_teams") or [who.get("team")] if value]
            person = html.escape(who.get("person") or "")
            return (
                f'<label>Organization <select name="org">{select_options(orgs, who.get("org"))}</select></label>\n'
                f'<label>Team <select name="team">{select_options(teams, who.get("team"))}</select></label>\n'
                f'<label>Person (id prefix) <input name="person" type="text" value="{person}" readonly></label>\n'
                '<p class="meta">Your token sets person, and the organizations and teams you may write as.</p>'
            )
        return (
            '<label>Organization <input name="org" type="text" value="" placeholder="your token\'s"></label>\n'
            '<label>Team <input name="team" type="text" value="" placeholder="your token\'s"></label>\n'
            '<label>Person (id prefix) <input name="person" type="text" value="" '
            'placeholder="set by your token" readonly></label>\n'
            '<p class="meta">Your token sets person. Blank organization and team mean your token\'s.</p>'
        )
    org_d = html.escape(iden.get("org") or "")
    team_d = html.escape(iden.get("team") or "")
    person_d = html.escape(iden.get("person") or "")
    return (
        f'<label>Organization <input name="org" type="text" value="{org_d}" autocomplete="organization"></label>\n'
        f'<label>Team <input name="team" type="text" value="{team_d}" autocomplete="organization-title"></label>\n'
        f'<label>Person (id prefix) <input name="person" type="text" value="{person_d}"></label>'
    )


def render_page(items, include_all, status=None, done=""):
    lead = "Sessions whose work is on the default branch, plus anything you pinned."
    if include_all:
        lead = "Every inbox bundle."
    if not items:
        body = "<p>Nothing to review.</p>"
    else:
        blocks = []
        for item in items:
            sid = html.escape(item["session_id"])
            tool = html.escape(item.get("tool") or "")
            draft = item.get("draft") or {}
            domain = draft.get("domain") if draft.get("domain") in DOMAINS else "code"
            draft_text = html.escape(draft.get("text") or "")
            land_meta = item.get("land") or {}
            if land_meta.get("commits"):
                commits = [
                    {"sha": c.get("sha", ""), "subject": c.get("subject", "")}
                    for c in land_meta["commits"]
                    if c.get("sha")
                ]
            else:
                commits = candidate_commits(item)
            kinds = KINDS if commits else tuple(name for name in KINDS if name != "change")
            draft_kind = draft.get("kind")
            if draft_kind in kinds:
                kind = draft_kind
            elif commits:
                kind = "change"
            else:
                kind = "learning"
            if commits and item.get("tool") == "git":
                commit_block = pointer_block(commits, draft, land_meta)
            elif commits:
                radios = []
                default_sha = (draft.get("commit") or "").strip()
                if not any(commit["sha"] == default_sha for commit in commits):
                    default_sha = commits[0]["sha"]
                for commit in commits:
                    checked = " checked" if commit["sha"] == default_sha else ""
                    short = html.escape(commit["sha"][:10])
                    subj = html.escape(commit.get("subject") or "")
                    sha = html.escape(commit["sha"])
                    radios.append(
                        f'<label><input type="radio" name="commit" value="{sha}"{checked}> {short} {subj}</label>'
                    )
                commit_block = "<fieldset><legend>Commit</legend>" + "".join(radios) + "</fieldset>"
            else:
                commit_block = "<p class=\"meta\">No commit yet. A change needs one. The working tree diff stays in git.</p>"
            message = item.get("last_assistant_message") or ""
            message_block = ""
            if message:
                message_block = f"<pre>{html.escape(message[:4000])}</pre>"
            text_label = "Context of this commit" if kind == "change" else "Keep this text"
            long_note = ""
            if len(draft.get("text") or "") > LONG_TEXT_CHARS:
                long_note = (
                    '<p class="meta">This text is long. The embedding reads only about '
                    "the first 512 tokens. Trim it, or keep what matters first.</p>"
                )
            iden = read_identity()
            rr_default = (land_meta.get("remote_repo") or "").strip()
            remote_d = html.escape(rr_default)
            occurred_default = html.escape(item.get("ended_at") or "")
            record_val = ""
            record_block = (
                '<label>Record <select name="record">'
                + "".join(
                    f'<option value="{html.escape(r)}"{" selected" if r == record_val else ""}>{html.escape(r or "(none)")}</option>'
                    for r in RECORDS
                )
                + "</select></label>"
            )
            extra_fields = f"""
{identity_fields(status, iden, rr_default)}
<label>Occurred at <input name="occurred_at" type="text" value="{occurred_default}"></label>
<label>Remote repo id <input name="remote_repo" type="text" value="{remote_d}" placeholder="github.com/owner/repo"></label>
{record_block}
<label>Subject <input name="subject" type="text" value=""></label>
<label>Alarm <input name="alarm" type="text" value=""></label>
<label>Look at <input name="look_at" type="text" value=""></label>
<label>Supersedes (document id) <input name="supersedes" type="text" value=""></label>
<label>Superseded by <input name="superseded_by" type="text" value=""></label>
<label>Updates (document id) <input name="updates" type="text" value=""></label>
"""
            blocks.append(
                f"""<article>
<p><strong>{html.escape(item.get("repo") or "")}</strong></p>
<p class="meta">{tool} · {sid} · {html.escape(item.get("ended_at") or "")}</p>
<p class="meta">A change stores the context of its commits. The diff stays in git.</p>
<form method="post" action="/approve">
<input type="hidden" name="session_id" value="{sid}">
<input type="hidden" name="tool" value="{tool}">
{commit_block}
{message_block}
<label>Kind <select name="kind">{select_options(kinds, kind)}</select></label>
<label>Domain <select name="domain">{select_options(DOMAINS, domain)}</select></label>
{extra_fields}
<label>{text_label} <textarea name="text">{draft_text}</textarea></label>
{long_note}
<button type="submit">Approve</button>
</form>
<form method="post" action="/reject">
<input type="hidden" name="session_id" value="{sid}">
<input type="hidden" name="tool" value="{tool}">
<button type="submit">Reject</button>
</form>
</article>"""
            )
        body = "\n".join(blocks)
    toggle = "Show the hidden sessions" if not include_all else "Show the default queue"
    href = "/?all=1" if not include_all else "/"
    body += f'<p><a href="{href}">{toggle}</a></p>'
    return (
        PAGE.replace("__LEAD__", html.escape(lead))
        .replace("__BANNER__", render_banner(status, done))
        .replace("__ITEMS__", body)
    )


class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self):
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8")
        return {key: values[0] for key, values in parse_qs(raw).items()}

    def _own_origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def _host_ok(self):
        """Exactly 127.0.0.1:<port>. A DNS-rebinding name such as 127.0.0.1.example.test is refused."""
        return self.headers.get("Host", "") == self._own_origin()[len("http://") :]

    def _same_origin(self):
        """A POST must come from this page: Origin, or else Referer, names 127.0.0.1:<port>."""
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin == self._own_origin()
        referer = self.headers.get("Referer") or ""
        return referer == self._own_origin() or referer.startswith(self._own_origin() + "/")

    def do_GET(self):
        if not self._host_ok():
            self._send(403, "localhost only")
            return
        query = parse_qs(urlparse(self.path).query)
        include_all = query.get("all", ["0"])[0] == "1"
        done = query.get("done", [""])[0]
        self._send(200, render_page(offered(include_all), include_all, page_status(), done))

    def do_POST(self):
        if not self._host_ok():
            self._send(403, "localhost only")
            return
        if not self._same_origin():
            self._send(403, "only the review page itself can approve or reject")
            return
        form = self._form()
        session_id = form.get("session_id", "")
        try:
            bundle = find_bundle(session_id, form.get("tool") or None)
            path = urlparse(self.path).path
            if path == "/approve":
                result = approve(
                    bundle,
                    form.get("kind", ""),
                    form.get("domain", ""),
                    form.get("text", ""),
                    form.get("commit", ""),
                    org=form.get("org", ""),
                    team=form.get("team", ""),
                    person=form.get("person", ""),
                    occurred_at=form.get("occurred_at", ""),
                    remote_repo=form.get("remote_repo", ""),
                    record=form.get("record", ""),
                    subject=form.get("subject", ""),
                    alarm=form.get("alarm", ""),
                    look_at=form.get("look_at", ""),
                    supersedes=form.get("supersedes", ""),
                    superseded_by=form.get("superseded_by", ""),
                    updates=form.get("updates", ""),
                )
                done = result["status"]
            elif path == "/reject":
                done = "rejected-queued" if reject_bundle(bundle) == "queued" else "rejected"
            else:
                self._send(404, "not found")
                return
        except Refused as error:
            stored = ""
            if error.stored is not None:
                stored = (
                    "<p>The server keeps this text under that id:</p>"
                    f"<pre>{html.escape(error.stored)}</pre>"
                    "<p>To replace it, approve a new note with Supersedes set to that id.</p>"
                )
            self._send(
                error.error.status if 400 <= error.error.status < 500 else 400,
                f"<p>{html.escape(str(error))}</p>{stored}<p>The bundle is kept.</p><p><a href='/'>Back</a></p>",
            )
            return
        except (SystemExit, ValueError, OSError, server_client.ConfigError) as error:
            self._send(400, f"<p>{html.escape(str(error))}</p><p><a href='/'>Back</a></p>")
            return
        self.send_response(303)
        self.send_header("Location", f"/?done={done}")
        self.end_headers()


def review_state_path():
    return store_root() / "data" / "review.json"


def review_alive(state):
    pid = state.get("pid")
    port = state.get("port")
    if not isinstance(pid, int) or not isinstance(port, int):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def review(args):
    warn_legacy()
    sync_first(quiet=False)
    state_path = review_state_path()
    if state_path.is_file():
        state = read_json(state_path)
        if review_alive(state):
            url = f"http://127.0.0.1:{state['port']}"
            print(url)
            webbrowser.open(url)
            return 0
    port = args.port or PORT_START
    server = None
    for candidate in range(port, port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), ReviewHandler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"no free port from {port}")
    write_json(state_path, {"pid": os.getpid(), "port": port})
    url = f"http://127.0.0.1:{port}"
    print(url)
    webbrowser.open(url)
    try:
        server.serve_forever()
    finally:
        if state_path.is_file():
            state_path.unlink()
    return 0


def present_tools():
    checks = {
        "cursor": HOME / ".cursor",
        "claude-code": HOME / ".claude",
        "codex": HOME / ".codex",
        "pi": HOME / ".pi" / "agent",
    }
    binaries = {"cursor": "cursor", "claude-code": "claude", "codex": "codex", "pi": "pi"}
    found = []
    for name, path in checks.items():
        if path.exists() or shutil.which(binaries[name]):
            found.append(name)
    return found


def hook_command(tool, event):
    return f"{BIN} capture --tool {tool} --event {event}"


def cursor_script():
    return f"""#!/bin/sh
# {MARKER}
exec {BIN} capture --tool cursor
"""


def write_cursor(home):
    hooks_dir = home / ".cursor" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    script = hooks_dir / "agent-memory.sh"
    script.write_text(cursor_script(), encoding="utf-8")
    script.chmod(0o755)
    path = home / ".cursor" / "hooks.json"
    data = read_json(path) if path.is_file() else {"version": 1, "hooks": {}}
    hooks = data.setdefault("hooks", {})
    for event in ("stop", "sessionEnd"):
        entries = [item for item in hooks.get(event, []) if "agent-memory.sh" not in item.get("command", "")]
        entries.append({"command": "./hooks/agent-memory.sh"})
        hooks[event] = entries
    write_json(path, data)


def command_entries(existing, command):
    kept = []
    for group in existing:
        inner = [item for item in group.get("hooks", []) if MARKER not in item.get("command", "") and "agent-memory capture" not in item.get("command", "")]
        if inner:
            group = dict(group)
            group["hooks"] = inner
            kept.append(group)
    kept.append({"hooks": [{"type": "command", "command": command}]})
    return kept


def write_claude(home):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_json(path) if path.is_file() else {}
    hooks = data.setdefault("hooks", {})
    hooks["Stop"] = command_entries(hooks.get("Stop", []), hook_command("claude-code", "stop"))
    hooks["SessionEnd"] = command_entries(
        hooks.get("SessionEnd", []),
        hook_command("claude-code", "session-end"),
    )
    write_json(path, data)


def codex_hooks_in_toml(home):
    path = home / ".codex" / "config.toml"
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "[hooks]":
            return True
    return False


def write_codex(home):
    if codex_hooks_in_toml(home):
        raise SystemExit("Codex already has [hooks] in ~/.codex/config.toml. Remove that table, then run install again. This tool writes ~/.codex/hooks.json only.")
    directory = home / ".codex"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hooks.json"
    data = read_json(path) if path.is_file() else {"hooks": {}}
    hooks = data.setdefault("hooks", {})
    hooks["Stop"] = command_entries(hooks.get("Stop", []), hook_command("codex", "stop"))
    hooks["SessionEnd"] = command_entries(
        hooks.get("SessionEnd", []),
        hook_command("codex", "session-end"),
    )
    write_json(path, data)


def write_pi(home):
    directory = home / ".pi" / "agent" / "extensions" / "agent-memory"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "index.ts"
    path.write_text(
        f"""// {MARKER}
import {{ spawn }} from "node:child_process";

function send(eventName: string, event: {{ reason?: string; aborted?: boolean }}, ctx: any) {{
  const payload = JSON.stringify({{
    hook_event_name: eventName,
    session_id: ctx.sessionManager.getSessionId(),
    transcript_path: ctx.sessionManager.getSessionFile(),
    cwd: ctx.cwd,
    reason: event?.reason,
    aborted: event?.aborted,
  }});
  const child = spawn("{BIN}", ["capture", "--tool", "pi", "--event", eventName], {{
    stdio: ["pipe", "ignore", "ignore"],
  }});
  child.stdin.write(payload);
  child.stdin.end();
}}

export default function (pi: any) {{
  pi.on("agent_settled", async (event: {{ aborted?: boolean }}, ctx: any) => {{
    send("agent_settled", event, ctx);
  }});
  pi.on("session_shutdown", async (event: {{ reason?: string }}, ctx: any) => {{
    send("session_shutdown", event, ctx);
  }});
}}
""",
        encoding="utf-8",
    )


WRITERS = {
    "cursor": write_cursor,
    "claude-code": write_claude,
    "codex": write_codex,
    "pi": write_pi,
}


def prompt_yes_no(label, default_no=True):
    if not sys.stdin.isatty():
        raise SystemExit("re-run in a terminal, or pass --yes with explicit flags")
    suffix = "[y/N]" if default_no else "[Y/n]"
    answer = input(f"{label} {suffix} ").strip().lower()
    if not answer:
        return not default_no
    return answer in {"y", "yes"}


def install(args):
    tools = present_tools()
    if not tools and not args.git_repo:
        raise SystemExit("no Cursor, Claude Code, Codex, or Pi install was found (pass --git-repo for repo-only setup)")

    if tools:
        print("This will write capture hooks for:")
        for tool in tools:
            print(f"  {tool}")
        print("Other hooks already in those files are left in place.")
        if not args.yes:
            if not sys.stdin.isatty():
                raise SystemExit("re-run in a terminal, or pass --yes")
            answer = input("Write these hooks? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("editor hooks left unchanged")
            else:
                for tool in tools:
                    WRITERS[tool](HOME)
                    print(f"wrote {tool}")
        else:
            for tool in tools:
                WRITERS[tool](HOME)
                print(f"wrote {tool}")

    if args.git_repo:
        repo = Path(args.git_repo).resolve()
        if not repo.is_dir():
            raise SystemExit(f"not a directory: {repo}")
        if run(["git", "rev-parse", "--is-inside-work-tree"], str(repo)) != "true":
            raise SystemExit(f"not a git repository: {repo}")

        want_git = False
        auto_ap = False
        if args.yes:
            gh = getattr(args, "install_git_hooks", None)
            if gh is None:
                raise SystemExit("with --yes --git-repo, pass --install-git-hooks true|false")
            want_git = gh == "true"
            aa = getattr(args, "install_auto_approve", None)
            auto_ap = aa == "true" if aa is not None else False
        else:
            print()
            print("Git hooks: after each commit or merge, file an inbox bundle for this repo")
            print(f"  ({repo})")
            want_git = prompt_yes_no(
                "Install post-commit and post-merge hooks here?",
                default_no=True,
            )
            if want_git:
                auto_ap = prompt_yes_no(
                    "Auto-approve those memories (skip review UI)?",
                    default_no=True,
                )
            else:
                auto_ap = False

        if want_git:
            hooks_dir = write_git_hooks(str(repo))
            print(f"wrote git hooks in {hooks_dir}")
        else:
            remove_git_hooks(str(repo))
        updates = {"git_hooks": want_git, "auto_approve": auto_ap}
        write_repo_config(str(repo), updates)
        print(f"repo policy: {repo_policy_path(repo)}")

    return 0


def config_cmd(args):
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        raise SystemExit(f"not a directory: {repo}")
    if run(["git", "rev-parse", "--is-inside-work-tree"], str(repo)) != "true":
        raise SystemExit(f"not a git repository: {repo}")
    repo_s = str(repo)

    def snapshot():
        c = read_repo_config(repo_s)
        return {
            "repo": repo_s,
            "git_hooks_config": c.get("git_hooks", False),
            "git_hooks_installed": git_hooks_installed(repo_s),
            "auto_approve": c.get("auto_approve", False),
            "capture": c.get("capture", True),
        }

    gh_arg = getattr(args, "git_hooks", None)
    mut = args.auto_approve is not None or gh_arg is not None
    if not mut:
        show = snapshot()
        if getattr(args, "json_out", False):
            print(json.dumps(show, indent=2))
        else:
            print(f"repo: {repo_s}")
            print(f"git_hooks (config): {show['git_hooks_config']}")
            print(f"git_hooks (installed): {show['git_hooks_installed']}")
            print(f"auto_approve: {show['auto_approve']}")
            print(f"capture: {show['capture']}")
        return 0

    updates = {}
    if args.auto_approve is not None:
        updates["auto_approve"] = args.auto_approve == "true"
    if gh_arg is not None:
        updates["git_hooks"] = gh_arg == "true"
        if gh_arg == "true":
            hooks_dir = write_git_hooks(repo_s)
            print(f"wrote git hooks in {hooks_dir}")
        else:
            remove_git_hooks(repo_s)
            print("removed agent-memory git hooks (post-commit, post-merge)")
    if updates:
        write_repo_config(repo_s, updates)
        print(f"updated {repo_policy_path(repo)}")

    show = snapshot()
    if getattr(args, "json_out", False):
        print(json.dumps(show, indent=2))
    else:
        print(f"git_hooks (config): {show['git_hooks_config']}")
        print(f"git_hooks (installed): {show['git_hooks_installed']}")
        print(f"auto_approve: {show['auto_approve']}")
    return 0


def strip_commands(groups):
    kept = []
    for group in groups:
        inner = [
            item
            for item in group.get("hooks", [])
            if MARKER not in item.get("command", "") and "agent-memory capture" not in item.get("command", "")
        ]
        if inner:
            group = dict(group)
            group["hooks"] = inner
            kept.append(group)
    return kept


def uninstall(args):
    cursor_hooks = HOME / ".cursor" / "hooks.json"
    if cursor_hooks.is_file():
        data = read_json(cursor_hooks)
        hooks = data.get("hooks", {})
        for event in ("stop", "sessionEnd"):
            hooks[event] = [item for item in hooks.get(event, []) if "agent-memory.sh" not in item.get("command", "")]
            if not hooks[event]:
                hooks.pop(event, None)
        write_json(cursor_hooks, data)
    script = HOME / ".cursor" / "hooks" / "agent-memory.sh"
    if script.is_file() and MARKER in script.read_text(encoding="utf-8"):
        script.unlink()

    settings = HOME / ".claude" / "settings.json"
    if settings.is_file():
        data = read_json(settings)
        hooks = data.get("hooks", {})
        for event in ("Stop", "SessionEnd"):
            hooks[event] = strip_commands(hooks.get(event, []))
            if not hooks[event]:
                hooks.pop(event, None)
        write_json(settings, data)

    codex = HOME / ".codex" / "hooks.json"
    if codex.is_file():
        data = read_json(codex)
        hooks = data.get("hooks", {})
        for event in ("Stop", "SessionEnd"):
            hooks[event] = strip_commands(hooks.get(event, []))
            if not hooks[event]:
                hooks.pop(event, None)
        write_json(codex, data)

    extension = HOME / ".pi" / "agent" / "extensions" / "agent-memory" / "index.ts"
    if extension.is_file() and MARKER in extension.read_text(encoding="utf-8"):
        shutil.rmtree(extension.parent)
    print("removed agent-memory hooks")
    return 0


LIST_FILTERS = ("team", "org", "repo", "person", "since", "until", "kind", "domain", "tool", "session_id", "limit", "offset")


def _server_config():
    try:
        return server_client.read_config()
    except server_client.ConfigError as error:
        raise SystemExit(str(error)) from None


def list_from_server(args, config):
    remote = (args.database or "").strip()
    if config["mode"] == "shared" and remote:
        raise SystemExit("database is only used in per-repo mode")
    if config["mode"] == "per-repo" and not remote:
        remote = git_remote_repo_id(os.getcwd())
        if not remote:
            raise SystemExit("per-repo list needs --database <remote id>")
    try:
        url, token = server_client.target(remote or None, config)
        filters = {name: getattr(args, name) for name in LIST_FILTERS}
        hits = server_client.Client(url, token).list_memories(**filters)
    except (server_client.ConfigError, server_client.ServerError, server_client.ServerUnreachable) as error:
        raise SystemExit(server_client.explain(error)) from None
    print(json.dumps(hits, indent=2))
    return 0


def list_memories(args):
    config = _server_config()
    if config is None:
        raise SystemExit(NO_SERVER)
    warn_legacy()
    sync_first(quiet=False)
    return list_from_server(args, config)


def flush_cmd(args):
    """Retired with the cutover: Approve sends to the server, and the outbox holds what waits."""
    print("flush is retired: use `agent-memory sync`", file=sys.stderr)
    return 1


def sync_cmd(args):
    """Send the outbox to the server, in order. Exit 1 when anything is left waiting or failed."""
    root = store_root()
    if not args.quiet:
        warn_legacy()
    try:
        report = outbox.sync(root, client_for, retry=args.retry_failed)
    except OSError as error:
        if args.quiet:
            log(f"sync failed: {error}")
            return 1
        raise SystemExit(f"sync failed: {error}") from None
    lines = report.lines(root)
    if args.quiet:
        if report.sent or report.failed or report.stopped:
            for line in lines:
                log(f"sync: {line}")
    else:
        for line in lines:
            print(line)
    left = outbox.summary(root)
    return 1 if left["queued"] or report.failed else 0


def describe_caller(who):
    if who.get("auth") == "off":
        return "auth is off, so identity comes from ~/.config/agent-memory/identity"
    scopes = ", ".join(who.get("scopes") or []) or "none"
    label = f" (token {who.get('label')})" if who.get("label") else ""
    return f"{who.get('person')} in {who.get('org')}/{who.get('team')}, scopes: {scopes}{label}"


def connect_cmd(args):
    """Check the server with /v1/whoami, then write the config. Nothing is written when the check fails."""
    try:
        url = server_client.clean_url(args.url)
        token = server_client.check_token(sys.stdin.readline()) if args.token_stdin else None
        if args.token_stdin and not token:
            raise server_client.ConfigError("--token-stdin read an empty line")
        existing = server_client.read_config()
        mode = args.mode or (existing["mode"] if existing else server_client.legacy_mode())
        who = server_client.Client(url, token).whoami()
    except server_client.ConfigError as error:
        raise SystemExit(str(error)) from None
    except (server_client.ServerError, server_client.ServerUnreachable) as error:
        raise SystemExit(server_client.explain(error)) from None
    if token and who.get("auth") == "off":
        # Approve takes a token to mean the server pins person; with auth off it doesn't.
        print("the server has auth off, so the token is not saved")
        token = None
    remote = (args.repo or "").strip()
    if remote:
        if existing is None or existing["mode"] != "per-repo":
            raise SystemExit("--repo maps a remote in per-repo mode: connect the default server with --mode per-repo first")
        servers, tokens = server_client.map_repo(remote, url, token)
        print(f"{remote} now goes to {url} ({servers}, {tokens})")
    else:
        path = server_client.write_config(url, token, mode)
        print(f"wrote {path} ({mode} mode)")
    print(f"connected to {url} as {describe_caller(who)}")
    if who.get("auth") == "on" and "write" not in (who.get("scopes") or []):
        print("this token can't write: Approve needs a token with the write scope")
    return 0


def migrate_cmd(args):
    try:
        return memory_migrate.migrate(dry_run=args.dry_run)
    except (server_client.ConfigError, memory_store.StoreError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
    except (server_client.ServerError, server_client.ServerUnreachable) as error:
        raise SystemExit(server_client.explain(error)) from None


def build_parser():
    parser = argparse.ArgumentParser(prog="agent-memory")
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser("capture", help="write an inbox bundle from hook JSON on stdin")
    capture_parser.add_argument("--tool", required=True, choices=["cursor", "claude-code", "codex", "pi"])
    capture_parser.add_argument("--event", default="")
    capture_parser.set_defaults(func=capture)

    review_parser = sub.add_parser("review", help="open the approve/reject page on 127.0.0.1")
    review_parser.add_argument("--port", type=int)
    review_parser.set_defaults(func=review)

    install_parser = sub.add_parser("install", help="write editor hooks after a yes")
    install_parser.add_argument("--yes", action="store_true", help="write without prompting")
    install_parser.add_argument(
        "--git-repo",
        metavar="PATH",
        default="",
        help="optional git repo: prompt to install post-commit/post-merge hooks and policy",
    )
    install_parser.add_argument(
        "--install-git-hooks",
        choices=["true", "false"],
        default=None,
        help="with --yes --git-repo: install or skip git hooks (required with --yes)",
    )
    install_parser.add_argument(
        "--install-auto-approve",
        choices=["true", "false"],
        default=None,
        help="with --yes --git-repo: auto-approve on (default false if omitted)",
    )
    install_parser.set_defaults(func=install)

    uninstall_parser = sub.add_parser("uninstall", help="remove only the hooks this tool added")
    uninstall_parser.set_defaults(func=uninstall)

    note_parser = sub.add_parser("note", help="pin a draft memory so review can approve it")
    note_parser.add_argument("--session-id", required=True)
    note_parser.add_argument("--kind", required=True, choices=KINDS)
    note_parser.add_argument("--domain", required=True, choices=DOMAINS)
    note_parser.add_argument("--text", help="memory text; omit to read stdin")
    note_parser.set_defaults(func=note)

    keep_parser = sub.add_parser("keep", help="pin a session so review shows it")
    keep_parser.add_argument("session_id")
    keep_parser.add_argument("--tool", choices=["cursor", "claude-code", "codex", "pi", "git"])
    keep_parser.set_defaults(func=keep)

    drop_parser = sub.add_parser("drop", help="delete an inbox bundle")
    drop_parser.add_argument("session_id")
    drop_parser.add_argument("--tool", choices=["cursor", "claude-code", "codex", "pi", "git"])
    drop_parser.set_defaults(func=drop)

    land_parser = sub.add_parser(
        "land",
        help="file inbox bundles for recent default-branch commits (grouped by PR when possible)",
    )
    land_parser.add_argument(
        "repo",
        help="path to the git repository to scan",
    )
    land_parser.add_argument(
        "--since",
        default="",
        help="start time (ISO-8601) or integer hours ago; default: 24 hours before now",
    )
    land_parser.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="used as the default window when --since is omitted (default: 24)",
    )
    land_parser.set_defaults(func=land)

    git_hook_parser = sub.add_parser("git-hook", help="run from repo post-commit/post-merge (internal)")
    git_hook_parser.add_argument("--repo", default="", help="repository root (default: cwd)")
    git_hook_parser.add_argument("--event", default="post-commit", help="post-commit or post-merge")
    git_hook_parser.set_defaults(func=git_hook)

    config_parser = sub.add_parser("config", help="show or set repo policy (.agent-memory.toml)")
    config_parser.add_argument("repo", help="path to the git repository")
    config_parser.add_argument(
        "--auto-approve",
        choices=["true", "false"],
        default=None,
        help="turn auto-approve on or off",
    )
    config_parser.add_argument(
        "--git-hooks",
        choices=["true", "false"],
        default=None,
        dest="git_hooks",
        help="install or remove post-commit/post-merge hooks",
    )
    config_parser.add_argument("--json", dest="json_out", action="store_true", help="machine-readable output")
    config_parser.set_defaults(func=config_cmd)

    list_parser = sub.add_parser("list", help="query approved memories by team, org, repo, and date")
    list_parser.add_argument("--team", default=None)
    list_parser.add_argument("--org", default=None)
    list_parser.add_argument("--repo", default=None, help="remote id, for example github.com/owner/name")
    list_parser.add_argument("--person", default=None)
    list_parser.add_argument("--since", default=None, help="occurred_at lower bound, ISO-8601")
    list_parser.add_argument("--until", default=None, help="occurred_at upper bound, ISO-8601")
    list_parser.add_argument("--kind", default=None, choices=KINDS)
    list_parser.add_argument("--domain", default=None, choices=DOMAINS)
    list_parser.add_argument("--tool", default=None)
    list_parser.add_argument("--session-id", default=None)
    list_parser.add_argument("--database", default=None, help="per-repo remote id; omit in shared mode")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--offset", type=int, default=0)
    list_parser.set_defaults(func=list_memories)

    connect_parser = sub.add_parser("connect", help="point the CLI at an agent-memory server")
    connect_parser.add_argument("--url", required=True, help="server URL, for example http://127.0.0.1:8765")
    connect_parser.add_argument(
        "--token-stdin", action="store_true", help="read the bearer token from the first line of stdin"
    )
    connect_parser.add_argument("--mode", choices=server_client.MODES, default=None, help="default: keep the current mode")
    connect_parser.add_argument("--repo", default="", help="per-repo mode: send this remote id to --url")
    connect_parser.set_defaults(func=connect_cmd)

    migrate_parser = sub.add_parser("migrate", help="copy the local memories.sqlite to the connected server")
    migrate_parser.add_argument("--dry-run", action="store_true", help="print the counts and send nothing")
    migrate_parser.set_defaults(func=migrate_cmd)

    sync_parser = sub.add_parser("sync", help="send the outbox to the server, in order")
    sync_parser.add_argument(
        "--retry-failed", action="store_true", help="move outbox/failed/ back into the queue first"
    )
    sync_parser.add_argument("--quiet", action="store_true", help="write to capture.log only (hooks)")
    sync_parser.set_defaults(func=sync_cmd)

    flush_parser = sub.add_parser("flush", help="retired: use agent-memory sync")
    flush_parser.add_argument("--repo", default="", help=argparse.SUPPRESS)
    flush_parser.set_defaults(func=flush_cmd)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    raise SystemExit(0 if result is None else result)


if __name__ == "__main__":
    main()
