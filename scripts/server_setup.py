"""`agent-memory setup`: the server in Docker for a tool install, with no clone. Standard library only.

It does what ./scripts/setup does in a clone. It runs the published image
instead of building one, from a folder that holds compose.yaml, .env and
data/ together: ${XDG_DATA_HOME:-~/.local/share}/agent-memory/server/, or the
folder --dir names, which ~/.config/agent-memory/setup remembers.

The order is strict. Every check runs first and writes nothing: Docker works,
no newer server's compose.yaml is there, and port 8765 is free or already ours.
Only then does it write compose.yaml (the copy in agent_memory_assets),
.setup.json, data/ and .env, pull, start, wait for /v1/health, and point the
CLI at the server. A refusal leaves every file as it was.

It never runs `docker compose down -v` and never deletes anything in data/.
Rerunning it is the upgrade step: it rewrites compose.yaml for this CLI's
version, keeps .env and data/, pulls and restarts.

Every Docker call goes through one runner (argv in, returncode, stdout and
stderr out), and the health check and the port probe are passed in too, so
the tests need no Docker and no server.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import agent_memory_assets
import editors
import local_state
import server_client

# A fixed project name keeps setup's containers apart from a clone's, which
# compose names after the clone's folder.
PROJECT = "agent-memory-server"
URL = "http://127.0.0.1:8765"
PORT = 8765
# The names that reach the same server; compose.yaml allows both hosts.
SAME_SERVER = {URL, "http://localhost:8765"}
HEALTH_TIMEOUT = 120
LOG_LINES = 40
SIDECAR = ".setup.json"
RECORD = "setup"
COMPOSE = "compose.yaml"
# Docker's words for a bind mount it can't make: Docker Desktop's file sharing,
# and snap Docker, which can't see hidden folders under $HOME.
MOUNT_ERRORS = (
    "mounts denied",
    "is not shared from the host",
    "error while creating mount source path",
    "bind source path does not exist",
    "invalid mount config",
)
DOCKER_HINT = "Docker Desktop, OrbStack or colima on macOS, or Docker Engine on Linux and in WSL"


class SetupFailed(Exception):
    """A check refused, or a step failed. The text is one line that says what to do next."""


@dataclass
class Ran:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_docker(argv):
    """The real runner: argv in, Ran out. Output is captured, never streamed."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, errors="replace")
    except OSError as error:
        return Ran(127, "", str(error))
    return Ran(done.returncode, done.stdout, done.stderr)


def health_ok(url=URL):
    """True when url's /v1/health answers with JSON."""
    try:
        server_client.Client(url, timeout=2.0).health()
    except (server_client.ServerError, server_client.ServerUnreachable):
        return False
    return True


def fetch_health(url=URL):
    """url's /v1/health answer as a dict, or None when it can't be read."""
    try:
        health = server_client.Client(url, timeout=2.0).health()
    except (server_client.ServerError, server_client.ServerUnreachable):
        return None
    return health if isinstance(health, dict) else None


def probe_port(url=URL):
    """Who answers on the port: "agent-memory" for a health answer, "other" for any listener, else None."""
    if health_ok(url):
        return "agent-memory"
    host, _, port = url.split("//", 1)[1].partition(":")
    try:
        with socket.create_connection((host, int(port or 80)), timeout=1.0):
            return "other"
    except OSError:
        return None


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


@dataclass
class Deps:
    """What setup calls outside itself. The tests replace each of these."""

    version: str
    run: Callable = run_docker
    which: Callable = shutil.which
    health: Callable = health_ok
    health_answer: Callable = fetch_health
    port_holder: Callable = probe_port
    sleep: Callable = time.sleep
    clock: Callable = time.monotonic
    now: Callable = datetime.now
    interactive: Callable = interactive
    ask: Callable = input
    present_tools: Callable = lambda: []
    # connect --editor for these tools; returns its exit code.
    connect_editors: Callable = None
    out: Callable = print

    def err(self, line):
        self.out(line, file=sys.stderr)


# The folder.


def default_dir():
    """${XDG_DATA_HOME:-~/.local/share}/agent-memory/server. AGENT_MEMORY_DIR is the CLI's state, and doesn't move it."""
    return local_state.default_root() / "server"


def record_path():
    return server_client.config_dir() / RECORD


def recorded_dir():
    """The folder an earlier --dir chose, or None."""
    try:
        lines = record_path().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    found = ""
    # Not server_client.parse_lines: a folder's name may hold a #, so only a whole line is a comment.
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and key.strip() == "dir":
            found = value.strip()
    return Path(found) if found and os.path.isabs(found) else None


def pick_dir(flag):
    """(folder, whether --dir chose it). --dir wins, then the remembered folder, then the default."""
    if flag:
        return Path(os.path.abspath(os.path.expanduser(flag))), True
    return recorded_dir() or default_dir(), False


def remember_dir(folder):
    if recorded_dir() == folder:
        return
    path = record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Written by agent-memory setup: the server's folder.\ndir = {folder}\n", encoding="utf-8")


# Step 3: what's in the folder now.


@dataclass
class Found:
    compose: bytes = None  # the current compose.yaml, or None
    sidecar: dict = None  # .setup.json, or None when missing or unreadable
    env: bytes = None  # the current .env, or None

    @property
    def ours(self):
        """setup wrote this compose.yaml: its hash is the one .setup.json recorded."""
        if self.compose is None or not self.sidecar:
            return False
        return self.sidecar.get("compose_sha256") == sha256(self.compose)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_folder(folder):
    if folder.exists() and not folder.is_dir():
        raise SetupFailed(f"{folder} is a file, not a folder. Pass --dir with a folder for the server.")
    found = Found()
    compose = folder / COMPOSE
    try:
        if compose.exists():
            found.compose = compose.read_bytes()
    except OSError as error:
        raise SetupFailed(f"Could not read {compose}: {error.strerror}. Fix its permissions, then run agent-memory setup again.") from None
    # .env too, so one that can't be read refuses here, before compose.yaml is touched.
    env = folder / ".env"
    try:
        if env.exists():
            found.env = env.read_bytes()
    except OSError as error:
        raise SetupFailed(f"Could not read {env}: {error.strerror}. Fix it, then run agent-memory setup again.") from None
    try:
        sidecar = json.loads((folder / SIDECAR).read_text(encoding="utf-8"))
        found.sidecar = sidecar if isinstance(sidecar, dict) else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        found.sidecar = None
    return found


# Step 4: no downgrade.


def version_tuple(value):
    """'0.2.0' -> (0, 2, 0); None for anything else, such as 'dev' or '0.2.0rc1'."""
    if not isinstance(value, str) or not re.fullmatch(r"\d+(\.\d+)*", value):
        return None
    return tuple(int(part) for part in value.split("."))


def check_downgrade(found, folder, version, force, deps):
    """Refuse to run an older image on data a newer one may have written. Reports a pin it can't compare."""
    if found.compose is None:
        return
    compose = folder / COMPOSE
    pinned = agent_memory_assets.pins(found.compose.decode("utf-8", errors="replace"))
    if len(pinned) != 1:
        deps.out(f"{compose} pins no {agent_memory_assets.IMAGE} version setup can read, so its version is unknown. Going on.")
        return
    old, new = version_tuple(pinned[0]), version_tuple(version)
    if old is None or new is None:
        deps.out(f"{compose} pins the server at {pinned[0]}, and this CLI is {version}: setup can't compare them. Going on.")
        return
    if old > new and not force:
        raise SetupFailed(
            f"{compose} runs the server at {pinned[0]}, newer than this CLI ({version}), and an older server may not read "
            "its data. Upgrade the CLI, then run agent-memory setup again, or pass --force to go back anyway."
        )
    if old > new:
        deps.out(f"{compose} ran the server at {pinned[0]}. Going back to {version}, because of --force.")


# Steps 1 and 5: Docker, and who holds the port.


def compose_argv(folder, *command):
    # -f stops compose from searching parent folders; --project-directory makes
    # it read this folder's .env.
    return ["docker", "compose", "-p", PROJECT, "-f", str(folder / COMPOSE), "--project-directory", str(folder), *command]


def check_docker(deps):
    if not deps.which("docker"):
        raise SetupFailed(f"Docker is not installed. Install {DOCKER_HINT}, then run agent-memory setup again.")
    if deps.run(["docker", "compose", "version"]).returncode != 0:
        raise SetupFailed(
            "docker compose doesn't work. Install the Docker Compose plugin (Docker Desktop and OrbStack include it), "
            "then run agent-memory setup again."
        )
    if deps.run(["docker", "info", "--format", "{{.ServerVersion}}"]).returncode != 0:
        raise SetupFailed(
            f"Docker is installed, but its daemon isn't running. Start {DOCKER_HINT}, then run agent-memory setup again."
        )


def compose_projects(deps):
    """[(name, status, [config files])] for each compose project, running or stopped."""
    # -a: a stopped project still owns its containers, and up -d from here would recreate them.
    ran = deps.run(["docker", "compose", "ls", "-a", "--format", "json"])
    if ran.returncode != 0:
        raise SetupFailed("docker compose ls failed, so setup can't tell what runs on port 8765. Check Docker, then run agent-memory setup again.")
    text = ran.stdout.strip()
    if not text:
        return []
    try:
        listed = json.loads(text)
    except json.JSONDecodeError:
        listed = None
    if not isinstance(listed, list) or not all(isinstance(item, dict) for item in listed):
        raise SetupFailed(
            "docker compose ls answered with something setup can't read, so it can't tell what runs on port 8765. "
            "Update Docker Compose, then run agent-memory setup again."
        )
    projects = []
    for item in listed:
        files = [part.strip() for part in str(item.get("ConfigFiles") or "").split(",") if part.strip()]
        projects.append((str(item.get("Name") or ""), str(item.get("Status") or ""), files))
    return projects


def is_running(status):
    return "running" in status.lower()


def refuse_other_project(folder, status, files):
    """Our project name, from files that aren't this folder's compose.yaml. Says how to stop it, never looping."""
    state = "runs" if is_running(status) else "exists, stopped,"
    if not files:
        raise SetupFailed(
            f"The {PROJECT} compose project {state} in another folder, and starting it here would replace its "
            f"containers. Remove it with `docker compose -p {PROJECT} down`, then run agent-memory setup again."
        )
    down = shlex.join(["docker", "compose", "-p", PROJECT, *[part for path in files for part in ("-f", path)], "down"])
    where = Path(files[0]).parent
    real_files = {os.path.realpath(path) for path in files}
    # --dir helps only when that folder's own compose.yaml is what runs. Otherwise
    # (compose.team.yaml, say, or this very folder through another file) it would refuse again.
    if os.path.realpath(where) != os.path.realpath(folder) and os.path.realpath(where / COMPOSE) in real_files:
        raise SetupFailed(
            f"The {PROJECT} compose project {state} in {where}, and starting it here would replace its containers. "
            f"Run agent-memory setup --dir {shlex.quote(str(where))} to keep using that folder, or remove it with `{down}`."
        )
    raise SetupFailed(
        f"The {PROJECT} compose project {state} from {', '.join(files)}, which setup doesn't manage, and starting it "
        f"here would replace its containers. Remove it with `{down}`, then run agent-memory setup again."
    )


def check_port(folder, deps):
    """Port 8765 must be free, or held by this folder's own project, which setup then restarts."""
    own = os.path.realpath(folder / COMPOSE)
    projects = compose_projects(deps)
    for name, status, files in projects:
        if name != PROJECT:
            continue
        if any(os.path.realpath(path) == own for path in files):
            return
        refuse_other_project(folder, status, files)
    holder = deps.port_holder()
    if holder == "agent-memory":
        others = [f"{name} in {Path(files[0]).parent}" if files else name for name, status, files in projects if is_running(status)]
        running = f" Running compose projects: {', '.join(others)}." if others else ""
        raise SetupFailed(
            f"Another agent-memory server already answers on {URL}, most likely a clone's ./scripts/setup.{running} "
            "Stop it with `docker compose down` in that clone, then run agent-memory setup again."
        )
    if holder == "other":
        raise SetupFailed(f"Another program already listens on 127.0.0.1:{PORT}. Stop it, then run agent-memory setup again.")


# Steps 6 to 8: the writes.


def write_atomic(path, data, mode):
    temp = path.with_name(path.name + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.chmod(temp, mode)
    os.replace(temp, path)


def make_folders(folder):
    """The folder, mode 0700 when setup creates it, and data/server and data/qdrant.

    The data folders must exist before Docker starts: on Linux Docker creates a
    missing bind folder owned by root, and the server couldn't write to it.
    """
    if not folder.exists():
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700)
        os.chmod(folder, 0o700)
    for name in ("server", "qdrant"):
        (folder / "data" / name).mkdir(parents=True, exist_ok=True)


def backup_name(folder, now):
    stamp = now.strftime("%Y%m%d-%H%M%S")
    path = folder / f"{COMPOSE}.{stamp}.bak"
    count = 1
    while path.exists():
        path = folder / f"{COMPOSE}.{stamp}-{count}.bak"
        count += 1
    return path


def write_compose(folder, found, version, deps):
    """compose.yaml from the package, and .setup.json with its hash. A file setup didn't write is kept as a .bak first."""
    data = agent_memory_assets.compose_bytes()
    compose = folder / COMPOSE
    if found.compose is not None and not found.ours:
        backup = backup_name(folder, deps.now())
        os.replace(compose, backup)
        deps.out(
            f"Saved the {COMPOSE} setup didn't write as {backup}. Put your settings in {folder / '.env'} "
            "instead, such as AGENT_MEMORY_IMAGE or AGENT_MEMORY_EMBEDDER."
        )
    write_atomic(compose, data, 0o644)
    sidecar = {"version": version, "compose_sha256": sha256(data)}
    write_atomic(folder / SIDECAR, (json.dumps(sidecar, indent=2) + "\n").encode("utf-8"), 0o644)


def env_has(text, key):
    return re.search(rf"^\s*(?:export\s+)?{key}\s*=", text, re.MULTILINE) is not None


def write_env(folder, found):
    """.env, mode 0600, with the host's uid and gid added when missing. Every other line stays."""
    path = folder / ".env"
    old = found.env or b""
    text = old.decode("utf-8", errors="replace")
    add = []
    # The server runs as you, so the files it writes in data/server are yours.
    if not env_has(text, "AGENT_MEMORY_UID"):
        add.append(f"AGENT_MEMORY_UID={os.getuid()}")
    if not env_has(text, "AGENT_MEMORY_GID"):
        add.append(f"AGENT_MEMORY_GID={os.getgid()}")
    if not add and found.env is not None:
        os.chmod(path, 0o600)
        return
    separator = b"\n" if old and not old.endswith(b"\n") else b""
    write_atomic(path, old + separator + ("\n".join(add) + "\n").encode("utf-8"), 0o600)


def env_value(folder, key):
    """key's value as compose sees it: the environment first, then .env. Empty counts as unset, as ${KEY:-...} does."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        text = (folder / ".env").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    found = ""
    for raw in text.splitlines():
        match = re.match(rf"^\s*(?:export\s+)?{key}\s*=(.*)$", raw)
        if match:
            value = match.group(1).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            found = value
    return found


def server_image(folder):
    """The image compose will run: AGENT_MEMORY_IMAGE when set, else the pin in the packaged compose.yaml."""
    pinned = agent_memory_assets.pins(agent_memory_assets.compose_text())
    return env_value(folder, "AGENT_MEMORY_IMAGE") or f"{agent_memory_assets.IMAGE}:{pinned[0]}"


# Steps 9 and 10: pull, start, wait.


def show_output(ran, deps, lines=20):
    shown = (ran.stderr or "").strip().splitlines() + (ran.stdout or "").strip().splitlines()
    for line in shown[-lines:]:
        deps.err(line)


def start(folder, deps):
    image = server_image(folder)
    deps.out(f"Pulling {image} and Qdrant. The first pull can take a few minutes.")
    pulled = deps.run(compose_argv(folder, "pull"))
    if pulled.returncode != 0:
        show_output(pulled, deps)
        raise SetupFailed(
            f"docker compose pull failed for {image}. GHCR may be unreachable, or the image isn't public yet. "
            "Check your network, then run agent-memory setup again."
        )
    # compose can warn, not fail, when a pull fails for a service that also has build:.
    if deps.run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).returncode != 0:
        show_output(pulled, deps)
        raise SetupFailed(
            f"{image} isn't on this machine after the pull. GHCR may be unreachable, or the image isn't public yet. "
            "Check your network, then run agent-memory setup again."
        )
    # --no-build: the packaged file keeps build: . for clones, and this folder has no Dockerfile.
    started = deps.run(compose_argv(folder, "up", "-d", "--no-build"))
    if started.returncode != 0:
        show_output(started, deps)
        said = f"{started.stderr}\n{started.stdout}".lower()
        if any(words in said for words in MOUNT_ERRORS):
            raise SetupFailed(
                f"Docker couldn't mount {folder / 'data'}. Snap-installed Docker can't mount hidden folders under your "
                "home, and Docker Desktop mounts only the folders it shares. Run agent-memory setup --dir with a folder "
                "Docker can see, such as ~/agent-memory-server."
            )
        raise SetupFailed("docker compose up failed; its output is above. Fix the cause, then run agent-memory setup again.")


def wait_for_health(folder, deps):
    deps.out(f"Waiting for {URL}/v1/health, for up to {HEALTH_TIMEOUT} seconds.")
    deadline = deps.clock() + HEALTH_TIMEOUT
    while not deps.health(URL):
        if deps.clock() >= deadline:
            deps.err("Recent logs from the server and Qdrant:")
            show_output(deps.run(compose_argv(folder, "logs", "--tail", str(LOG_LINES), "server", "qdrant")), deps, lines=2 * LOG_LINES)
            raise SetupFailed(
                f"The server didn't answer {URL}/v1/health within {HEALTH_TIMEOUT} seconds. Check the logs above, "
                "then run agent-memory setup again."
            )
        deps.sleep(1)


# Steps 11 and 12: the CLI, then the editors.


@dataclass
class CliConfig:
    """What connect_cli found: whether the config names this server, why not, and a write that failed."""

    this_server: bool
    why_not: str = ""
    failed: str = ""


def connect_cli(deps):
    """Write the CLI's server config when it's missing, and say what it names."""
    path = server_client.config_dir() / "server"
    try:
        if not path.exists():
            # The mode the legacy database file had, or shared, as scripts/setup does.
            written = server_client.write_config(URL, None, server_client.legacy_mode())
            deps.out(f"Wrote {written}, so the CLI sends approved memories to {URL}.")
            return CliConfig(True)
        config = server_client.read_config()
    except server_client.ConfigError as error:
        deps.out(f"Left {path} as it is: {error}.")
        deps.out(f"To use this server, run: agent-memory connect --url {URL}")
        return CliConfig(False, "the CLI's server config can't be read")
    except OSError as error:
        return CliConfig(
            False,
            f"setup couldn't write {path}",
            f"The server runs, but setup couldn't write {path}: {error.strerror}. Fix that, then run agent-memory connect --url {URL}.",
        )
    if config is None:
        deps.out(f"Left {path} as it is. It names no server; to use this one, run: agent-memory connect --url {URL}")
        return CliConfig(False, "the CLI's server config names no server")
    if config["url"] in SAME_SERVER:
        deps.out(f"Left {path} as it is. It already points at {config['url']}.")
        return CliConfig(True)
    deps.out(f"Left {path} as it is. It points the CLI at {config['url']}, not at this server.")
    deps.out(f"To use this server instead, run: agent-memory connect --url {URL}")
    # connect --editor registers the servers the CLI's config names, which isn't this one.
    return CliConfig(False, "the CLI points at another server")


def offer_editors(found, cli, options, deps):
    """Ask once whether to add MCP to the editors found. Returns connect's exit code, or 0 when nothing ran."""
    if not found:
        return 0
    if not cli.this_server:
        deps.out(f"Setup doesn't offer to add MCP to your editors, because {cli.why_not}.")
        return 0
    if options.no_input or not deps.interactive():
        return 0
    try:
        answer = deps.ask(f"Add MCP to {', '.join(found)}? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"y", "yes"}:
        return 0
    # connect --editor shows what it will write, and asks again.
    try:
        code = deps.connect_editors(found)
    except SystemExit as error:
        if isinstance(error.code, str):
            deps.err(error.code)
            return 1
        code = error.code
    return code or 0


def next_steps(folder, found, deps):
    compose = shlex.join(compose_argv(folder))
    deps.out("")
    deps.out("Next steps:")
    deps.out("  agent-memory install    # capture hooks for your editors, after a yes")
    deps.out(f"  agent-memory connect --editor {','.join(found) or 'claude-code,cursor,codex'}    # the read-only MCP tools")
    deps.out("")
    deps.out(f"Logs:    {compose} logs")
    deps.out(f"Stop:    {compose} down")
    deps.out("After you upgrade the CLI, run agent-memory setup again. It keeps .env and data/.")


# The command.


def setup(options, deps):
    """options has dir, force, no_input and print_dir. Returns the exit code, or raises SetupFailed."""
    folder, chosen = pick_dir(options.dir)
    if options.print_dir:
        deps.out(str(folder))
        return 0

    # Checks: nothing is written until all of them pass.
    check_docker(deps)
    found = read_folder(folder)
    check_downgrade(found, folder, deps.version, options.force, deps)
    check_port(folder, deps)

    # Writes.
    try:
        if chosen:
            remember_dir(folder)
        make_folders(folder)
        write_compose(folder, found, deps.version, deps)
        write_env(folder, found)
    except OSError as error:
        raise SetupFailed(f"Could not write {error.filename or folder}: {error.strerror}. Fix that, then run agent-memory setup again.") from None

    start(folder, deps)
    wait_for_health(folder, deps)
    deps.out(f"The agent-memory server is ready at {URL}, from {folder}.")
    # A custom AGENT_MEMORY_IMAGE can run another version than the pin; say so.
    warning = server_client.version_line(deps.health_answer(URL), deps.version)
    if warning:
        deps.err(warning)

    cli = connect_cli(deps)
    tools = [tool for tool in deps.present_tools() if tool not in editors.NO_MCP]
    code = offer_editors(tools, cli, options, deps)
    next_steps(folder, tools, deps)
    if cli.failed:
        raise SetupFailed(cli.failed)
    if code:
        raise SetupFailed(f"The server runs, but adding MCP to the editors failed. Fix the cause above, then run agent-memory connect --editor {','.join(tools)}.")
    return 0
