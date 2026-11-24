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
from the server. `connect --editor` registers the server's read-only MCP
endpoint in Claude Code, Cursor and Codex, with a read token for each.
"""

import argparse
import html
import json
import os
import re
import shlex
import shutil
import signal
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
import editors
import local_state
import memory_migrate
import memory_rules
import memory_store
import outbox
import server_client
HOME = Path.home()
# The clone's own wrapper. Hooks point here only when install runs from a clone
# without a tool install; see hook_bin().
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
    """inbox/, outbox/, review.json and capture.log: the XDG data directory, or AGENT_MEMORY_DIR."""
    return local_state.root()


def inbox_root():
    return store_root() / "inbox"


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(message):
    path = store_root() / "capture.log"
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


def write_git_hooks(repo, command=None):
    """Install post-commit and post-merge hooks in the directory git will run.

    A hook we did not write is moved aside and called after ours. Reinstall
    rewrites only our script and leaves that saved hook in place. The hooks run
    command, by default the agent-memory running now (hook_bin()).
    """
    command = command or hook_bin()
    repo = str(Path(repo).resolve())
    hooks_dir = git_hooks_dir(repo)
    if hooks_dir is None:
        raise SystemExit(f"could not locate the git hooks directory for {repo}")
    if hooks_dir.exists() and not hooks_dir.is_dir():
        raise SystemExit(f"git hooks path is not a directory: {hooks_dir}")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    bin_quoted = sh_quote(command)
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


def no_pr_description(repo, pr_number):
    """The hook's describe: no gh call, so the hook never waits on GitHub."""
    return "", ""


def no_pr_for_sha(sha):
    return None


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


def _rev_list(repo, spec):
    listing = run(["git", "rev-list", "--reverse", spec], repo)
    return [line.strip() for line in listing.splitlines() if line.strip()]


def hook_commit_shas(repo, event, head):
    """Commits this hook invocation brought onto the branch, oldest first, merge commits included.

    post-merge: every commit reachable from HEAD but not from ORIG_HEAD, so a
    fast-forward or merge does not keep only the tip. After `git merge --squash`
    git runs post-merge with ORIG_HEAD at HEAD: nothing is new, and the squash
    commit comes later, through post-commit.

    post-commit: the new HEAD. When HEAD is a merge (a conflicted merge that
    `git commit` concluded, which runs no post-merge), the commits it merged.

    The caller groups them as land does: a merge commit's own message is never
    filed, it only names the pull request of the commits it brought in.
    """
    if event == "post-merge":
        orig = run(["git", "rev-parse", "--verify", "--quiet", "ORIG_HEAD"], repo)
        if orig == head:
            return []
        if orig:
            return _rev_list(repo, f"{orig}..{head}")
    if is_merge_commit(repo, head):
        return _rev_list(repo, f"{head}^1..{head}")
    return [head]


def git_group_session_id(repo_key, group):
    """The session id land and the hook both give a group, so one group files one bundle."""
    shas = [m["sha"] for m in group["commits"]]
    prn = group.get("pr")
    return land_session_id(repo_key, f"pr-{prn}" if prn is not None else shas[0], prn, shas)


def git_bundle(repo, session_id, group, event, branch, tip, remote_id, draft_text):
    """An inbox bundle for one group of default-branch commits, from land or the git hook."""
    members = group["commits"]
    shas = [m["sha"] for m in members]
    return {
        "tool": "git",
        "session_id": session_id,
        "repo": repo,
        "started_at": started_at_for_group(group),
        "ended_at": occurred_at_for_group(group),
        "event": event,
        "closed": True,
        "transcript_copy": None,
        "last_assistant_message": "",
        "git": {
            "branch": branch,
            "head": tip,
            "status": "",
            "diff_stat": "",
            "paths": git_paths_for_group(repo, shas),
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
            "pull_request": group.get("pr"),
            "commits": [
                {"sha": m["sha"], "subject": (m["message"] or "").split("\n", 1)[0].strip()}
                for m in members
            ],
        },
    }


def file_hook_group(repo_s, group, event, branch, tip, remote_id, auto_approve):
    """File one group of commits locally: an approved document in the outbox, or a bundle in the inbox.

    Returns the path written, or None when a bundle for this group already
    waits in the inbox (land filed it, or an earlier hook did).
    """
    session_id = git_group_session_id(remote_id or repo_s, group)
    bundle_path = land_bundle_path("git", session_id)
    if bundle_path.is_file():
        return None
    draft_text = draft_text_for_group(repo_s, group, describe=no_pr_description)
    bundle = git_bundle(
        repo_s, session_id, group, "git-" + event.replace("_", "-"), branch, tip, remote_id, draft_text
    )
    if auto_approve:
        try:
            kind, domain, text, commit = draft_choice(bundle)
            url, body = approved_request(bundle, kind, domain, text, commit=commit)
            # The hook's own status call may time out, so sync asks again before
            # it sends: a commit someone else already explained gets no second memory.
            shas = [m["sha"] for m in group["commits"]]
            # A PR is dropped only when all its commits are settled: a duplicate
            # costs one Reject, a missed memory is lost.
            check = {"repo": remote_id, "shas": shas} if remote_id else None
            if check and len(shas) > 1:
                check["all"] = True
            return outbox.add(store_root(), url, "memory", body, unless_settled=check)
        except (ValueError, OSError, server_client.ConfigError) as error:
            log(f"git-hook: auto_approve failed, inbox bundle kept for review: {error}")
    write_json(bundle_path, bundle)
    return bundle_path


def _unfile(path):
    if path.name == "bundle.json":
        shutil.rmtree(path.parent, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def git_hook(args):
    """File the new default-branch commits locally first, check them with one short call, then start a detached sync.

    Only commits on the repo's default branch are filed, the branch land reads:
    a commit on a feature branch is filed when it reaches that branch. Commits
    are grouped by pull request the way land groups them, from the messages
    alone. The hook makes no other network call (no gh), so it returns within
    about two seconds even when the server doesn't answer.
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
    branch = run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], repo_s)
    default = local_default_branch(repo_s, fall_back_to_head=False)
    if not default:
        log(
            "git-hook skipped: no local default branch (origin/HEAD's branch, main or master); "
            "set origin/HEAD with git remote set-head origin --auto, or check out that branch once"
        )
        return 0
    if branch != default:
        log(f"git-hook skipped: {branch or 'detached HEAD'} is not the default branch {default}")
        return 0
    head = run(["git", "rev-parse", "HEAD"], repo_s)
    if not head:
        log("git-hook skipped: could not resolve HEAD")
        return 0
    shas = hook_commit_shas(repo_s, event, head)
    if not shas:
        return 0
    groups, _ = build_land_groups(repo_s, commit_records(repo_s, shas), pr_for_sha=no_pr_for_sha)
    remote_id = git_remote_repo_id(repo_s)
    waiting = waiting_settled(remote_id, [m["sha"] for group in groups for m in group["commits"]])
    auto_approve = bool(cfg.get("auto_approve"))

    def file(group):
        return file_hook_group(repo_s, group, event, branch, head, remote_id, auto_approve)

    written = []
    for group in groups:
        members = [m for m in group["commits"] if m["sha"] not in waiting]
        if not members:
            continue
        group = dict(group, commits=members)
        path = file(group)
        if path is not None:
            written.append((path, group))
    # Commits pulled in by a merge may be settled already, by someone else or
    # by another laptop. Such a group is filed again with only the rest, so no
    # commit gets a second memory. On any failure the files stay.
    settled = server_settled(
        remote_id, [m["sha"] for _, group in written for m in group["commits"]], timeout=HOOK_STATUS_TIMEOUT
    )
    for path, group in written:
        rest = [m for m in group["commits"] if m["sha"] not in settled]
        if len(rest) == len(group["commits"]):
            continue
        _unfile(path)
        if rest:
            file(dict(group, commits=rest))
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


# One gh call answers land's whole window; past this many PRs it asks per commit.
GH_PR_LIMIT = 1000


class GhMergedPrs:
    """The PRs merged since a date, from one `gh pr list` call, for land.

    land used to ask gh once per commit without a PR marker and once per PR for
    its description, which took about 20 s over a long history. This asks once,
    lazily, and answers both questions from that list. When the list fails, or
    is cut at GH_PR_LIMIT, what it doesn't hold is asked for alone, as before.
    """

    def __init__(self, repo, since_iso):
        self.repo = repo
        self.since_iso = since_iso
        self._prs = None
        self._loaded = False
        self.complete = False

    def _load(self):
        if self._loaded:
            return self._prs
        self._loaded = True
        try:
            since = datetime.fromisoformat(str(self.since_iso).replace("Z", "+00:00"))
            day = (since - timedelta(days=1)).date().isoformat()
        except ValueError:
            day = ""
        command = ["gh", "pr", "list", "--state", "merged", "--json", "number,title,body,mergeCommit"]
        command += ["--limit", str(GH_PR_LIMIT)]
        if day:
            command += ["--search", f"merged:>={day}"]
        out = run(command, self.repo)
        try:
            rows = json.loads(out) if out else None
        except json.JSONDecodeError:
            rows = None
        if not isinstance(rows, list):
            return None
        prs = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("number"), int):
                continue
            prs[row["number"]] = {
                "title": (row.get("title") or "").strip(),
                "body": (row.get("body") or "").strip(),
                "merge_commit": ((row.get("mergeCommit") or {}).get("oid") or "").lower(),
            }
        self._prs = prs
        self.complete = len(rows) < GH_PR_LIMIT
        self._by_merge_commit = {pr["merge_commit"]: n for n, pr in prs.items() if pr["merge_commit"]}
        return prs

    def pr_for_sha(self, sha):
        prs = self._load()
        if prs is None:
            return gh_pr_for_sha(self.repo, sha)
        found = self._by_merge_commit.get(sha.lower())
        if found is not None or self.complete:
            return found
        return gh_pr_for_sha(self.repo, sha)

    def describe(self, repo, pr_number):
        prs = self._load()
        if prs is not None and pr_number in prs:
            return prs[pr_number]["title"], prs[pr_number]["body"]
        return gh_pr_description(repo, pr_number)


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
        outbox.add(root, url, op, body, unless_settled=unless_settled)
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


# Unit separator between fields, record separator after each commit.
# A commit message cannot end up holding either one.
_LOG_FORMAT = "--pretty=format:%H%x1f%ct%x1f%B%x1e"


def commits_on_branch_since(repo, branch, since_iso):
    return _log_records(run(["git", "log", branch, f"--since={since_iso}", "--reverse", _LOG_FORMAT], repo))


def commit_records(repo, shas):
    """{sha, committer_date, message} for each sha, in the order given."""
    records = []
    # In batches, so a pull of many thousands of commits stays under ARG_MAX.
    for start in range(0, len(shas), 500):
        batch = shas[start : start + 500]
        records.extend(_log_records(run(["git", "log", "--no-walk=unsorted", _LOG_FORMAT, *batch], repo)))
    return records


def _log_records(listing):
    commits = []
    for block in listing.split("\x1e"):
        block = block.strip("\n")
        if not block.strip():
            continue
        # An empty message leaves "sha\x1fct", once run() strips the output's end.
        parts = block.split("\x1f", 2)
        if len(parts) == 2:
            parts.append("")
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


def merge_titles(merges):
    """PR number -> the title GitHub writes under "Merge pull request #N from …"."""
    titles = {}
    for item in merges:
        prn = pr_from_commit_message(item["message"])
        if prn is None:
            continue
        _, _, rest = (item["message"] or "").strip().partition("\n")
        # Skip the "# Conflicts:" block git leaves when `git commit --no-edit` concludes a merge.
        lines = [line.strip() for line in clean_message(rest).splitlines()]
        title = next((line for line in lines if line and not line.startswith("#")), "")
        if title:
            titles.setdefault(prn, title)
    return titles


def build_land_groups(repo, commits, pr_for_sha=None):
    """Split off merge commits; group the rest by PR number when present.

    A root commit has no parent. It is filed like any single-parent commit.
    pr_for_sha(sha) is the last way to tell a commit's PR (gh_pr_for_sha by
    default); the git hook passes one that never calls gh.
    """
    pr_for_sha = pr_for_sha or (lambda sha: gh_pr_for_sha(repo, sha))
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
            prn = pr_for_sha(item["sha"])
        if prn is not None:
            by_pr.setdefault(prn, []).append(item)
        else:
            orphan_singles.append(item)

    titles = merge_titles(merges)
    for prn, members in sorted(by_pr.items()):
        group = {"pr": prn, "commits": sorted(members, key=lambda x: x["committer_date"])}
        if prn in titles:
            group["merge_title"] = titles[prn]
        groups.append(group)

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


def draft_text_for_group(repo, group, describe=None):
    """PR title and description, then each commit message, each said once.

    The PR number, shas, and repo id are stored beside the text, so
    they are not repeated in what gets embedded. describe(repo, n) gives the
    PR's title and body (gh_pr_description by default). Without one, the title
    is the one a "Merge pull request" commit carries, if any.
    """
    describe = describe or gh_pr_description
    parts = []
    title = ""
    pr_number = group.get("pr")
    if pr_number is not None:
        title, body = describe(repo, pr_number)
        title = title or group.get("merge_title") or ""
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


def started_at_for_group(group):
    """The group's oldest commit date: when the work it explains was committed."""
    times = [m["committer_date"] for m in group["commits"] if m.get("committer_date")]
    if not times:
        return now()
    return datetime.fromtimestamp(min(times), tz=timezone.utc).replace(microsecond=0).isoformat()


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
    gh_prs = GhMergedPrs(str(repo), since)
    groups, merge_notes = build_land_groups(str(repo), commits, pr_for_sha=gh_prs.pr_for_sha)

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
        session_id = git_group_session_id(remote_id or str(repo), group)
        bundle_path = land_bundle_path(tool, session_id)
        if bundle_path.is_file():
            continue
        draft_text = draft_text_for_group(str(repo), group, describe=gh_prs.describe)
        write_json(bundle_path, git_bundle(str(repo), session_id, group, "land", branch, tip, remote_id, draft_text))
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


# How much of a large transcript's head and tail transcript_times reads first,
# and how far it widens a window that holds no timestamped line (one huge line).
TRANSCRIPT_TIME_WINDOW = 1 << 20
TRANSCRIPT_TIME_LIMIT = 8 << 20


def _transcript_line_time(line):
    """The top-level "timestamp" of one JSONL line, in UTC, or None."""
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    value = entry.get("timestamp") if isinstance(entry, dict) else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def transcript_times(path):
    """The first and last times a JSONL transcript records, as UTC text, or None for each.

    Claude Code and Codex put an ISO time on each line's top-level "timestamp".
    Cursor's agent transcript has none, so the caller falls back to now. Only
    the times are read: nothing else from the transcript reaches the bundle. A
    large file is read at its head and its tail only, to keep a hook fast. A
    window that holds no whole line (one huge line) widens, up to
    TRANSCRIPT_TIME_LIMIT.
    """

    def found(lines):
        return [t for t in map(_transcript_line_time, lines or []) if t is not None]

    head = tail = None
    window = TRANSCRIPT_TIME_WINDOW
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            while True:
                if size <= 2 * window:
                    handle.seek(0)
                    head = tail = handle.read().splitlines()
                    break
                if not head:
                    handle.seek(0)
                    head = handle.read(window).splitlines()[:-1]
                if not tail:
                    handle.seek(size - window)
                    tail = handle.read(window).splitlines()[1:]
                if (head and tail) or window >= TRANSCRIPT_TIME_LIMIT:
                    break
                window *= 2
    except OSError:
        return None, None

    def text(moment):
        return moment.replace(microsecond=0).isoformat()

    first = found(head)
    last = first if tail is head else found(tail)
    return (text(min(first)) if first else None, text(max(last)) if last else None)


def session_times(first_at, last_at, previous):
    """started_at and ended_at for a capture: the transcript's, else the earlier capture's or now.

    When only one side came from the transcript (a line too long to read), the
    start is kept no later than the end, so the landing check still has a window.
    """
    ended = last_at or now()
    started = first_at or previous.get("started_at") or now()
    try:
        if datetime.fromisoformat(started) > datetime.fromisoformat(ended):
            started = ended
    except (TypeError, ValueError):
        pass
    return started, ended


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
    started = previous.get("captured_at") or previous.get("started_at")
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
    # The session's own times, so an old transcript captured late keeps its dates.
    source = transcript
    if source is None and copy_name and (folder / copy_name).is_file():
        source = folder / copy_name
    first_at, last_at = transcript_times(source) if source is not None else (None, None)
    started_at, ended_at = session_times(first_at, last_at, previous)
    bundle = {
        "tool": tool,
        "session_id": session_id,
        "repo": repo,
        "started_at": started_at,
        # The first capture of this session id. A resumed or forked transcript can
        # start days earlier, so auto-approve keeps to this time, not started_at.
        "captured_at": previous.get("captured_at") or previous.get("started_at") or now(),
        "ended_at": ended_at,
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


def local_default_branch(repo, fall_back_to_head=True):
    """Local branch `land` should read, and the only one the git hook files from.

    `origin/HEAD` only supplies the name (`main` or `master`). Logging the
    remote-tracking ref skips commits that are on the local branch and not
    pushed, and files commits that are not on the local branch.

    With neither, land reads the checked-out branch. The hook passes
    fall_back_to_head=False and gets "" instead, so it never takes a feature
    branch for the default one.
    """
    ref = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo)
    if ref:
        name = ref.split("/", 1)[1] if "/" in ref else ref
        if ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo):
            return name
    for name in ("main", "master"):
        if ok(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"], repo):
            return name
    if not fall_back_to_head:
        return ""
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


# The tool a note is filed under: no harness wrote it.
NOTE_TOOL = "note"


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
    rr = git_remote_repo_id(repo)
    if not rr:
        raise SystemExit(
            f"note needs a remote repo id, and {repo} has none. Run it inside the git repo the memory is "
            "about, with an origin URL such as https://github.com/owner/name or git@host:owner/name, "
            "or set repo= in its .agent-memory.toml."
        )
    folder = session_dir(NOTE_TOOL, session_id)
    bundle_path = folder / "bundle.json"
    previous = read_json(bundle_path) if bundle_path.is_file() else {}
    # Before v0.1.0 a note was filed as a cursor bundle. Pinning the same id again
    # replaces that one, so the id doesn't match two tools in keep and drop.
    legacy = session_dir("cursor", session_id)
    legacy_path = legacy / "bundle.json"
    replaces_legacy = False
    if legacy_path.is_file():
        # Read it directly: read_json exits on bad JSON, and an unreadable
        # cursor bundle is not a note to replace.
        try:
            old = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        if old.get("event") == "note":
            previous = previous or old
            replaces_legacy = True
    bundle = {
        "tool": NOTE_TOOL,
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
    if replaces_legacy:
        shutil.rmtree(legacy)
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
    if bundle.get("tool") == "git" and body.get("commits") and not queue_only:
        recheck_git_bundle(bundle, {"repo": body["repo"], "shas": list(body["commits"])}, kind, domain, text)
    status, answer = deliver(url, "memory", body, queue_only=queue_only, unless_settled=unless_settled)
    shutil.rmtree(session_dir(bundle["tool"], bundle["session_id"]))
    if status == "sent":
        return {"status": "sent", "id": (answer or {}).get("id"), "reason": None}
    return {"status": "queued", "id": None, "reason": answer}


class AlreadySettled(ValueError):
    """Approve found the git bundle's commits already kept or rejected. Nothing was stored."""

    def __init__(self, message, dropped):
        super().__init__(message)
        self.dropped = dropped


def recheck_git_bundle(bundle, check, kind, domain, text):
    """Ask again, at Approve, whether a git bundle's commits are settled. Raises AlreadySettled.

    A land bundle can wait in review while the git hook auto-approves the same
    commit, or another laptop keeps it. Approving it then would store a second
    memory for that commit. /v1/commits/status is asked, with the page's short
    timeout, and the outbox is read. If the server can't be reached, only the
    outbox is known. A queued approve carries no unless_settled: sync would
    drop the reviewed text if one commit were rejected meanwhile.

    Only what the server reports kept or rejected changes the bundle. When that
    is every commit it lists, it is dropped; when it is some, they are taken
    off it, and the text as edited is kept for the next try. A commit that only
    an outbox item covers (queued, or in failed/) is refused with the bundle
    kept, since that item may never reach the server.
    """
    on_server = server_settled(check["repo"], check["shas"], timeout=PAGE_TIMEOUT)
    waiting = waiting_settled(check["repo"], [sha for sha in check["shas"] if sha not in on_server])
    if not on_server and not waiting:
        return
    folder = session_dir(bundle["tool"], bundle["session_id"])
    land_meta = bundle.get("land") if isinstance(bundle.get("land"), dict) else {}
    listed = [c for c in land_meta.get("commits") or [] if isinstance(c, dict)]
    rest = [c for c in listed if str(c.get("sha") or "") not in on_server]

    def names(found):
        return ", ".join(sha[:10] for sha in check["shas"] if sha in found)

    said = []
    if on_server:
        said.append(f"{names(on_server)}: already kept or rejected on the server")
    if waiting:
        said.append(f"{names(waiting)}: waiting in the outbox (run agent-memory sync, or see its failed items)")
    said = "Not approved, so no commit gets a second memory. " + "; ".join(said) + "."
    if listed and not rest:
        shutil.rmtree(folder, ignore_errors=True)
        raise AlreadySettled(f"{said} Every commit of this bundle is settled, so the bundle was dropped.", True)
    draft = dict(bundle.get("draft") or {}, kind=kind, domain=domain, text=text)
    if rest and str(draft.get("commit") or "") in on_server:
        draft["commit"] = str(rest[-1].get("sha") or "")
    write_json(folder / "bundle.json", dict(bundle, draft=draft, land=dict(land_meta, commits=rest)))
    if 0 < len(rest) < len(listed):
        raise AlreadySettled(f"{said} Those were taken off this bundle. Check the text, then approve it again.", False)
    raise AlreadySettled(f"{said} The bundle is kept, with your text.", False)


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
<script>
// The long-text note follows the textarea as you type. Without JS it shows
// how the text was when the page loaded.
document.querySelectorAll("textarea[data-long-chars]").forEach(function (area) {
  var note = area.form && area.form.querySelector("[data-long-note]");
  if (!note) return;
  var limit = parseInt(area.getAttribute("data-long-chars"), 10);
  var update = function () { note.hidden = Array.from(area.value).length <= limit; };
  area.addEventListener("input", update);
  update();
});
</script>
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
            # Rendered for every form, and hidden while the text is short. The
            # page script follows the textarea; without JS it shows the load state.
            hidden = "" if len(draft.get("text") or "") > LONG_TEXT_CHARS else " hidden"
            long_note = (
                f'<p class="meta" data-long-note aria-live="polite"{hidden}>This text is long. '
                "The embedding reads only about the first 512 tokens. "
                "Trim it, or keep what matters first.</p>"
            )
            iden = read_identity()
            # As approve does: the bundle's own id, else the origin of its repo.
            rr_default = (land_meta.get("remote_repo") or "").strip()
            if not rr_default and item.get("repo") and Path(str(item["repo"])).is_dir():
                rr_default = git_remote_repo_id(str(item["repo"]))
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
<label>{text_label} <textarea name="text" data-long-chars="{LONG_TEXT_CHARS}">{draft_text}</textarea></label>
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
        except AlreadySettled as error:
            self._send(409, f"<p>{html.escape(str(error))}</p><p><a href='/'>Back</a></p>")
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
    return store_root() / "review.json"


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
    stop_on_signals()
    try:
        write_json(state_path, {"pid": os.getpid(), "port": port})
        url = f"http://127.0.0.1:{port}"
        print(url)
        webbrowser.open(url)
        server.serve_forever()
    finally:
        # serve_forever only ends by an exception, so the process exits next.
        # The handlers stay, and a later signal is ignored while this runs.
        server.server_close()
        drop_review_state(state_path)
    return 0


def _stop_review(signum, frame):
    # A second signal (a closed terminal can send two) must not cut the cleanup short.
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None and signal.getsignal(number) is _stop_review:
            signal.signal(number, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def stop_on_signals():
    """`kill <pid>` and a closed terminal stop the page through its finally, as Ctrl-C does.

    A SIGHUP that is ignored (nohup) stays ignored.
    """
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        if name == "SIGHUP" and signal.getsignal(number) == signal.SIG_IGN:
            continue
        signal.signal(number, _stop_review)


def drop_review_state(state_path):
    """Remove review.json if it still names this process, not a page started since.

    A file that is no longer JSON (a kill while it was written) goes too, or the
    next review would refuse to start on it.
    """
    try:
        state = read_json(state_path)
    except OSError:
        return
    except SystemExit:
        state = None
    if state is None or (isinstance(state, dict) and state.get("pid") == os.getpid()):
        state_path.unlink(missing_ok=True)


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


def hook_bin():
    """The command the hooks run: the agent-memory that is running install.

    A tool install (uv tool install) runs as a console script named
    agent-memory, so the hooks get that path as it was called, without
    resolving links: ~/.local/bin/agent-memory stays put across upgrades. Run
    from a clone (bin/agent-memory, or python3 scripts/agent_memory.py), they
    get the clone's bin/agent-memory, as before.
    """
    called = Path(sys.argv[0])
    if called.name == "agent-memory" and called.is_file():
        return Path(os.path.abspath(called))
    if BIN.is_file():
        return BIN
    found = shutil.which("agent-memory")
    if found:
        return Path(os.path.abspath(found))
    raise SystemExit(
        "could not find the agent-memory command for the hooks to run. Install it with "
        "`uv tool install git+https://github.com/roune/agent-memory`, then run `agent-memory install` again."
    )


# A Claude Code or Codex hook entry is ours when its command runs
# `agent-memory capture`, quoted or not. Files we write carry MARKER.
_OURS_RE = re.compile(r"agent-memory['\"]? capture\b")


def ours(command):
    return isinstance(command, str) and (MARKER in command or bool(_OURS_RE.search(command)))


def hook_command(tool, event, command):
    return f"{shlex.quote(str(command))} capture --tool {tool} --event {event}"


def cursor_script(command):
    return f"""#!/bin/sh
# {MARKER}
exec {shlex.quote(str(command))} capture --tool cursor
"""


def cursor_script_path(home):
    return home / ".cursor" / "hooks" / "agent-memory.sh"


def pi_extension_path(home):
    return home / ".pi" / "agent" / "extensions" / "agent-memory" / "index.ts"


def refuse_unmarked(path):
    """A file at our path that we didn't write is someone else's: leave it."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SystemExit(f"could not read {path}: {error}. Nothing was written.") from None
    if MARKER not in text:
        raise SystemExit(
            f"{path} was not written by agent-memory (it has no {MARKER} line). "
            "Move it aside, then run install again. Nothing was written."
        )


def check_writable(tool, home):
    """Refuse before anything is written, so install changes all of the tools or none."""
    if tool == "cursor":
        refuse_unmarked(cursor_script_path(home))
    elif tool == "pi":
        refuse_unmarked(pi_extension_path(home))
    elif tool == "codex" and codex_hooks_in_toml(home):
        raise SystemExit(
            "Codex already has [hooks] in ~/.codex/config.toml. Remove that table, then run install again. "
            "This tool writes ~/.codex/hooks.json only. Nothing was written."
        )


def write_cursor(home, command):
    script = cursor_script_path(home)
    refuse_unmarked(script)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(cursor_script(command), encoding="utf-8")
    script.chmod(0o755)
    path = home / ".cursor" / "hooks.json"
    data = read_json(path) if path.is_file() else {"version": 1, "hooks": {}}
    hooks = data.setdefault("hooks", {})
    for event in ("stop", "sessionEnd"):
        entries = list(hooks.get(event, []))
        if not any("agent-memory.sh" in str(item.get("command", "")) for item in entries):
            entries.append({"command": "./hooks/agent-memory.sh"})
        hooks[event] = entries
    write_json(path, data)


def command_entries(existing, command):
    """Point our entry at command where it is, keeping its other keys. Other entries stay as they are.

    A second entry of ours is dropped. With none, a new group goes at the end.
    """
    placed = False
    kept = []
    for group in existing:
        inner = []
        for item in group.get("hooks", []):
            if ours(item.get("command", "")):
                if placed:
                    continue
                item = dict(item)
                item["command"] = command
                placed = True
            inner.append(item)
        if inner:
            group = dict(group)
            group["hooks"] = inner
            kept.append(group)
    if not placed:
        kept.append({"hooks": [{"type": "command", "command": command}]})
    return kept


def write_claude(home, command):
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_json(path) if path.is_file() else {}
    hooks = data.setdefault("hooks", {})
    hooks["Stop"] = command_entries(hooks.get("Stop", []), hook_command("claude-code", "stop", command))
    hooks["SessionEnd"] = command_entries(
        hooks.get("SessionEnd", []),
        hook_command("claude-code", "session-end", command),
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


def write_codex(home, command):
    if codex_hooks_in_toml(home):
        raise SystemExit("Codex already has [hooks] in ~/.codex/config.toml. Remove that table, then run install again. This tool writes ~/.codex/hooks.json only.")
    directory = home / ".codex"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "hooks.json"
    data = read_json(path) if path.is_file() else {"hooks": {}}
    hooks = data.setdefault("hooks", {})
    hooks["Stop"] = command_entries(hooks.get("Stop", []), hook_command("codex", "stop", command))
    hooks["SessionEnd"] = command_entries(
        hooks.get("SessionEnd", []),
        hook_command("codex", "session-end", command),
    )
    write_json(path, data)


def write_pi(home, command):
    path = pi_extension_path(home)
    refuse_unmarked(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
  const child = spawn({json.dumps(str(command))}, ["capture", "--tool", "pi", "--event", eventName], {{
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


def hooked_commands(home):
    """The executables the hooks we marked run today, from each tool's files."""
    found = []
    for path in (home / ".claude" / "settings.json", home / ".codex" / "hooks.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        events = data.get("hooks") if isinstance(data, dict) else None
        for groups in (events.values() if isinstance(events, dict) else []):
            for group in groups if isinstance(groups, list) else []:
                for item in (group.get("hooks") or []) if isinstance(group, dict) else []:
                    command = item.get("command") if isinstance(item, dict) else None
                    if ours(command):
                        found.append(_first_word(command))
    try:
        for line in cursor_script_path(home).read_text(encoding="utf-8").splitlines():
            if line.startswith("exec "):
                found.append(_first_word(line[len("exec "):]))
    except (OSError, UnicodeDecodeError):
        pass
    try:
        match = re.search(r'spawn\(("(?:[^"\\]|\\.)*")', pi_extension_path(home).read_text(encoding="utf-8"))
        if match:
            found.append(json.loads(match.group(1)))
    except (OSError, UnicodeDecodeError, ValueError):
        pass
    return [Path(item) for item in found if item]


def _first_word(command):
    try:
        words = shlex.split(command)
    except ValueError:
        return ""
    return words[0] if words else ""


def hooked_clones(home):
    """Clones whose bin/agent-memory the hooks still run: where the old state may be."""
    clones = []
    for command in hooked_commands(home):
        if command.name == "agent-memory" and command.parent.name == "bin":
            clone = command.parent.parent
            if local_state.is_clone(clone) and clone not in clones:
                clones.append(clone)
    return clones


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

    command = hook_bin()
    if tools:
        for tool in tools:
            check_writable(tool, HOME)
        print("This will write capture hooks for:")
        for tool in tools:
            print(f"  {tool}")
        print(f"They run {command}.")
        print("Hooks this tool wrote are updated where they are. Other hooks in those files are left in place.")
        if not args.yes:
            if not sys.stdin.isatty():
                raise SystemExit("re-run in a terminal, or pass --yes")
            answer = input("Write these hooks? [y/N] ").strip().lower()
            write = answer in {"y", "yes"}
            if not write:
                print("editor hooks left unchanged")
        else:
            write = True
        if write:
            # The hooks may still run an old clone's bin/agent-memory. Its
            # inbox and outbox move here before the hooks stop pointing at it.
            if not os.environ.get("AGENT_MEMORY_DIR"):
                for clone in hooked_clones(HOME):
                    move_state(clone)
                    print(
                        f"Git hooks set up from {clone} still run {clone / 'bin' / 'agent-memory'}. "
                        "In each repo that has them, run `agent-memory config <repo> --git-hooks true`."
                    )
            for tool in tools:
                WRITERS[tool](HOME, command)
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
            hooks_dir = write_git_hooks(str(repo), command)
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
            if not ours(item.get("command", ""))
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
    script = cursor_script_path(HOME)
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

    extension = pi_extension_path(HOME)
    if extension.is_file() and MARKER in extension.read_text(encoding="utf-8"):
        shutil.rmtree(extension.parent)
    print("removed agent-memory hooks")

    try:
        removed = editors.remove_all(HOME)
    except (editors.EditorError, server_client.ConfigError) as error:
        raise SystemExit(f"MCP entries left in place: {error}") from None
    for tool, path, names, ids in removed:
        print(f"removed MCP {', '.join(names)} from {tool} ({path})")
        for server, token_id in ids:
            print(f"  its read token stays valid until revoked, on {server}: agent-memory-server token revoke {token_id}")
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


def editor_servers(config):
    """[(name, url, token, auth on)] for every distinct server, checked with /v1/whoami first."""
    urls = editors.server_urls(config)
    names = editors.entry_names(urls)
    found = []
    for url in urls:
        token = server_client.token_for(url, config)
        try:
            who = server_client.Client(url, token).whoami()
        except (server_client.ServerError, server_client.ServerUnreachable) as error:
            raise SystemExit(f"{url}: {server_client.explain(error)}") from None
        auth_on = who.get("auth") == "on"
        if auth_on and not {"read", "write"} <= set(who.get("scopes") or []):
            raise SystemExit(
                f"{url}: an editor token is minted from a token with the read and write scopes, and this one has "
                f"{', '.join(who.get('scopes') or []) or 'none'}. Connect with your laptop's token, then run this again."
            )
        found.append((names[url], url, token if auth_on else None, auth_on))
    return found


def connect_editors(args):
    """Register the server's /mcp in each editor, with a read token minted for it. Writes only after a yes."""
    for flag, value in (("--url", args.url), ("--token-stdin", args.token_stdin), ("--mode", args.mode), ("--repo", args.repo)):
        if value:
            raise SystemExit(f"--editor uses the servers connect already wrote; run connect {flag} on its own first")
    try:
        tools, notes = editors.parse_editors(args.editor)
        config = server_client.read_config()
        record = editors.read_record()
    except server_client.ConfigError as error:
        raise SystemExit(str(error)) from None
    for line in notes:
        print(line)
    if not tools:
        return 0
    if config is None:
        raise SystemExit("no server is configured: run agent-memory connect --url <server> first")
    servers = editor_servers(config)
    names = [name for name, *_ in servers]

    # What each file holds now, so the preview says what changes and nothing is read twice.
    plan = []
    try:
        for tool in tools:
            present = editors.current(tool, HOME)
            owned = editors.ours(tool, HOME, record)
            stale = [name for name in owned if name not in names]
            editors.check(tool, HOME, names, stale)
            plan.append((tool, present, owned, stale))
    except editors.EditorError as error:
        raise SystemExit(str(error)) from None

    if args.print:
        auth_on = {name for name, *_rest, on in servers if on}
        written = [tool for tool, _present, owned, _stale in plan if auth_on & set(owned)]
        if written:
            raise SystemExit(
                f"connect --editor already wrote {', '.join(written)}, and --print would mint a new token that "
                "revokes the one in that file. Run it without --print to replace the entry, or run "
                "agent-memory uninstall first."
            )
        return print_editor_lines(tools, servers, config)

    print("This will write MCP entries for agent-memory:")
    for tool, present, owned, stale in plan:
        print(f"  {tool}: {editors.display(tool, HOME)}")
        for name, url, _token, auth_on in servers:
            how = f"a new read token, label {editors.label(tool)}" if auth_on else "auth is off, so no header"
            note = " (replaces an entry you added)" if name in present and name not in owned else ""
            print(f"    {name} -> {editors.mcp_url(url)} ({how}){note}")
        for name in stale:
            print(f"    {name}: removed, its server is no longer configured")
    print("Other MCP servers in those files are left in place.")
    if any(auth_on for *_, auth_on in servers):
        print("Running this again replaces each editor's token; the old one stops working.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("re-run in a terminal, or pass --yes (or --print to only print the lines)")
        answer = input("Write these entries? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("editor configs left unchanged; no token was minted")
            return 0

    failures = []
    for tool, _present, _owned, stale in plan:
        # A server whose mint failed keeps its old entry: the old token wasn't replaced.
        add, ids, failed = mint_for(tool, servers, config)
        failures += failed
        if not add and not stale:
            print(f"{tool}: nothing written")
            continue
        try:
            path = editors.apply(tool, HOME, add, stale)
        except editors.EditorError as error:
            raise SystemExit(str(error)) from None
        entries = record.setdefault(tool, {})
        for name in stale:
            entries.pop(name, None)
        for name, (url, _token) in add.items():
            entries[name] = {"url": editors.mcp_url(url), "token_id": ids.get(name)}
        editors.write_record(record)
        print(f"wrote {tool}: {path} ({', '.join(add) or 'removed ' + ', '.join(stale)})")
    print("Restart the editors to pick up the change. Pi keeps capture through its extension and has no MCP entry.")
    if failures:
        for line in failures:
            print(line)
        print("Those entries were left as they were. Fix the cause, then run this again.")
        return 1
    return 0


def mint_for(tool, servers, config):
    """{name: (url, read token or None)}, {name: token id}, and a line for each server that failed.

    Every server is tried, so one that fails doesn't keep the others from
    being written. Never the caller's own token.
    """
    add, ids, failed = {}, {}, []
    own = {config.get("token")} | set(server_client.read_tokens().values())
    for name, url, token, auth_on in servers:
        if not auth_on:
            add[name] = (url, None)
            continue
        try:
            minted = server_client.Client(url, token).mint_token(editors.label(tool))
        except (server_client.ServerError, server_client.ServerUnreachable, server_client.ConfigError) as error:
            message = server_client.explain(error) if not isinstance(error, server_client.ConfigError) else str(error)
            failed.append(f"{url}: minting the {tool} token failed: {message}")
            continue
        if minted["token"] in own:
            failed.append(f"{url}: the server answered with the CLI's own token, which never goes in an editor file")
            continue
        add[name] = (url, minted["token"])
        ids[name] = minted.get("id")
    return add, ids, failed


def print_editor_lines(tools, servers, config):
    """--print: mint the tokens and print the commands and file lines. No file is written."""
    if any(auth_on for *_, auth_on in servers):
        print("Each line holds a new read token, and the earlier token with that label stops working.")
    failures = []
    for tool in tools:
        add, _ids, failed = mint_for(tool, servers, config)
        failures += failed
        print(f"{tool}:")
        for name, (url, token) in add.items():
            for line in editors.shown(tool, name, url, token).splitlines():
                print(f"  {line}")
    for line in failures:
        print(line)
    print("Nothing was written.")
    return 1 if failures else 0


def connect_cmd(args):
    """Check the server with /v1/whoami, then write the config. Nothing is written when the check fails."""
    if getattr(args, "editor", ""):
        return connect_editors(args)
    if getattr(args, "print", False) or getattr(args, "yes", False):
        raise SystemExit("--print and --yes go with --editor")
    if not args.url:
        raise SystemExit("connect needs --url <server>, or --editor <tools>")
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

    uninstall_parser = sub.add_parser("uninstall", help="remove only the hooks and MCP entries this tool added")
    uninstall_parser.set_defaults(func=uninstall)

    note_parser = sub.add_parser("note", help="pin a draft memory so review can approve it")
    note_parser.add_argument("--session-id", required=True)
    note_parser.add_argument("--kind", required=True, choices=KINDS)
    note_parser.add_argument("--domain", required=True, choices=DOMAINS)
    note_parser.add_argument("--text", help="memory text; omit to read stdin")
    note_parser.set_defaults(func=note)

    keep_parser = sub.add_parser("keep", help="pin a session so review shows it")
    keep_parser.add_argument("session_id")
    keep_parser.add_argument("--tool", choices=["cursor", "claude-code", "codex", "pi", "git", NOTE_TOOL])
    keep_parser.set_defaults(func=keep)

    drop_parser = sub.add_parser("drop", help="delete an inbox bundle")
    drop_parser.add_argument("session_id")
    drop_parser.add_argument("--tool", choices=["cursor", "claude-code", "codex", "pi", "git", NOTE_TOOL])
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
    connect_parser.add_argument("--url", default="", help="server URL, for example http://127.0.0.1:8765")
    connect_parser.add_argument(
        "--token-stdin", action="store_true", help="read the bearer token from the first line of stdin"
    )
    connect_parser.add_argument("--mode", choices=server_client.MODES, default=None, help="default: keep the current mode")
    connect_parser.add_argument("--repo", default="", help="per-repo mode: send this remote id to --url")
    connect_parser.add_argument(
        "--editor",
        default="",
        metavar="TOOLS",
        help="register the server's MCP endpoint in these editors: claude-code,cursor,codex (Pi has none)",
    )
    shown = connect_parser.add_mutually_exclusive_group()
    shown.add_argument("--print", action="store_true", help="with --editor: print the lines and write nothing")
    shown.add_argument("--yes", action="store_true", help="with --editor: write without prompting")
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


def move_state(old_root):
    """The one-time move of old_root's inbox/, outbox/ and the rest (local_state). It never stops a command."""
    new_root = store_root()
    try:
        moved, left = local_state.move(old_root, new_root)
    except OSError as error:
        print(f"agent-memory: could not move the state in {old_root} to {new_root}: {error}", file=sys.stderr)
        return
    lines = [f"moved {source} to {target}" for source, target in moved]
    lines += [f"left {source} where it is: {reason}" for source, reason in left]
    for line in lines:
        print(f"agent-memory: {line}", file=sys.stderr)
        try:
            log(line)
        except OSError:
            pass


def move_old_state():
    """State that sits where it was before the move: in this clone, or in AGENT_MEMORY_DIR's data/."""
    override = os.environ.get("AGENT_MEMORY_DIR")
    if override:
        move_state(Path(override))
        return
    try:
        local_state.ensure_root()
    except OSError as error:
        print(f"agent-memory: could not create {store_root()}: {error}", file=sys.stderr)
        return
    if local_state.is_clone(ROOT):
        move_state(ROOT)
    # Clones moved from before: an older CLI there (a git hook in a repo not
    # set up again) may have filed new bundles since.
    for old in local_state.recorded(store_root()):
        if old != ROOT and local_state.is_clone(old):
            move_state(old)


def main():
    parser = build_parser()
    args = parser.parse_args()
    move_old_state()
    result = args.func(args)
    raise SystemExit(0 if result is None else result)


if __name__ == "__main__":
    main()
