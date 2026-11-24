"""PR 1 of docs/distribution-plan.md: the package name, versions, the Windows guard,
and install refusing a run that won't last.

Every test has a temporary HOME and XDG_DATA_HOME, so none of them touches a
real editor folder.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory  # noqa: E402
import agent_memory_server  # noqa: E402
from test_packaging import Temp, console_script, make_clone, tree  # noqa: E402


def pyproject():
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]


class NameTest(unittest.TestCase):
    def test_dist_is_the_project_name_in_both_places(self):
        self.assertEqual(agent_memory.DIST, pyproject()["name"])
        self.assertEqual(agent_memory_server.DIST, agent_memory.DIST)

    def test_every_install_hint_names_dist(self):
        self.assertIn(agent_memory.DIST, agent_memory.INSTALL_HINT)
        self.assertIn(f"{agent_memory.DIST}[server]", agent_memory_server.INSTALL_HINT)

    def test_the_server_without_its_extra_prints_the_hint(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = agent_memory_server.main([], find_spec=lambda name: None)
        self.assertEqual(code, 2)
        self.assertIn(agent_memory_server.INSTALL_HINT, err.getvalue())

    def test_hook_bin_with_no_command_names_the_install(self):
        with mock.patch.object(agent_memory, "BIN", Path("/nonexistent/bin/agent-memory")), mock.patch.object(
            agent_memory.shutil, "which", return_value=None
        ), mock.patch.object(sys, "argv", ["python3"]):
            with self.assertRaises(SystemExit) as caught:
                agent_memory.hook_bin()
        self.assertIn(agent_memory.INSTALL_HINT, str(caught.exception))


class VersionTest(unittest.TestCase):
    def test_version_matches_pyproject(self):
        result = subprocess.run([str(ROOT / "bin" / "agent-memory"), "--version"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"agent-memory {pyproject()['version']}")

    def test_without_an_install_it_reads_pyproject(self):
        missing = agent_memory.importlib.metadata.PackageNotFoundError
        with mock.patch.object(agent_memory.importlib.metadata, "version", side_effect=missing):
            self.assertEqual(agent_memory.cli_version(), pyproject()["version"])
        with mock.patch.object(agent_memory_server.importlib.metadata, "version", side_effect=missing):
            self.assertEqual(agent_memory_server.package_version(), pyproject()["version"])

    def test_an_install_wins_over_pyproject(self):
        with mock.patch.object(agent_memory.importlib.metadata, "version", return_value="9.9.9") as lookup:
            self.assertEqual(agent_memory.cli_version(), "9.9.9")
        lookup.assert_called_once_with(agent_memory.DIST)


class WindowsGuardTest(Temp):
    def test_main_exits_2_with_the_wsl_message_and_writes_nothing(self):
        err = io.StringIO()
        with mock.patch.object(agent_memory.os, "name", "nt"), mock.patch.object(
            sys, "argv", ["agent-memory", "config", "."]
        ), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                agent_memory.main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("macOS, Linux and WSL 2", err.getvalue())
        self.assertIn("docs/deploy.md#windows-wsl-2", err.getvalue())
        self.assertEqual(tree(self.home), {})
        self.assertFalse(self.xdg.exists())

    def test_the_anchor_exists_in_the_docs(self):
        anchor = agent_memory.WINDOWS_MESSAGE.rsplit("#", 1)[1]
        headings = [line.lstrip("# ").lower() for line in (ROOT / "docs" / "deploy.md").read_text().splitlines() if line.startswith("#")]
        self.assertIn(anchor, [h.replace(" ", "-").replace("(", "").replace(")", "") for h in headings])

    def test_outbox_imports_without_fcntl(self):
        code = (
            "import sys; sys.modules['fcntl'] = None; sys.path.insert(0, sys.argv[1])\n"
            "import outbox, tempfile, pathlib\n"
            "with tempfile.TemporaryDirectory() as folder:\n"
            "    with outbox.lock(pathlib.Path(folder)):\n"
            "        pass\n"
            "print('ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], text=True, capture_output=True)
        self.assertEqual(result.stdout.strip(), "ok", result.stderr)

    def test_the_whole_cli_imports_without_fcntl(self):
        code = "import sys; sys.modules['fcntl'] = None; sys.path.insert(0, sys.argv[1]); import agent_memory"
        result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class LastingInstallTest(Temp):
    """install writes hooks only from the clone's bin or the agent-memory on PATH."""

    def setUp(self):
        super().setUp()
        (self.home / ".claude").mkdir()
        self.env = os.environ.copy()
        self.env.pop("AGENT_MEMORY_DIR", None)
        self.path = self.env["PATH"]

    def install(self, command, path):
        env = dict(self.env, PATH=path)
        return subprocess.run(
            [str(command), "install", "--yes"], env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL
        )

    def hook_command(self):
        settings = json.loads((self.home / ".claude" / "settings.json").read_text())
        return settings["hooks"]["Stop"][0]["hooks"][0]["command"]

    def test_a_copy_that_is_not_on_path_is_refused_and_nothing_is_written(self):
        # What uvx leaves: a console script in a cache folder, not on PATH.
        copy = console_script(self.base / "uvx-cache", make_clone(self.base / "uvx-cache"))
        before = tree(self.home)
        result = self.install(copy, self.path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("won't last", result.stderr)
        self.assertIn(agent_memory.DIST, result.stderr)
        self.assertIn("agent-memory install", result.stderr)
        self.assertEqual(tree(self.home), before)

    def test_a_copy_is_refused_when_another_agent_memory_is_on_path(self):
        copy = console_script(self.base / "uvx-cache", make_clone(self.base / "uvx-cache"))
        other = console_script(self.base / "other", make_clone(self.base / "other"))
        result = self.install(copy, f"{other.parent}{os.pathsep}{self.path}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("won't last", result.stderr)
        self.assertEqual(tree(self.home), {".claude": None})

    def test_the_tool_on_path_installs(self):
        tool = console_script(self.base / "tool", make_clone(self.base / "tool"))
        result = self.install(tool, f"{tool.parent}{os.pathsep}{self.path}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hook_command(), f"{tool} capture --tool claude-code --event stop")

    def test_the_clones_own_bin_installs_without_a_path_entry(self):
        clone = make_clone(self.base / "clone")
        result = self.install(clone / "bin" / "agent-memory", self.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hook_command(), f"{clone / 'bin' / 'agent-memory'} capture --tool claude-code --event stop")


if __name__ == "__main__":
    unittest.main()
