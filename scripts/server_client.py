"""The CLI's side of the agent-memory server: its config files and its HTTP calls.

Standard library only (urllib), so the CLI keeps no third-party dependencies.

Config lives in ~/.config/agent-memory/:

- server   url = http://127.0.0.1:8765, token = am_... (optional), mode = shared or per-repo
- servers  per-repo mode only: <remote id> = <server url>
- tokens   per-repo mode only: <server url> = am_...

server and tokens hold secrets, so they are written with mode 0600. The legacy
database and databases files are read only to carry their mode over.

Every request sends Content-Type: application/json (the server's browser rules
refuse a POST without it) and never an Origin header.
"""

import contextlib
import http.client
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

TIMEOUT = 10.0
HOOK_TIMEOUT = 2.0
USER_AGENT = "agent-memory-cli"
MODES = ("shared", "per-repo")


class ConfigError(Exception):
    """A config file that can't be used. Its text says which file and why."""


class ServerError(Exception):
    """The server answered with an error status."""

    def __init__(self, status, message, body=None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.body = body or {}


class ServerUnreachable(Exception):
    """No answer: refused, timed out, or not HTTP."""


def config_dir():
    return Path.home() / ".config" / "agent-memory"


def parse_lines(text, lower_keys=False):
    """key = value lines. # starts a comment, quotes around a value are dropped, the last key wins."""
    found = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if lower_keys:
            key = key.lower().replace("-", "_")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            found[key] = value
    return found


def _read(name, lower_keys=False):
    path = config_dir() / name
    if not path.is_file():
        return {}
    return parse_lines(path.read_text(encoding="utf-8"), lower_keys)


def clean_url(value, where="url"):
    """http(s)://host[:port][/path] without a trailing slash. Refuses anything else."""
    url = (value or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{where} must be an http:// or https:// URL, not {value!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError(f"{where} must be a plain URL without user, query or fragment: {value!r}")
    return url


def _mode(value, where):
    mode = (value or "shared").strip().lower().replace("_", "-")
    if mode not in MODES:
        raise ConfigError(f"{where} has mode {mode!r}; use shared or per-repo")
    return mode


def legacy_mode():
    """The mode in the legacy database file, or shared."""
    return _mode(_read("database", lower_keys=True).get("mode"), config_dir() / "database")


def read_config():
    """The server file as {url, token, mode}, or None when there is no server file or it has no url."""
    path = config_dir() / "server"
    found = _read("server", lower_keys=True)
    if not found.get("url", "").strip():
        return None
    return {
        "url": clean_url(found["url"], f"{path}: url"),
        "token": found.get("token", "").strip() or None,
        "mode": _mode(found.get("mode"), path),
    }


def read_servers():
    """remote id -> server URL, from the servers file."""
    path = config_dir() / "servers"
    return {remote: clean_url(url, f"{path}: {remote}") for remote, url in _read("servers").items()}


def read_tokens():
    """server URL -> token, from the tokens file."""
    return {url.strip().rstrip("/"): token.strip() for url, token in _read("tokens").items() if token.strip()}


def target(remote_id=None, config=None):
    """(url, token) for a repo, or None when no server is configured.

    In shared mode every repo goes to the server file's url. In per-repo mode a
    remote listed in servers goes to that url, any other to the server file's;
    its token is the one tokens names for that url, else the server file's when
    the url is the same.
    """
    config = config if config is not None else read_config()
    if config is None:
        return None
    if config["mode"] == "shared" or not remote_id:
        return config["url"], config["token"]
    url = read_servers().get(remote_id, config["url"])
    token = read_tokens().get(url)
    if token is None and url == config["url"]:
        token = config["token"]
    return url, token


def token_for(url, config=None):
    """The token for a server url: the server file's for its own url, else the tokens file's."""
    config = config if config is not None else read_config()
    url = clean_url(url)
    if config is not None and url == config["url"]:
        return config["token"]
    return read_tokens().get(url)


def _write_private(path, text):
    """Write text with mode 0600, also when the file already exists with looser bits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def write_config(url, token=None, mode="shared"):
    """Write the server file. A missing token means none (auth off)."""
    lines = ["# Written by agent-memory connect.", f"url = {clean_url(url)}", f"mode = {_mode(mode, 'mode')}"]
    if token:
        lines.append(f"token = {token}")
    path = config_dir() / "server"
    _write_private(path, "\n".join(lines) + "\n")
    return path


def _set_line(name, key, value, private):
    """Set key = value in a key = value file, keeping every other line. value None removes it."""
    path = config_dir() / name
    kept = []
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if "=" in line and line.partition("=")[0].strip() == key:
                continue
            kept.append(raw)
    if value is not None:
        kept.append(f"{key} = {value}")
    text = "\n".join(kept) + "\n" if kept else ""
    if private:
        _write_private(path, text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path


def map_repo(remote_id, url, token=None):
    """Per-repo mode: send remote_id to url, with that url's token (none removes it)."""
    url = clean_url(url)
    servers = _set_line("servers", remote_id, url, private=False)
    tokens = _set_line("tokens", url, token or None, private=True)
    return servers, tokens


def check_token(token):
    text = (token or "").strip()
    if not text:
        return None
    if any(character.isspace() for character in text) or not text.isprintable():
        raise ConfigError("a token is one word with no spaces")
    return text


class _NoRedirects(HTTPRedirectHandler):
    """Never follow a redirect. urllib would send the Authorization header to
    whatever host Location names, https to http included, and turn a POST into
    a GET. The API never redirects, so a 3xx is answered as an error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(_NoRedirects)


class Client:
    """One server. Every call returns the decoded JSON or raises ServerError / ServerUnreachable."""

    def __init__(self, url, token=None, timeout=TIMEOUT, opener=_OPENER.open):
        self.url = clean_url(url)
        self.token = token
        self.timeout = timeout
        self._open = opener

    def request(self, method, path, body=None, params=None):
        target_url = self.url + path
        if params:
            target_url += "?" + urlencode({key: value for key, value in params.items() if value is not None})
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(target_url, data=data, method=method, headers=headers)
        try:
            with self._open(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as error:
            with contextlib.closing(error):
                raw = error.read() or b""
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {}
            message = decoded.get("error") if isinstance(decoded, dict) else None
            if 300 <= error.code < 400:
                message = (
                    f"the server redirected to {error.headers.get('Location') or 'another URL'}, and agent-memory "
                    "never follows a redirect; connect to the URL it should use instead"
                )
            raise ServerError(error.code, message or error.reason or "error", decoded if isinstance(decoded, dict) else {}) from None
        except (URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            reason = getattr(error, "reason", error)
            raise ServerUnreachable(f"the server at {self.url} did not answer: {reason}") from None
        try:
            return json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServerUnreachable(f"{self.url}{path} did not answer with JSON; is that the agent-memory server?") from None

    def health(self):
        return self.request("GET", "/v1/health")

    def whoami(self):
        return self.request("GET", "/v1/whoami")

    def list_memories(self, **filters):
        return self.request("GET", "/v1/memories", params=filters)["memories"]

    def get_memory(self, document_id):
        """One memory, or None for a 404."""
        try:
            return self.request("GET", "/v1/memories/" + quote(document_id, safe=""))
        except ServerError as error:
            if error.status == 404:
                return None
            raise

    def commits_status(self, repo, shas):
        answer = self.request("POST", "/v1/commits/status", {"repo": repo, "shas": list(shas)})
        commits = answer.get("commits") if isinstance(answer, dict) else None
        if not isinstance(commits, dict):
            raise ServerUnreachable(f"{self.url}/v1/commits/status answered without commits; is that the agent-memory server?")
        return commits

    def import_rows(self, memories, rejections):
        return self.request("POST", "/v1/admin/import", {"memories": list(memories), "rejections": list(rejections)})


def explain(error):
    """One line for a person, with what to do next."""
    if isinstance(error, ServerError):
        if error.status == 401:
            return f"{error.message}. Run agent-memory connect again with --token-stdin."
        if error.status == 403:
            return f"{error.message}. Ask for a token with that scope, then run agent-memory connect again."
        return f"the server refused: {error.message} ({error.status})"
    return str(error)
