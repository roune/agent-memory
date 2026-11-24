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
import sysconfig
import tomllib
import unittest
import venv
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

    def venv_script(self, name):
        """A console script inside a real venv, as uvx and uv tool leave: its folder is that env's scripts folder."""
        folder = self.base / name
        venv.EnvBuilder(with_pip=False, symlinks=True).create(folder)
        clone = make_clone(self.base / name)
        script = folder / "bin" / "agent-memory"
        script.write_text(
            f"#!{folder / 'bin' / 'python'}\nimport sys\nsys.path.insert(0, {str(clone / 'scripts')!r})\n"
            "import agent_memory\nagent_memory.main()\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def test_a_uvx_run_with_its_env_first_on_path_is_refused_and_nothing_is_written(self):
        copy = self.venv_script("uvx-env")
        before = tree(self.home)
        result = self.install(copy, f"{copy.parent}{os.pathsep}{self.path}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(copy), result.stderr)
        self.assertIn(agent_memory.DIST, result.stderr)
        self.assertIn("run this command again", result.stderr)
        self.assertEqual(tree(self.home), before)

    def test_a_copy_off_path_is_refused(self):
        copy = console_script(self.base / "cache", make_clone(self.base / "cache"))
        result = self.install(copy, self.path)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(tree(self.home), {".claude": None})

    def test_the_refusal_names_the_other_agent_memory_on_path(self):
        copy = console_script(self.base / "cache", make_clone(self.base / "cache"))
        other = console_script(self.base / "other", make_clone(self.base / "other"))
        result = self.install(copy, f"{other.parent}{os.pathsep}{self.path}")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"this is {copy}; `agent-memory` on PATH is {other}", result.stderr)
        self.assertEqual(tree(self.home), {".claude": None})

    def test_a_tool_bin_link_into_a_venv_installs(self):
        target = self.venv_script("tool-env")
        link = self.base / "tool-bin" / "agent-memory"
        link.parent.mkdir()
        link.symlink_to(target)
        result = self.install(link, f"{link.parent}{os.pathsep}{self.path}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hook_command(), f"{link} capture --tool claude-code --event stop")

    def test_python_dash_m_uses_the_agent_memory_on_path(self):
        found = self.base / "bin" / "agent-memory"
        with mock.patch.object(agent_memory, "BIN", self.base / "none"), mock.patch.object(
            agent_memory, "lasting_agent_memory", return_value=str(found)
        ), mock.patch.object(sys, "argv", ["/site-packages/agent_memory.py", "install"]):
            self.assertEqual(agent_memory.hook_bin(), found)

    def test_the_running_envs_scripts_folder_is_dropped_from_path(self):
        own = sysconfig.get_path("scripts")
        with mock.patch.dict(os.environ, {"PATH": f"{own}{os.pathsep}/x/bin"}):
            self.assertEqual(agent_memory.path_without_own_env(), "/x/bin")

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


class PyPITest(unittest.TestCase):
    """PR 3: what PyPI shows, and that every install command names the distribution."""

    def test_the_long_description_is_pypi_md_and_has_no_relative_links(self):
        import re

        self.assertEqual(pyproject()["readme"], "docs/pypi.md")
        text = (ROOT / "docs" / "pypi.md").read_text()
        self.assertEqual(re.findall(r"!\[[^\]]*\]\(", text), [])
        for target in re.findall(r"\]\(([^)]+)\)", text):
            self.assertTrue(target.startswith("https://"), target)

    def test_metadata_has_urls_and_no_license_classifier(self):
        project = pyproject()
        for name in ("Homepage", "Source", "Issues", "Documentation"):
            self.assertTrue(project["urls"][name].startswith("https://github.com/roune/agent-memory"))
        self.assertTrue(project["keywords"])
        self.assertIn("Environment :: Console", project["classifiers"])
        self.assertEqual([c for c in project["classifiers"] if c.startswith("License ::")], [])

    def test_every_install_command_names_the_distribution(self):
        import re

        command = re.compile(
            r"(?:uv tool (?:install|upgrade)|pipx (?:install|upgrade)|uvx --from|mise (?:use -g|upgrade)) +'?(?:pipx:)?([^\s'`\]\[]+)"
        )
        files = [ROOT / "README.md", ROOT / "pyproject.toml", *sorted((ROOT / "docs").glob("*.md")), *sorted(SCRIPTS.glob("*.py"))]
        files += sorted((SCRIPTS / "agent_memory_server").glob("*.py"))
        # Plans and tests quote old or foreign names on purpose.
        skip = {"distribution-plan.md", "server-plan.md", "research.md"}
        for path in files:
            if path.name in skip or path.name.startswith("test_"):
                continue
            for name in command.findall(path.read_text()):
                if name.startswith(("git+", "--", ".", "$", "<", "{", '"')):
                    continue
                self.assertEqual(name, agent_memory.DIST, f"{path.name}: {name}")

    def test_the_release_workflow_uploads_after_verify_with_a_pinned_action(self):
        import re

        text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        start = text.index("\n  pypi:")
        after = re.search(r"\n  [a-z-]+:\n", text[start + 1 :])
        job = text[start : start + 1 + after.start()] if after else text[start:]
        self.assertIn("needs: [check, build, verify]", job)
        self.assertIn("environment: pypi", job)
        self.assertRegex(job, r"pypa/gh-action-pypi-publish@[0-9a-f]{40} # v\d")
        self.assertIn("id-token: write", job)
        self.assertEqual(text.count("id-token: write"), 1)
        self.assertIn("twine check --strict", (ROOT / ".github" / "workflows" / "test.yml").read_text())


if __name__ == "__main__":
    unittest.main()
