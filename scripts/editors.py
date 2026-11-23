"""MCP entries for editors: Claude Code, Cursor and Codex. Standard library only.

`agent-memory connect --editor` registers the server's /mcp endpoint in each
editor's user config, with a read-scope token minted for that editor. The files,
checked against each tool's docs in November 2026:

- Claude Code: ~/.claude.json, mcpServers.<name> = {type: "http", url, headers}.
  That is what `claude mcp add --transport http --scope user <name> <url>
  --header ...` writes. --header takes several values, so it goes after the
  name and the url.
- Cursor: ~/.cursor/mcp.json, mcpServers.<name> = {url, headers}.
- Codex: ~/.codex/config.toml, [mcp_servers.<name>] with url and http_headers.
  `codex mcp add --url` has no header flag, so the file is the way.

Pi gets no MCP registration: it keeps capture through its extension.

What was added is recorded in ~/.config/agent-memory/editors (tool, name, url,
token id), so `uninstall` removes only those entries, and only while they still
point where they were written.
"""

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import server_client

EDITORS = ("claude-code", "cursor", "codex")
NO_MCP = {"pi": "Pi gets no MCP registration; it keeps capture through its extension"}
DEFAULT_NAME = "agent-memory"
LABEL_PREFIX = "editor-"


class EditorError(Exception):
    """An editor file this can't change safely. Its text says which file and why."""


def parse_editors(value):
    """'claude-code,cursor' -> (tools in EDITORS order, notes for tools without MCP)."""
    asked = [part.strip().lower() for part in (value or "").split(",") if part.strip()]
    if not asked:
        raise server_client.ConfigError("--editor needs one or more of: " + ", ".join(EDITORS))
    unknown = [name for name in asked if name not in EDITORS and name not in NO_MCP]
    if unknown:
        raise server_client.ConfigError(
            f"unknown editor {', '.join(unknown)}; use one or more of: {', '.join(EDITORS)}"
        )
    tools = [name for name in EDITORS if name in asked]
    notes = [f"{name}: {NO_MCP[name]}" for name in NO_MCP if name in asked]
    return tools, notes


def label(tool):
    return LABEL_PREFIX + tool


def mcp_url(url):
    return server_client.clean_url(url) + "/mcp"


def _key(url):
    parts = urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower(), parts.path)


def server_urls(config):
    """Every distinct server URL: the server file's first, then the servers file's (per-repo mode)."""
    urls = [config["url"]]
    if config["mode"] == "per-repo":
        seen = {_key(config["url"])}
        for url in sorted(set(server_client.read_servers().values())):
            if _key(url) not in seen:
                seen.add(_key(url))
                urls.append(url)
    return urls


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def entry_names(urls):
    """url -> entry name. The first is agent-memory, each other agent-memory-<host>."""
    names = {}
    taken = set()
    for index, url in enumerate(urls):
        if index == 0:
            name = DEFAULT_NAME
        else:
            parts = urlsplit(url)
            name = f"{DEFAULT_NAME}-{_slug(parts.hostname or '')}"
            if name in taken and parts.port:
                name = f"{name}-{parts.port}"
            base, count = name, 2
            while name in taken:
                name, count = f"{base}-{count}", count + 1
        taken.add(name)
        names[url] = name
    return names


# --- what goes in each file -------------------------------------------------


def headers(token):
    return {"Authorization": f"Bearer {token}"} if token else None


def entry(tool, url, token):
    """The JSON entry for Claude Code or Cursor, or the table for Codex. No header without a token."""
    if tool == "claude-code":
        value = {"type": "http", "url": mcp_url(url)}
        if token:
            value["headers"] = headers(token)
    elif tool == "cursor":
        value = {"url": mcp_url(url)}
        if token:
            value["headers"] = headers(token)
    else:
        value = {"url": mcp_url(url)}
        if token:
            value["http_headers"] = headers(token)
    return value


def _toml_key(name):
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def toml_block(name, url, token):
    lines = [f"[mcp_servers.{_toml_key(name)}]", f"url = {json.dumps(mcp_url(url))}"]
    if token:
        lines.append(f"http_headers = {{ \"Authorization\" = {json.dumps('Bearer ' + token)} }}")
    return "\n".join(lines) + "\n"


def path_for(tool, home):
    home = Path(home)
    return {
        "claude-code": home / ".claude.json",
        "cursor": home / ".cursor" / "mcp.json",
        "codex": home / ".codex" / "config.toml",
    }[tool]


def display(tool, home):
    return "~/" + str(path_for(tool, home).relative_to(Path(home)))


def shown(tool, name, url, token):
    """What --print shows for one entry: the command to run, or what to put in the file."""
    if tool == "claude-code":
        command = f"claude mcp add --transport http --scope user {name} {mcp_url(url)}"
        if token:
            command += f' --header "Authorization: Bearer {token}"'
        return command
    if tool == "cursor":
        return "~/.cursor/mcp.json: " + json.dumps({"mcpServers": {name: entry(tool, url, token)}})
    return "~/.codex/config.toml:\n" + toml_block(name, url, token).rstrip("\n")


# --- reading and writing the files ------------------------------------------


def _write(path, text, private):
    """Replace path's contents in one step. A symlink is followed, so a dotfiles link stays a link.

    private (a token is in the text) clears the group and other bits.
    """
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    mode = (real.stat().st_mode & 0o777) if real.exists() else 0o644
    if private:
        mode &= 0o600
    descriptor, temp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.")
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, real)
    except BaseException:
        with _ignored():
            os.unlink(temp)
        raise


class _ignored:
    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return kind is not None and issubclass(kind, OSError)


def _read_json(path, where):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EditorError(f"{where} is not valid JSON ({error}); fix it, then run this again") from None
    if not isinstance(data, dict):
        raise EditorError(f"{where} is not a JSON object; fix it, then run this again")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise EditorError(f"{where} has mcpServers that is not an object; fix it, then run this again")
    return data


def _read_toml(path, where):
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise EditorError(f"{where} is not valid TOML ({error}); fix it, then run this again") from None
    return text, parsed


def current(tool, home):
    """name -> url of every MCP entry in the tool's file (an entry without a url maps to None)."""
    path = path_for(tool, home)
    where = display(tool, home)
    if tool == "codex":
        servers = _read_toml(path, where)[1].get("mcp_servers", {})
    else:
        servers = _read_json(path, where).get("mcpServers", {})
    if not isinstance(servers, dict):
        return {}
    return {name: (value.get("url") if isinstance(value, dict) else None) for name, value in servers.items()}


def _header_names(names):
    pattern = "|".join(
        alternative
        for name in names
        for alternative in (re.escape(name), re.escape(json.dumps(name)), re.escape("'" + name + "'"))
    )
    return re.compile(r"^\s*\[\s*mcp_servers\s*\.\s*(?:" + pattern + r")\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$")


def _drop_tables(text, names):
    """The text without the [mcp_servers.<name>] tables (and their sub-tables) for these names.

    Comments and blank lines after a dropped table's last key are the user's:
    they are kept, whether another table or the end of the file follows.
    """
    if not names:
        return text
    starts = _header_names(names)
    kept = []
    pending = []
    dropping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            kept.extend(pending)
            pending = []
            dropping = bool(starts.match(line))
            if not dropping:
                kept.append(line)
        elif not dropping:
            kept.append(line)
        elif not stripped or stripped.startswith("#"):
            pending.append(line)
        else:
            # A key of the dropped table: comments above it were inside the table.
            pending = []
    kept.extend(pending)
    return "".join(kept)


def _edit_codex(path, where, add, remove):
    text, before = _read_toml(path, where)
    touched = set(add) | set(remove)
    edited = _drop_tables(text, touched).rstrip("\n")
    blocks = [toml_block(name, url, token) for name, (url, token) in add.items()]
    if blocks:
        edited = (edited + "\n\n" if edited else "") + "\n".join(blocks)
    else:
        edited = edited + "\n" if edited else ""
    try:
        after = tomllib.loads(edited)
    except tomllib.TOMLDecodeError:
        after = None
    servers = (after or {}).get("mcp_servers", {})
    fine = (
        after is not None
        and _same_rest(before, after, touched)
        and all(servers.get(name) == entry("codex", url, token) for name, (url, token) in add.items())
        and not any(name in servers for name in remove)
    )
    if not fine:
        names = ", ".join(sorted(touched))
        raise EditorError(
            f"{where} defines mcp_servers {names} in a form this can't edit (not as its own "
            "[mcp_servers.<name>] table); remove those entries by hand, then run this again"
        )
    return edited


def _same_rest(before, after, touched):
    """Everything but the touched mcp_servers entries is unchanged."""
    def rest(parsed):
        copy = {key: value for key, value in parsed.items() if key != "mcp_servers"}
        servers = {name: value for name, value in parsed.get("mcp_servers", {}).items() if name not in touched}
        return copy, servers

    return rest(before) == rest(after)


def check(tool, home, names, remove=()):
    """Refuse now, before any token is minted, a file that apply() would refuse later."""
    path = path_for(tool, home)
    where = display(tool, home)
    if tool == "codex":
        _edit_codex(path, where, {name: ("http://check.invalid", "am_check") for name in names}, remove)
    else:
        _read_json(path, where)


def apply(tool, home, add, remove=()):
    """Write add {name: (url, token)} and drop the names in remove, keeping every other entry. Returns the path."""
    path = path_for(tool, home)
    where = display(tool, home)
    private = any(token for _, token in add.values())
    if tool == "codex":
        text = _edit_codex(path, where, add, remove)
        if path.exists() or text:
            _write(path, text, private)
        return path
    data = _read_json(path, where)
    servers = dict(data.get("mcpServers", {}))
    for name in remove:
        servers.pop(name, None)
    for name, (url, token) in add.items():
        servers[name] = entry(tool, url, token)
    data["mcpServers"] = servers
    _write(path, json.dumps(data, indent=2) + "\n", private)
    return path


# --- the record of what was added -------------------------------------------


def record_path():
    return server_client.config_dir() / "editors"


def read_record():
    """{tool: {name: {url, token_id}}} of the entries connect --editor wrote."""
    path = record_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise server_client.ConfigError(f"{path} is not valid JSON; delete it to forget which editor entries were added") from None
    if not isinstance(data, dict):
        return {}
    return {
        tool: {name: item for name, item in entries.items() if isinstance(item, dict)}
        for tool, entries in data.items()
        if tool in EDITORS and isinstance(entries, dict)
    }


def write_record(record):
    record = {tool: entries for tool, entries in record.items() if entries}
    path = record_path()
    if not record:
        if path.exists():
            path.unlink()
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, json.dumps(record, indent=2, sort_keys=True) + "\n", private=True)
    return path


def ours(tool, home, record):
    """The recorded names for tool whose entry still points at the recorded url."""
    found = current(tool, home)
    return [
        name
        for name, item in record.get(tool, {}).items()
        if name in found and found[name] == item.get("url")
    ]


def server_of(item):
    """The server URL a recorded entry was minted on: its /mcp url without /mcp."""
    url = item.get("url") or ""
    return url[: -len("/mcp")] if url.endswith("/mcp") else url


def remove_all(home):
    """Drop every entry connect --editor added and still owns.

    Returns [(tool, path, names, [(server url, token id)])]: `token revoke`
    takes one id, run on the server that minted it.
    """
    record = read_record()
    done = []
    for tool in EDITORS:
        if tool not in record:
            continue
        names = ours(tool, home, record)
        if names:
            path = apply(tool, home, {}, names)
            ids = [
                (server_of(record[tool][name]), record[tool][name]["token_id"])
                for name in names
                if record[tool][name].get("token_id")
            ]
            done.append((tool, path, names, ids))
        record.pop(tool)
    write_record(record)
    return done
