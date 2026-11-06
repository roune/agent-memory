#!/usr/bin/env python3
"""agent-memory command: capture, review, and hook install.

The hook calls `capture` and returns. Review is a page on 127.0.0.1.
`install` writes editor hooks only after a yes.
"""

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
BIN = ROOT / "bin" / "agent-memory"
PORT_START = 4173
KINDS = ("change", "learning", "decision")
DOMAINS = ("code", "architecture", "operations")
END_EVENTS = {"session-end", "sessionend", "session_shutdown"}
MARKER = "agent-memory-hook"


def store_root():
    override = os.environ.get("AGENT_MEMORY_DIR")
    return Path(override) if override else ROOT


def inbox_root():
    return store_root() / "inbox"


def memories_root():
    return store_root() / "memories"


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
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


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


def capture_disabled(repo):
    policy = Path(repo) / ".agent-memory.toml"
    if policy.is_file():
        for line in policy.read_text(encoding="utf-8").splitlines():
            if line.strip().replace(" ", "") == "capture=false":
                return True
    ignore = HOME / ".config" / "agent-memory" / "ignore"
    if ignore.is_file():
        repo_path = str(Path(repo).resolve())
        for line in ignore.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if item and (repo_path == item or repo_path.startswith(item.rstrip("/") + "/")):
                return True
    return False


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


def approve(bundle, kind, domain, text, commit=""):
    text = text.strip()
    if kind not in KINDS or domain not in DOMAINS or not text:
        raise ValueError("kind, domain, and text are required")
    repo = str(bundle.get("repo") or "").strip()
    tool = str(bundle.get("tool") or "").strip()
    session_id = str(bundle.get("session_id") or "").strip()
    if not repo or not tool or not session_id:
        raise ValueError("repo, tool, and session id are required")
    document = {
        "id": f"{kind}-{session_id}",
        "kind": kind,
        "domain": domain,
        "repo": repo,
        "tool": tool,
        "session_id": session_id,
        "text": text,
    }
    if kind == "change":
        commit = commit.strip()
        if len(commit) < 7 or any(char not in "0123456789abcdefABCDEF" for char in commit):
            raise ValueError("a change needs the commit it explains")
        document["commit"] = commit
    write_json(memories_root() / f"{document['id']}.json", document)
    shutil.rmtree(session_dir(bundle["tool"], bundle["session_id"]))
    return document


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
</style>
</head>
<body>
<h1>Review</h1>
<p class="meta">__LEAD__</p>
__ITEMS__
</body>
</html>
"""


def select_options(choices, selected):
    return "".join(
        f'<option{" selected" if choice == selected else ""}>{html.escape(choice)}</option>'
        for choice in choices
    )


def render_page(items, include_all):
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
            commits = candidate_commits(item)
            kinds = KINDS if commits else tuple(name for name in KINDS if name != "change")
            draft_kind = draft.get("kind")
            if draft_kind in kinds:
                kind = draft_kind
            elif commits:
                kind = "change"
            else:
                kind = "learning"
            if commits:
                radios = []
                for index, commit in enumerate(commits):
                    checked = " checked" if index == 0 else ""
                    short = html.escape(commit["sha"][:10])
                    subject = html.escape(commit["subject"])
                    sha = html.escape(commit["sha"])
                    radios.append(
                        f'<label><input type="radio" name="commit" value="{sha}"{checked}> {short} {subject}</label>'
                    )
                commit_block = "<fieldset><legend>Commit</legend>" + "".join(radios) + "</fieldset>"
            else:
                commit_block = "<p class=\"meta\">No commit yet. A change needs one. The working tree diff stays in git.</p>"
            message = item.get("last_assistant_message") or ""
            message_block = ""
            if message:
                message_block = f"<pre>{html.escape(message[:4000])}</pre>"
            text_label = "Context of this commit" if kind == "change" else "Keep this text"
            blocks.append(
                f"""<article>
<p><strong>{html.escape(item.get("repo") or "")}</strong></p>
<p class="meta">{tool} · {sid} · {html.escape(item.get("ended_at") or "")}</p>
<p class="meta">A change stores the context of the commit you pick. The diff stays in git.</p>
{commit_block}
{message_block}
<form method="post" action="/approve">
<input type="hidden" name="session_id" value="{sid}">
<input type="hidden" name="tool" value="{tool}">
<label>Kind <select name="kind">{select_options(kinds, kind)}</select></label>
<label>Domain <select name="domain">{select_options(DOMAINS, domain)}</select></label>
<label>{text_label} <textarea name="text">{draft_text}</textarea></label>
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
    return PAGE.replace("__LEAD__", html.escape(lead)).replace("__ITEMS__", body)


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

    def do_GET(self):
        host = self.headers.get("Host", "")
        if not host.startswith("127.0.0.1"):
            self._send(403, "localhost only")
            return
        query = parse_qs(urlparse(self.path).query)
        include_all = query.get("all", ["0"])[0] == "1"
        self._send(200, render_page(offered(include_all), include_all))

    def do_POST(self):
        host = self.headers.get("Host", "")
        if not host.startswith("127.0.0.1"):
            self._send(403, "localhost only")
            return
        form = self._form()
        session_id = form.get("session_id", "")
        try:
            bundle = find_bundle(session_id, form.get("tool") or None)
            path = urlparse(self.path).path
            if path == "/approve":
                approve(
                    bundle,
                    form.get("kind", ""),
                    form.get("domain", ""),
                    form.get("text", ""),
                    form.get("commit", ""),
                )
            elif path == "/reject":
                shutil.rmtree(session_dir(bundle["tool"], bundle["session_id"]))
            else:
                self._send(404, "not found")
                return
        except (SystemExit, ValueError, OSError) as error:
            self._send(400, f"<p>{html.escape(str(error))}</p><p><a href='/'>Back</a></p>")
            return
        self.send_response(303)
        self.send_header("Location", "/")
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


def install(args):
    tools = present_tools()
    if not tools:
        raise SystemExit("no Cursor, Claude Code, Codex, or Pi install was found")
    print("This will write capture hooks for:")
    for tool in tools:
        print(f"  {tool}")
    print("Other hooks already in those files are left in place.")
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit("re-run in a terminal, or pass --yes")
        answer = input("Write these hooks? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("left unchanged")
            return 0
    for tool in tools:
        WRITERS[tool](HOME)
        print(f"wrote {tool}")
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
    keep_parser.add_argument("--tool", choices=["cursor", "claude-code", "codex", "pi"])
    keep_parser.set_defaults(func=keep)

    drop_parser = sub.add_parser("drop", help="delete an inbox bundle")
    drop_parser.add_argument("session_id")
    drop_parser.add_argument("--tool", choices=["cursor", "claude-code", "codex", "pi"])
    drop_parser.set_defaults(func=drop)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    raise SystemExit(0 if result is None else result)


if __name__ == "__main__":
    main()
