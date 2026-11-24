#!/usr/bin/env python3
"""`agent-memory setup` (server_setup.py), with no Docker and no server.

Standard library only. A fake runner plays Docker: it records each argv and
answers with scripted results, so the tests check the exact compose calls.
Health and the port probe are fakes too, except in the tests of the real
probes, which use a stub server on a free port. Every test has a temporary
HOME, XDG_DATA_HOME and XDG_CONFIG_HOME, so nothing touches the real
~/.config, the editors' files, or a server on 8765.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory
import agent_memory_assets
import server_client
import server_setup
from server_setup import Ran, SetupFailed
from stub_server import StubServer

PACKAGED = agent_memory_assets.compose_bytes()
PINNED = agent_memory_assets.pins(PACKAGED.decode("utf-8"))[0]
IMAGE = f"{agent_memory_assets.IMAGE}:{PINNED}"
NOW = datetime(2026, 11, 24, 15, 30, 0)


def with_pin(version):
    """The packaged compose.yaml, pinned at another version."""
    return PACKAGED.replace(f"{agent_memory_assets.IMAGE}:{PINNED}".encode(), f"{agent_memory_assets.IMAGE}:{version}".encode())


def kind(argv):
    """What a docker argv does, for the fake's scripted answers."""
    if argv[:3] == ["docker", "compose", "version"]:
        return "version"
    if argv[:2] == ["docker", "info"]:
        return "info"
    if argv[:3] == ["docker", "compose", "ls"]:
        return "ls"
    if argv[:3] == ["docker", "image", "inspect"]:
        return "inspect"
    if argv[:2] == ["docker", "compose"] and "--project-directory" in argv:
        return argv[argv.index("--project-directory") + 2]
    return "other"


class FakeDocker:
    """Records every call. Each kind answers 0 with no output unless a test says otherwise."""

    def __init__(self):
        self.calls = []
        self.answers = {"ls": Ran(0, "[]\n", "")}

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.answers.get(kind(argv), Ran(0, "", ""))

    def kinds(self):
        return [kind(argv) for argv in self.calls]


def snapshot(root):
    """Every path under root with its type, mode and bytes, for 'nothing changed'."""
    found = {}
    root = Path(root)
    if not root.exists():
        return found
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            relative = str(path.relative_to(root))
            if stat.S_ISLNK(info.st_mode):
                found[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[relative] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                found[relative] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
    return found


def mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


class SetupCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # resolve(): macOS's /var is a link to /private/var, and compose ls reports real paths.
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.xdg = self.base / "xdg-data"
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.xdg), "XDG_CONFIG_HOME": str(self.base / "xdg-config")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("AGENT_MEMORY_DIR", "AGENT_MEMORY_IMAGE"):
            os.environ.pop(name, None)
        self.folder = self.xdg / "agent-memory" / "server"
        self.config = self.home / ".config" / "agent-memory"
        self.docker = FakeDocker()
        self.answers = []
        self.asked = []
        self.connected = []
        self.probed = []
        self.time = [0.0]
        self.deps = server_setup.Deps(
            version=PINNED,
            run=self.docker,
            which=lambda name: f"/usr/local/bin/{name}",
            health=lambda url: True,
            port_holder=self.probe,
            sleep=lambda seconds: self.time.__setitem__(0, self.time[0] + seconds),
            clock=lambda: self.time[0],
            now=lambda: NOW,
            interactive=lambda: False,
            ask=self.ask,
            present_tools=lambda: [],
            connect_editors=self.connect,
        )
        self.holder = None

    def probe(self):
        self.probed.append(True)
        return self.holder

    def ask(self, prompt):
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def connect(self, tools):
        self.connected.append(list(tools))
        return 0

    def options(self, **values):
        given = {"dir": "", "force": False, "no_input": False, "print_dir": False}
        given.update(values)
        return argparse.Namespace(**given)

    def setup(self, **values):
        """(exit code or the SetupFailed text, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = server_setup.setup(self.options(**values), self.deps)
            except SetupFailed as error:
                result = str(error)
        return result, out.getvalue(), err.getvalue()

    def succeed(self, **values):
        result, out, err = self.setup(**values)
        self.assertEqual(result, 0, f"{result}\n{out}\n{err}")
        return out

    def refuse(self, **values):
        result, out, err = self.setup(**values)
        self.assertIsInstance(result, str, f"setup went ahead:\n{out}\n{err}")
        return result

    def compose(self, folder=None, *command):
        folder = folder or self.folder
        return ["docker", "compose", "-p", "agent-memory-server", "-f", str(folder / "compose.yaml"), "--project-directory", str(folder), *command]

    def first_run(self):
        self.succeed()
        self.docker.calls.clear()
        self.probed.clear()


class FirstRunTest(SetupCase):
    def test_it_creates_the_folder_compose_sidecar_data_and_env(self):
        self.succeed()
        self.assertEqual(mode(self.folder), 0o700)
        self.assertEqual((self.folder / "compose.yaml").read_bytes(), PACKAGED)
        sidecar = json.loads((self.folder / ".setup.json").read_text())
        self.assertEqual(sidecar, {"version": PINNED, "compose_sha256": hashlib.sha256(PACKAGED).hexdigest()})
        self.assertTrue((self.folder / "data" / "server").is_dir())
        self.assertTrue((self.folder / "data" / "qdrant").is_dir())
        env = self.folder / ".env"
        self.assertEqual(mode(env), 0o600)
        self.assertEqual(env.read_text(), f"AGENT_MEMORY_UID={os.getuid()}\nAGENT_MEMORY_GID={os.getgid()}\n")
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [".env", ".setup.json", "compose.yaml", "data"])

    def test_it_runs_the_compose_calls_in_order_with_the_project_file_and_folder(self):
        self.succeed()
        self.assertEqual(
            self.docker.calls,
            [
                ["docker", "compose", "version"],
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                ["docker", "compose", "ls", "--format", "json"],
                self.compose(None, "pull"),
                ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
                self.compose(None, "up", "-d", "--no-build"),
            ],
        )
        self.assertEqual(self.probed, [True])

    def test_it_writes_the_server_config_when_it_is_missing(self):
        out = self.succeed()
        written = (self.config / "server").read_text()
        self.assertIn("url = http://127.0.0.1:8765\n", written)
        self.assertIn("mode = shared\n", written)
        self.assertNotIn("token", written)
        self.assertEqual(mode(self.config / "server"), 0o600)
        self.assertIn(f"Wrote {self.config / 'server'}", out)

    def test_the_server_config_takes_the_legacy_mode(self):
        self.config.mkdir(parents=True)
        (self.config / "database").write_text("mode = per-repo\n")
        self.succeed()
        self.assertIn("mode = per-repo\n", (self.config / "server").read_text())

    def test_an_existing_server_config_for_another_url_is_left_alone_and_printed(self):
        self.config.mkdir(parents=True)
        (self.config / "server").write_text("url = https://memory.example.com\ntoken = am_secret\n")
        before = (self.config / "server").read_bytes()
        out = self.succeed()
        self.assertEqual((self.config / "server").read_bytes(), before)
        self.assertIn("https://memory.example.com", out)
        self.assertIn("agent-memory connect --url http://127.0.0.1:8765", out)

    def test_an_existing_server_config_for_this_server_is_left_alone(self):
        self.config.mkdir(parents=True)
        (self.config / "server").write_text("url = http://127.0.0.1:8765\nmode = per-repo\n")
        before = (self.config / "server").read_bytes()
        out = self.succeed()
        self.assertEqual((self.config / "server").read_bytes(), before)
        self.assertIn("Left", out)
        self.assertNotIn("connect --url", out)

    def test_the_final_message_names_the_logs_and_stop_commands(self):
        out = self.succeed()
        self.assertIn(f"docker compose -p agent-memory-server -f {self.folder / 'compose.yaml'} --project-directory {self.folder} logs", out)
        self.assertIn(f"docker compose -p agent-memory-server -f {self.folder / 'compose.yaml'} --project-directory {self.folder} down", out)
        self.assertIn("agent-memory install", out)
        self.assertNotIn("down -v", out)

    def test_an_empty_compose_ls_means_no_project_runs(self):
        self.docker.answers["ls"] = Ran(0, "", "")
        self.succeed()
        self.assertIn("up", self.docker.kinds())

    def test_agent_memory_dir_does_not_move_the_folder(self):
        os.environ["AGENT_MEMORY_DIR"] = str(self.base / "state")
        self.succeed()
        self.assertTrue((self.folder / "compose.yaml").is_file())
        self.assertFalse((self.base / "state").exists())

    def test_agent_memory_image_in_env_is_the_image_it_checks(self):
        self.folder.mkdir(parents=True)
        (self.folder / ".env").write_text("AGENT_MEMORY_IMAGE=ghcr.io/me/custom:dev\n")
        self.succeed()
        self.assertIn(["docker", "image", "inspect", "--format", "{{.Id}}", "ghcr.io/me/custom:dev"], self.docker.calls)


class RerunTest(SetupCase):
    def test_it_keeps_the_env_lines_and_data(self):
        self.first_run()
        env = self.folder / ".env"
        env.write_text(env.read_text() + "AGENT_MEMORY_EMBEDDER=fastembed\n# mine\n")
        kept = env.read_bytes()
        (self.folder / "data" / "server" / "memories.sqlite").write_bytes(b"rows")
        (self.folder / "data" / "qdrant" / "segment").write_bytes(b"vectors")
        self.succeed()
        self.assertEqual(env.read_bytes(), kept)
        self.assertEqual((self.folder / "data" / "server" / "memories.sqlite").read_bytes(), b"rows")
        self.assertEqual((self.folder / "data" / "qdrant" / "segment").read_bytes(), b"vectors")
        self.assertEqual(self.docker.kinds()[-1], "up")

    def test_it_adds_only_the_missing_ids_and_tightens_the_mode(self):
        self.folder.mkdir(parents=True)
        env = self.folder / ".env"
        env.write_text("AGENT_MEMORY_UID=1234\nAGENT_MEMORY_EMBEDDER=fastembed")
        env.chmod(0o644)
        self.succeed()
        self.assertEqual(env.read_text(), f"AGENT_MEMORY_UID=1234\nAGENT_MEMORY_EMBEDDER=fastembed\nAGENT_MEMORY_GID={os.getgid()}\n")
        self.assertEqual(mode(env), 0o600)

    def test_it_replaces_a_compose_file_whose_hash_matches_the_sidecar(self):
        self.first_run()
        older = with_pin("0.0.9")
        (self.folder / "compose.yaml").write_bytes(older)
        (self.folder / ".setup.json").write_text(json.dumps({"version": "0.0.9", "compose_sha256": hashlib.sha256(older).hexdigest()}))
        self.succeed()
        self.assertEqual((self.folder / "compose.yaml").read_bytes(), PACKAGED)
        self.assertEqual(list(self.folder.glob("*.bak")), [])
        self.assertEqual(json.loads((self.folder / ".setup.json").read_text())["version"], PINNED)

    def test_it_backs_up_a_hand_edited_compose_file(self):
        self.first_run()
        edited = PACKAGED + b"# my edit\n"
        (self.folder / "compose.yaml").write_bytes(edited)
        out = self.succeed()
        backup = self.folder / "compose.yaml.20261124-153000.bak"
        self.assertEqual(backup.read_bytes(), edited)
        self.assertEqual((self.folder / "compose.yaml").read_bytes(), PACKAGED)
        self.assertIn(str(backup), out)
        self.assertIn(".env", out)

    def test_it_backs_up_an_identical_downloaded_file_without_a_sidecar(self):
        # The manual steps in deploy.md download the same bytes, but setup didn't write them.
        self.folder.mkdir(parents=True)
        (self.folder / "compose.yaml").write_bytes(PACKAGED)
        self.succeed()
        self.assertEqual((self.folder / "compose.yaml.20261124-153000.bak").read_bytes(), PACKAGED)
        self.assertEqual((self.folder / "compose.yaml").read_bytes(), PACKAGED)
        self.assertTrue((self.folder / ".setup.json").is_file())

    def test_a_second_backup_in_the_same_second_gets_its_own_name(self):
        self.folder.mkdir(parents=True)
        (self.folder / "compose.yaml.20261124-153000.bak").write_bytes(b"earlier")
        (self.folder / "compose.yaml").write_bytes(b"mine\n")
        self.succeed()
        self.assertEqual((self.folder / "compose.yaml.20261124-153000.bak").read_bytes(), b"earlier")
        self.assertEqual((self.folder / "compose.yaml.20261124-153000-1.bak").read_bytes(), b"mine\n")

    def test_its_own_project_running_from_this_folder_is_restarted(self):
        self.first_run()
        # A link to the folder: compose may report the path another way.
        link = self.base / "link"
        link.symlink_to(self.folder)
        listed = [{"Name": "agent-memory-server", "Status": "running(2)", "ConfigFiles": str(link / "compose.yaml")}]
        self.docker.answers["ls"] = Ran(0, json.dumps(listed), "")
        self.holder = "agent-memory"
        self.succeed()
        self.assertEqual(self.probed, [])
        self.assertEqual(self.docker.kinds()[-1], "up")


class DowngradeTest(SetupCase):
    def newer_folder(self):
        self.first_run()
        newer = with_pin("99.0.0")
        (self.folder / "compose.yaml").write_bytes(newer)
        (self.folder / ".setup.json").write_text(json.dumps({"version": "99.0.0", "compose_sha256": hashlib.sha256(newer).hexdigest()}))

    def test_a_downgrade_is_refused(self):
        self.newer_folder()
        before = snapshot(self.base)
        message = self.refuse()
        self.assertIn("99.0.0", message)
        self.assertIn("Upgrade the CLI", message)
        self.assertIn("--force", message)
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.docker.kinds(), ["version", "info"])

    def test_force_goes_ahead(self):
        self.newer_folder()
        out = self.succeed(force=True)
        self.assertIn("--force", out)
        self.assertEqual((self.folder / "compose.yaml").read_bytes(), PACKAGED)
        self.assertEqual(self.docker.kinds()[-1], "up")

    def test_an_unparseable_pin_is_reported_and_setup_goes_on(self):
        self.folder.mkdir(parents=True)
        (self.folder / "compose.yaml").write_bytes(with_pin("dev"))
        out = self.succeed()
        self.assertIn("pins the server at dev", out)
        self.assertIn("Going on", out)
        self.assertEqual((self.folder / "compose.yaml").read_bytes(), PACKAGED)

    def test_a_compose_file_with_no_pin_is_unknown_and_setup_goes_on(self):
        self.folder.mkdir(parents=True)
        (self.folder / "compose.yaml").write_bytes(b"services:\n  server:\n    image: ghcr.io/me/mine:latest\n")
        out = self.succeed()
        self.assertIn("version is unknown", out)

    def test_an_older_pin_is_an_upgrade(self):
        self.folder.mkdir(parents=True)
        (self.folder / "compose.yaml").write_bytes(with_pin("0.0.1"))
        out = self.succeed()
        self.assertNotIn("Going", out)

    def test_versions_compare_as_ints(self):
        self.assertGreater(server_setup.version_tuple("0.10.0"), server_setup.version_tuple("0.9.0"))
        self.assertIsNone(server_setup.version_tuple("0.2.0rc1"))
        self.assertIsNone(server_setup.version_tuple(""))


class DirTest(SetupCase):
    def test_dir_is_remembered_and_a_rerun_uses_it(self):
        mine = self.base / "mine"
        self.succeed(dir=str(mine))
        self.assertIn(f"dir = {mine}\n", (self.config / "setup").read_text())
        self.assertTrue((mine / "compose.yaml").is_file())
        self.assertFalse(self.folder.exists())
        self.docker.calls.clear()
        self.succeed()
        self.assertIn(self.compose(mine, "up", "-d", "--no-build"), self.docker.calls)
        self.assertFalse(self.folder.exists())

    def test_print_dir_prints_the_folder_and_does_nothing_else(self):
        before = snapshot(self.base)
        result, out, _ = self.setup(print_dir=True)
        self.assertEqual((result, out), (0, f"{self.folder}\n"))
        mine = self.base / "mine"
        self.config.mkdir(parents=True)
        (self.config / "setup").write_text(f"dir = {mine}\n")
        result, out, _ = self.setup(print_dir=True)
        self.assertEqual((result, out), (0, f"{mine}\n"))
        (self.config / "setup").unlink()
        self.config.rmdir()
        self.config.parent.rmdir()
        self.assertEqual(snapshot(self.base), before)
        self.assertEqual(self.docker.calls, [])

    def test_a_dir_with_a_hash_in_its_name_is_remembered_whole(self):
        mine = self.base / "team #2"
        self.succeed(dir=str(mine))
        result, out, _ = self.setup(print_dir=True)
        self.assertEqual(out, f"{mine}\n")

    def test_a_relative_dir_is_made_absolute(self):
        with contextlib.chdir(self.base):
            self.succeed(dir="relative")
        self.assertIn(f"dir = {self.base / 'relative'}\n", (self.config / "setup").read_text())

    def test_an_existing_folder_keeps_its_mode(self):
        mine = self.base / "mine"
        mine.mkdir(mode=0o755)
        mine.chmod(0o755)
        self.succeed(dir=str(mine))
        self.assertEqual(mode(mine), 0o755)


class RefusalTest(SetupCase):
    """Each refusal leaves the tree byte-identical and runs no compose call after its check."""

    def setUp(self):
        super().setUp()
        self.first_run()
        (self.folder / ".env").write_text((self.folder / ".env").read_text() + "AGENT_MEMORY_EMBEDDER=fastembed\n")
        (self.folder / "data" / "server" / "memories.sqlite").write_bytes(b"rows")
        (self.config / "server").unlink()
        self.before = snapshot(self.base)

    def assertUnchanged(self, kinds):
        self.assertEqual(snapshot(self.base), self.before)
        self.assertEqual(self.docker.kinds(), kinds)

    def test_its_project_running_from_another_folder(self):
        listed = [{"Name": "agent-memory-server", "Status": "running(2)", "ConfigFiles": "/srv/agent-memory-server/compose.yaml"}]
        self.docker.answers["ls"] = Ran(0, json.dumps(listed), "")
        message = self.refuse()
        self.assertIn("/srv/agent-memory-server", message)
        self.assertIn("--dir /srv/agent-memory-server", message)
        self.assertUnchanged(["version", "info", "ls"])
        self.assertEqual(self.probed, [])

    def test_its_project_from_an_unknown_folder(self):
        self.docker.answers["ls"] = Ran(0, json.dumps([{"Name": "agent-memory-server", "Status": "running(2)", "ConfigFiles": ""}]), "")
        self.assertIn("docker compose -p agent-memory-server down", self.refuse())
        self.assertUnchanged(["version", "info", "ls"])

    def test_its_project_from_another_folder_also_refuses_a_new_dir(self):
        listed = [{"Name": "agent-memory-server", "Status": "running(2)", "ConfigFiles": str(self.folder / "compose.yaml")}]
        self.docker.answers["ls"] = Ran(0, json.dumps(listed), "")
        self.refuse(dir=str(self.base / "elsewhere"))
        self.assertUnchanged(["version", "info", "ls"])

    def test_port_8765_answers_for_another_project(self):
        listed = [{"Name": "agent-memory", "Status": "running(2)", "ConfigFiles": "/code/agent-memory/compose.yaml"}]
        self.docker.answers["ls"] = Ran(0, json.dumps(listed), "")
        self.holder = "agent-memory"
        message = self.refuse()
        self.assertIn("docker compose down", message)
        self.assertIn("agent-memory in /code/agent-memory", message)
        self.assertUnchanged(["version", "info", "ls"])

    def test_another_program_on_the_port(self):
        self.holder = "other"
        message = self.refuse()
        self.assertIn("8765", message)
        self.assertUnchanged(["version", "info", "ls"])

    def test_a_downgrade(self):
        newer = with_pin("99.0.0")
        (self.folder / "compose.yaml").write_bytes(newer)
        (self.folder / ".setup.json").write_text(json.dumps({"version": "99.0.0", "compose_sha256": hashlib.sha256(newer).hexdigest()}))
        self.before = snapshot(self.base)
        self.refuse()
        self.assertUnchanged(["version", "info"])

    def test_docker_missing(self):
        self.deps.which = lambda name: None
        self.assertIn("Docker is not installed", self.refuse())
        self.assertUnchanged([])

    def test_docker_compose_failing(self):
        self.docker.answers["version"] = Ran(1, "", "docker: 'compose' is not a docker command.")
        self.assertIn("Docker Compose plugin", self.refuse())
        self.assertUnchanged(["version"])

    def test_the_daemon_not_running(self):
        self.docker.answers["info"] = Ran(1, "", "Cannot connect to the Docker daemon")
        self.assertIn("daemon isn't running", self.refuse())
        self.assertUnchanged(["version", "info"])

    def test_compose_ls_failing(self):
        self.docker.answers["ls"] = Ran(1, "", "boom")
        self.assertIn("docker compose ls failed", self.refuse())
        self.assertUnchanged(["version", "info", "ls"])

    def test_compose_ls_unreadable(self):
        for answer in ("not json", '{"Name": "x"}', '["x"]'):
            self.docker.calls.clear()
            self.docker.answers["ls"] = Ran(0, answer, "")
            self.assertIn("can't read", self.refuse())
            self.assertUnchanged(["version", "info", "ls"])

    def test_a_refusal_on_a_first_run_creates_nothing(self):
        fresh = self.base / "fresh"
        self.holder = "other"
        self.refuse(dir=str(fresh))
        self.assertFalse(fresh.exists())
        self.assertUnchanged(["version", "info", "ls"])


class StartFailureTest(SetupCase):
    def test_a_failed_pull_exits_with_the_image_name_and_up_is_not_run(self):
        self.docker.answers["pull"] = Ran(1, "", "error from registry: denied")
        result, _, err = self.setup()
        self.assertIn(IMAGE, result)
        self.assertIn("denied", err)
        self.assertEqual(self.docker.kinds()[-1], "pull")
        self.assertFalse((self.config / "server").exists())

    def test_a_missing_image_after_pull_exits_with_its_name_and_up_is_not_run(self):
        self.docker.answers["inspect"] = Ran(1, "", "No such image")
        result, _, _ = self.setup()
        self.assertIn(f"{IMAGE} isn't on this machine", result)
        self.assertEqual(self.docker.kinds()[-2:], ["pull", "inspect"])

    def test_a_failed_bind_mount_suggests_another_dir(self):
        self.docker.answers["up"] = Ran(1, "", "Error response from daemon: Mounts denied: The path /x is not shared from the host")
        result, _, _ = self.setup()
        self.assertIn("--dir", result)
        self.assertIn("couldn't mount", result)

    def test_another_up_failure_says_so(self):
        self.docker.answers["up"] = Ran(1, "", "something else")
        result, _, err = self.setup()
        self.assertIn("docker compose up failed", result)
        self.assertIn("something else", err)

    def test_a_health_timeout_exits_and_shows_the_logs(self):
        self.deps.health = lambda url: False
        self.docker.answers["logs"] = Ran(0, "server | boom\n", "")
        result, _, err = self.setup()
        self.assertIn("120 seconds", result)
        self.assertEqual(self.docker.calls[-1], self.compose(None, "logs", "--tail", "40", "server", "qdrant"))
        self.assertIn("server | boom", err)
        self.assertGreaterEqual(self.time[0], 120)
        self.assertFalse((self.config / "server").exists())

    def test_it_waits_until_health_answers(self):
        answers = [False, False, True]
        self.deps.health = lambda url: answers.pop(0)
        self.succeed()
        self.assertEqual(self.time[0], 2)


class EditorOfferTest(SetupCase):
    def setUp(self):
        super().setUp()
        self.deps.present_tools = lambda: ["cursor", "claude-code", "pi"]

    def test_without_a_terminal_it_only_prints(self):
        out = self.succeed()
        self.assertEqual(self.asked, [])
        self.assertEqual(self.connected, [])
        self.assertIn("agent-memory connect --editor cursor,claude-code", out)
        self.assertNotIn("pi", out.split("connect --editor", 1)[1].splitlines()[0])

    def test_in_a_terminal_no_runs_nothing(self):
        self.deps.interactive = lambda: True
        self.answers = ["n"]
        self.succeed()
        self.assertEqual(self.asked, ["Add MCP to cursor, claude-code? [y/N] "])
        self.assertEqual(self.connected, [])

    def test_enter_and_eof_mean_no(self):
        self.deps.interactive = lambda: True
        self.answers = [""]
        self.succeed()
        self.answers = []
        self.succeed()
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(self.connected, [])

    def test_yes_connects_the_editors_found_minus_pi(self):
        self.deps.interactive = lambda: True
        self.answers = ["y"]
        self.succeed()
        self.assertEqual(self.connected, [["cursor", "claude-code"]])

    def test_no_input_never_asks(self):
        self.deps.interactive = lambda: True
        self.answers = ["y"]
        self.succeed(no_input=True)
        self.assertEqual((self.asked, self.connected), ([], []))

    def test_a_config_for_another_server_means_no_question(self):
        self.deps.interactive = lambda: True
        self.answers = ["y"]
        self.config.mkdir(parents=True)
        (self.config / "server").write_text("url = https://memory.example.com\n")
        out = self.succeed()
        self.assertEqual((self.asked, self.connected), ([], []))
        self.assertIn("another server", out)

    def test_nothing_found_means_no_question(self):
        self.deps.interactive = lambda: True
        self.deps.present_tools = lambda: ["pi"]
        self.answers = ["y"]
        self.succeed()
        self.assertEqual((self.asked, self.connected), ([], []))

    def test_a_failed_connect_fails_setup_with_the_next_step(self):
        self.deps.interactive = lambda: True
        self.answers = ["y"]

        def broken(tools):
            raise SystemExit("no server is configured")

        self.deps.connect_editors = broken
        result, out, err = self.setup()
        self.assertIn("agent-memory connect --editor cursor,claude-code", result)
        self.assertIn("no server is configured", err)
        self.assertIn("Next steps", out)


class EditorFilesTest(SetupCase):
    """Whatever the answers, setup itself writes nothing under the editors' folders."""

    def test_editor_files_stay_untouched(self):
        for name in (".claude", ".cursor", ".codex", ".pi/agent"):
            (self.home / name).mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text("{}\n")
        (self.home / ".claude.json").write_text('{"mcpServers": {}}\n')
        (self.home / ".cursor" / "mcp.json").write_text("{}\n")
        watched = [self.home / name for name in (".claude", ".claude.json", ".cursor", ".codex", ".pi")]

        def state():
            return {str(path): snapshot(path) if path.is_dir() else path.read_bytes() for path in watched}

        before = state()
        self.deps.interactive = lambda: True
        with mock.patch.object(agent_memory, "HOME", self.home):
            self.deps.present_tools = agent_memory.present_tools
            for answer in ("y", "n", ""):
                self.answers = [answer]
                self.succeed()
        self.assertEqual(state(), before)
        self.assertEqual(self.connected, [["cursor", "claude-code", "codex"]])
        self.assertFalse((self.home / ".codex" / "config.toml").exists())


class WiringTest(SetupCase):
    """The thin subcommand in agent_memory.py."""

    def test_a_yes_runs_connect_editor_for_those_tools_which_asks_again(self):
        seen = []
        with mock.patch.object(agent_memory, "connect_editors", lambda args: seen.append(args) or 0):
            self.assertEqual(agent_memory.connect_found_editors(["claude-code", "cursor"]), 0)
        (args,) = seen
        self.assertEqual(args.editor, "claude-code,cursor")
        self.assertFalse(args.yes)
        self.assertFalse(args.print)
        self.assertFalse(args.url)

    def test_a_failure_exits_1_with_its_line(self):
        with mock.patch.object(server_setup, "setup", side_effect=SetupFailed("Start Docker, then run agent-memory setup again.")):
            with self.assertRaises(SystemExit) as raised:
                agent_memory.setup_cmd(self.options())
        self.assertEqual(raised.exception.code, "Start Docker, then run agent-memory setup again.")

    def test_the_flags_parse(self):
        args = agent_memory.build_parser().parse_args(["setup", "--dir", "/x", "--force", "--no-input", "--print-dir"])
        self.assertEqual((args.dir, args.force, args.no_input, args.print_dir), ("/x", True, True, True))
        self.assertIs(args.func, agent_memory.setup_cmd)

    def test_print_dir_from_the_command_line(self):
        # AGENT_MEMORY_DIR keeps main()'s one-time state move away from this checkout; it doesn't move the folder.
        env = dict(os.environ, PATH="/usr/bin:/bin", AGENT_MEMORY_DIR=str(self.base / "state"))
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "agent_memory.py"), "setup", "--print-dir"], env=env, text=True, capture_output=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, f"{self.folder}\n")

    def test_help_names_the_flags(self):
        done = subprocess.run([sys.executable, str(SCRIPTS / "agent_memory.py"), "setup", "--help"], text=True, capture_output=True)
        for flag in ("--dir", "--force", "--no-input", "--print-dir"):
            self.assertIn(flag, done.stdout)


class ProbeTest(unittest.TestCase):
    """The real health check, port probe and runner, against a stub server on a free port."""

    def test_health_and_the_port_probe_see_an_agent_memory_server(self):
        stub = StubServer()
        self.addCleanup(stub.close)
        self.assertTrue(server_setup.health_ok(stub.url))
        self.assertEqual(server_setup.probe_port(stub.url), "agent-memory")

    def test_the_port_probe_sees_another_program(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)

        def hang_up():
            with contextlib.suppress(OSError):
                while True:
                    connection, _ = listener.accept()
                    connection.close()

        threading.Thread(target=hang_up, daemon=True).start()
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self.assertFalse(server_setup.health_ok(url))
        self.assertEqual(server_setup.probe_port(url), "other")

    def test_the_port_probe_sees_a_free_port(self):
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        self.assertIsNone(server_setup.probe_port(f"http://127.0.0.1:{port}"))

    def test_the_runner_returns_code_and_output(self):
        ran = server_setup.run_docker([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((ran.returncode, ran.stdout, ran.stderr), (3, "out\n", "err\n"))
        self.assertEqual(server_setup.run_docker(["/nonexistent/docker", "info"]).returncode, 127)


if __name__ == "__main__":
    unittest.main()
