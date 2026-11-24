"""PR 7: the state in the XDG data directory, the one-time move, hooks that run
the installed command, and scripts/install.

Every test has a temporary HOME and XDG_DATA_HOME. A test that leaves
AGENT_MEMORY_DIR unset runs a copy of the CLI from a temporary clone, never
from this checkout, so it can't move a real inbox.
"""

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import local_state  # noqa: E402

MARKER = "agent-memory-hook"


def tree(root):
    """Every path under root with its bytes (None for a directory), for before/after checks."""
    root = Path(root)
    if not root.exists():
        return {}
    found = {}
    for path in sorted(root.rglob("*")):
        found[str(path.relative_to(root))] = None if path.is_dir() else path.read_bytes()
    return found


def old_layout(root):
    """State as a clone held it before the move."""
    root = Path(root)
    (root / "inbox" / "claude-code" / "s1").mkdir(parents=True)
    (root / "inbox" / "claude-code" / "s1" / "bundle.json").write_text('{"session_id": "s1"}\n')
    (root / "inbox" / "git" / "g1").mkdir(parents=True)
    (root / "inbox" / "git" / "g1" / "bundle.json").write_text('{"session_id": "g1"}\n')
    (root / "outbox" / "failed").mkdir(parents=True)
    item = root / "outbox" / "20261122T100000Z-abc.json"
    item.write_text('{"op": "memory"}\n')
    item.chmod(0o600)
    (root / "outbox" / ".lock").write_text("")
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "review.json").write_text('{"pid": 1, "port": 4173}\n')
    (root / "data" / "capture.log").write_text("old line\n")


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.xdg = self.base / "xdg"
        env = {"HOME": str(self.home), "XDG_DATA_HOME": str(self.xdg)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("AGENT_MEMORY_DIR", None)


class RootTest(Temp):
    def test_the_xdg_data_directory_is_the_default(self):
        self.assertEqual(local_state.root(), self.xdg / "agent-memory")

    def test_without_xdg_data_home_it_is_under_local_share(self):
        os.environ.pop("XDG_DATA_HOME")
        self.assertEqual(local_state.root(), self.home / ".local" / "share" / "agent-memory")

    def test_a_relative_xdg_data_home_is_ignored(self):
        os.environ["XDG_DATA_HOME"] = "relative/dir"
        self.assertEqual(local_state.root(), self.home / ".local" / "share" / "agent-memory")

    def test_the_default_root_is_created_private(self):
        local_state.ensure_root()
        self.assertEqual((self.xdg / "agent-memory").stat().st_mode & 0o777, 0o700)

    def test_agent_memory_dir_is_not_created(self):
        os.environ["AGENT_MEMORY_DIR"] = str(self.base / "elsewhere")
        local_state.ensure_root()
        self.assertFalse((self.base / "elsewhere").exists())
        self.assertFalse(self.xdg.exists())

    def test_agent_memory_dir_overrides_it(self):
        os.environ["AGENT_MEMORY_DIR"] = str(self.base / "elsewhere")
        self.assertEqual(local_state.root(), self.base / "elsewhere")


class MoveTest(Temp):
    def setUp(self):
        super().setUp()
        self.old = self.base / "clone"
        self.new = self.xdg / "agent-memory"
        old_layout(self.old)
        (self.old / "data" / "qdrant").mkdir()

    def test_the_old_layout_ends_up_in_the_new_one(self):
        moved, left = local_state.move(self.old, self.new)
        self.assertEqual(left, [])
        self.assertEqual(
            {(str(a.relative_to(self.old)), str(b.relative_to(self.new))) for a, b in moved},
            {("inbox", "inbox"), ("outbox", "outbox"), ("data/review.json", "review.json"), ("data/capture.log", "capture.log")},
        )
        self.assertEqual((self.new / "inbox" / "git" / "g1" / "bundle.json").read_text(), '{"session_id": "g1"}\n')
        self.assertEqual((self.new / "capture.log").read_text(), "old line\n")
        self.assertTrue((self.new / "review.json").is_file())
        self.assertEqual((self.new / "outbox" / "20261122T100000Z-abc.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.new.stat().st_mode & 0o777, 0o700)
        for old in ("inbox", "outbox", "data/review.json", "data/capture.log"):
            self.assertFalse((self.old / old).exists(), old)
        # The rest of the clone's data/ (Qdrant, the server) stays.
        self.assertTrue((self.old / "data" / "qdrant").is_dir())

    def test_a_second_run_does_nothing(self):
        local_state.move(self.old, self.new)
        before, clone = tree(self.new), tree(self.old)
        self.assertEqual(local_state.move(self.old, self.new), ([], []))
        self.assertEqual(tree(self.new), before)
        self.assertEqual(tree(self.old), clone)

    def test_nothing_to_move_creates_nothing(self):
        empty = self.base / "empty-clone"
        empty.mkdir()
        self.assertEqual(local_state.move(empty, self.base / "fresh"), ([], []))
        self.assertFalse((self.base / "fresh").exists())

    def test_it_merges_into_a_new_inbox_and_never_overwrites(self):
        (self.new / "inbox" / "claude-code" / "s1").mkdir(parents=True)
        (self.new / "inbox" / "claude-code" / "s1" / "bundle.json").write_text('{"newer": true}\n')
        (self.new / "inbox" / "cursor" / "c1").mkdir(parents=True)
        (self.new / "outbox").mkdir()
        (self.new / "outbox" / ".lock").write_text("")
        (self.new / "capture.log").write_text("new line\n")
        (self.new / "review.json").write_text('{"pid": 2, "port": 4174}\n')

        moved, left = local_state.move(self.old, self.new)

        self.assertEqual(
            [(str(path.relative_to(self.old)), reason.startswith(str(self.new))) for path, reason in left],
            [("inbox/claude-code/s1/bundle.json", True)],
        )
        self.assertEqual((self.new / "inbox" / "claude-code" / "s1" / "bundle.json").read_text(), '{"newer": true}\n')
        self.assertEqual((self.old / "inbox" / "claude-code" / "s1" / "bundle.json").read_text(), '{"session_id": "s1"}\n')
        self.assertTrue((self.new / "inbox" / "git" / "g1" / "bundle.json").is_file())
        self.assertTrue((self.new / "inbox" / "cursor" / "c1").is_dir())
        self.assertTrue((self.new / "outbox" / "20261122T100000Z-abc.json").is_file())
        self.assertFalse((self.old / "outbox").exists(), "the empty .lock clash is dropped")
        self.assertEqual((self.new / "capture.log").read_text(), "new line\nold line\n")
        self.assertEqual((self.new / "review.json").read_text(), '{"pid": 2, "port": 4174}\n')
        self.assertFalse((self.old / "data" / "review.json").exists())

    def test_across_file_systems_it_copies_and_then_deletes(self):
        real = os.rename

        def rename(source, target):
            if str(source).startswith(str(self.old)):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(source, target)

        with mock.patch.object(local_state.os, "rename", rename):
            moved, left = local_state.move(self.old, self.new)
        self.assertEqual(left, [])
        self.assertEqual(len(moved), 4)
        self.assertEqual((self.new / "inbox" / "claude-code" / "s1" / "bundle.json").read_text(), '{"session_id": "s1"}\n')
        self.assertEqual((self.new / "outbox" / "20261122T100000Z-abc.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.old / "inbox").exists())

    def test_a_cross_device_copy_cut_short_leaves_no_partial_file(self):
        real_rename = os.rename

        def rename(source, target):
            if str(source).startswith(str(self.old)):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(source, target)

        def killed(handle, out, *args):
            out.write(handle.read(3))
            raise KeyboardInterrupt("the hook's timeout")

        bundle = self.old / "inbox" / "claude-code" / "s1" / "bundle.json"
        with mock.patch.object(local_state.os, "rename", rename), mock.patch.object(local_state.shutil, "copyfileobj", killed):
            with self.assertRaises(KeyboardInterrupt):
                local_state.move(self.old, self.new)
        self.assertEqual(bundle.read_text(), '{"session_id": "s1"}\n', "the old file is whole")
        self.assertEqual(list((self.new / "inbox").rglob("bundle.json")), [], "no partial file at a final name")

        with mock.patch.object(local_state.os, "rename", rename):
            moved, left = local_state.move(self.old, self.new)
        self.assertEqual(left, [])
        self.assertEqual((self.new / "inbox" / "claude-code" / "s1" / "bundle.json").read_text(), '{"session_id": "s1"}\n')
        self.assertEqual((self.new / "inbox" / "git" / "g1" / "bundle.json").read_text(), '{"session_id": "g1"}\n')
        self.assertFalse((self.old / "inbox").exists())

    def test_the_old_root_is_recorded_once(self):
        local_state.move(self.old, self.new)
        old_layout(self.old)
        local_state.move(self.old, self.new)
        self.assertEqual(local_state.recorded(self.new), [self.old])
        self.assertEqual(local_state.move(self.base / "nothing-here", self.new), ([], []))
        self.assertEqual(local_state.recorded(self.new), [self.old])

    def test_in_place_under_agent_memory_dir(self):
        moved, left = local_state.move(self.old, self.old)
        self.assertEqual(left, [])
        self.assertEqual({str(b.relative_to(self.old)) for _, b in moved}, {"review.json", "capture.log"})
        self.assertTrue((self.old / "inbox" / "git" / "g1" / "bundle.json").is_file())
        self.assertEqual((self.old / "capture.log").read_text(), "old line\n")


def make_clone(base):
    """A throwaway clone: bin/agent-memory and the CLI's modules, without the tests."""
    clone = Path(base) / "clone"
    (clone / "bin").mkdir(parents=True)
    shutil.copy2(ROOT / "bin" / "agent-memory", clone / "bin" / "agent-memory")
    (clone / "scripts").mkdir()
    for path in SCRIPTS.glob("*.py"):
        if not path.name.startswith("test_"):
            shutil.copy2(path, clone / "scripts" / path.name)
    return clone


def console_script(base, clone):
    """What `uv tool install` puts on PATH: a script named agent-memory that runs main()."""
    folder = Path(base) / "tool-bin"
    folder.mkdir(exist_ok=True)
    script = folder / "agent-memory"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(clone / 'scripts')!r})\n"
        "import agent_memory\n"
        "agent_memory.main()\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


class CliTemp(Temp):
    def setUp(self):
        super().setUp()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = os.environ.copy()
        self.env.pop("AGENT_MEMORY_DIR", None)

    def run_cli(self, command, *args, env=None):
        return subprocess.run(
            [str(command), *args], env=env or self.env, text=True, capture_output=True, stdin=subprocess.DEVNULL
        )


class FirstRunTest(CliTemp):
    """The move happens on the first command after the upgrade, and only once."""

    def test_an_inbox_in_the_clone_moves_on_the_first_run_and_the_second_does_nothing(self):
        clone = make_clone(self.base)
        old_layout(clone)
        new = self.xdg / "agent-memory"

        first = self.run_cli(clone / "bin" / "agent-memory", "config", str(self.repo))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn(f"moved {clone / 'inbox'} to {new / 'inbox'}", first.stderr)
        self.assertTrue((new / "inbox" / "claude-code" / "s1" / "bundle.json").is_file())
        self.assertTrue((new / "outbox" / "20261122T100000Z-abc.json").is_file())
        self.assertFalse((clone / "inbox").exists())
        self.assertIn("old line", (new / "capture.log").read_text())

        before = tree(new)
        second = self.run_cli(clone / "bin" / "agent-memory", "config", str(self.repo))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("moved", second.stderr)
        self.assertEqual(tree(new), before)

    def test_a_hook_in_a_fresh_install_creates_a_private_state_folder(self):
        clone = make_clone(self.base)
        result = self.run_cli(clone / "bin" / "agent-memory", "config", str(self.repo))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual((self.xdg / "agent-memory").stat().st_mode & 0o777, 0o700)

    def test_help_moves_nothing(self):
        clone = make_clone(self.base)
        old_layout(clone)
        result = self.run_cli(clone / "bin" / "agent-memory", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((clone / "inbox").is_dir())
        self.assertFalse(self.xdg.exists())

    def test_with_agent_memory_dir_the_clone_is_left_alone(self):
        clone = make_clone(self.base)
        old_layout(clone)
        state = self.base / "state"
        old_layout(state)
        env = dict(self.env, AGENT_MEMORY_DIR=str(state))
        result = self.run_cli(clone / "bin" / "agent-memory", "config", str(self.repo), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((clone / "inbox").is_dir())
        self.assertTrue((clone / "data" / "capture.log").is_file())
        self.assertTrue((state / "capture.log").is_file())
        self.assertFalse((state / "data" / "capture.log").exists())
        self.assertFalse(self.xdg.exists())


class InstallTest(CliTemp):
    """install points the hooks it marked at the installed command, and leaves the rest."""

    def setUp(self):
        super().setUp()
        self.old_clone = make_clone(self.base / "old")
        self.old_bin = self.old_clone / "bin" / "agent-memory"
        self.tool = console_script(self.base, make_clone(self.base / "installed"))
        # install writes hooks only from the agent-memory that is on PATH.
        self.env["PATH"] = f"{self.tool.parent}{os.pathsep}{self.env['PATH']}"
        for name in (".claude", ".codex", ".cursor", ".pi/agent"):
            (self.home / name).mkdir(parents=True)

    def write(self, path, value):
        path = self.home / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) if not isinstance(value, str) else value, encoding="utf-8")
        return path

    def old_hooks(self):
        """Hooks as an older install wrote them, pointing at the old clone, next to other people's hooks."""
        old = f"{self.old_bin} capture --tool claude-code --event stop"
        self.write(".claude/settings.json", {
            "model": "x",
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "echo before"}]},
                    {"hooks": [{"type": "command", "command": old, "timeout": 30}, {"type": "command", "command": "echo same group"}]},
                    {"matcher": "x", "hooks": [{"type": "command", "command": "/usr/local/bin/agent-memory-notify --tool x"}]},
                ],
                "SessionEnd": [{"hooks": [{"type": "command", "command": f"{self.old_bin} capture --tool claude-code --event session-end"}]}],
            },
        })
        self.write(".codex/hooks.json", {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": f"{self.old_bin} capture --tool codex --event stop"}]},
                    {"hooks": [{"type": "command", "command": "echo codex"}]},
                ],
            },
        })
        self.write(".cursor/hooks.json", {"version": 1, "hooks": {"stop": [{"command": "./hooks/other.sh"}, {"command": "./hooks/agent-memory.sh"}]}})
        self.write(".cursor/hooks/agent-memory.sh", f"#!/bin/sh\n# {MARKER}\nexec {self.old_bin} capture --tool cursor\n")
        self.write(".pi/agent/extensions/agent-memory/index.ts", f'// {MARKER}\nconst child = spawn("{self.old_bin}", ["capture"]);\n')

    def install(self, *extra):
        return self.run_cli(self.tool, "install", "--yes", *extra)

    def test_marked_entries_point_at_the_installed_command_and_the_rest_stay(self):
        self.old_hooks()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn(f"They run {self.tool}.", result.stdout)

        claude = json.loads((self.home / ".claude/settings.json").read_text())
        self.assertEqual(claude["model"], "x")
        stop = claude["hooks"]["Stop"]
        self.assertEqual(stop[0], {"hooks": [{"type": "command", "command": "echo before"}]})
        self.assertEqual(stop[1]["hooks"], [
            {"type": "command", "command": f"{self.tool} capture --tool claude-code --event stop", "timeout": 30},
            {"type": "command", "command": "echo same group"},
        ])
        self.assertEqual(stop[2], {"matcher": "x", "hooks": [{"type": "command", "command": "/usr/local/bin/agent-memory-notify --tool x"}]})
        self.assertEqual(len(stop), 3, "rewritten in place, not added again")
        self.assertEqual(claude["hooks"]["SessionEnd"], [{"hooks": [{"type": "command", "command": f"{self.tool} capture --tool claude-code --event session-end"}]}])

        codex = json.loads((self.home / ".codex/hooks.json").read_text())["hooks"]
        self.assertEqual(codex["Stop"], [
            {"hooks": [{"type": "command", "command": f"{self.tool} capture --tool codex --event stop"}]},
            {"hooks": [{"type": "command", "command": "echo codex"}]},
        ])
        self.assertEqual(codex["SessionEnd"], [{"hooks": [{"type": "command", "command": f"{self.tool} capture --tool codex --event session-end"}]}])

        cursor = json.loads((self.home / ".cursor/hooks.json").read_text())["hooks"]
        self.assertEqual(cursor["stop"], [{"command": "./hooks/other.sh"}, {"command": "./hooks/agent-memory.sh"}])
        self.assertEqual(cursor["sessionEnd"], [{"command": "./hooks/agent-memory.sh"}])
        self.assertIn(f"exec {self.tool} capture --tool cursor", (self.home / ".cursor/hooks/agent-memory.sh").read_text())
        self.assertIn(f'spawn("{self.tool}"', (self.home / ".pi/agent/extensions/agent-memory/index.ts").read_text())

        again = self.install()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads((self.home / ".claude/settings.json").read_text()), claude)

    def test_a_file_at_our_path_without_the_marker_is_refused_and_nothing_is_written(self):
        self.old_hooks()
        script = self.write(".cursor/hooks/agent-memory.sh", "#!/bin/sh\necho mine\n")
        claude_before = (self.home / ".claude/settings.json").read_text()
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("was not written by agent-memory", result.stderr)
        self.assertEqual(script.read_text(), "#!/bin/sh\necho mine\n")
        self.assertEqual((self.home / ".claude/settings.json").read_text(), claude_before)

    def test_install_moves_the_state_of_the_clone_the_hooks_ran(self):
        self.old_hooks()
        old_layout(self.old_clone)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        new = self.xdg / "agent-memory"
        self.assertIn(f"moved {self.old_clone / 'inbox'} to {new / 'inbox'}", result.stderr)
        self.assertTrue((new / "inbox" / "git" / "g1" / "bundle.json").is_file())
        self.assertFalse((self.old_clone / "inbox").exists())

    def test_install_refused_without_a_terminal_moves_nothing(self):
        self.old_hooks()
        old_layout(self.old_clone)
        result = subprocess.run(
            [str(self.tool), "install"], env=self.env, text=True, capture_output=True, input="n\n"
        )
        # No terminal: install refuses before it asks, and nothing moves.
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.old_clone / "inbox").is_dir())

    def test_bundles_an_old_clone_files_later_are_moved_too(self):
        # A git hook in a repo not set up again still runs the old clone, which
        # files into its own inbox. The clone was recorded, so the next command moves it.
        self.old_hooks()
        old_layout(self.old_clone)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        # The old clone has no git hooks, so nothing is said about them.
        self.assertNotIn("still run", result.stdout)
        late = self.old_clone / "inbox" / "git" / "late" / "bundle.json"
        late.parent.mkdir(parents=True)
        late.write_text('{"session_id": "late"}\n')
        (self.old_clone / "outbox").mkdir()
        (self.old_clone / "outbox" / "20261123T090000Z-late.json").write_text('{"op": "memory"}\n')

        again = self.run_cli(self.tool, "config", str(self.repo))
        self.assertEqual(again.returncode, 0, again.stderr)
        new = self.xdg / "agent-memory"
        self.assertTrue((new / "inbox" / "git" / "late" / "bundle.json").is_file())
        self.assertTrue((new / "outbox" / "20261123T090000Z-late.json").is_file())
        self.assertFalse((self.old_clone / "inbox").exists())

    def test_git_hooks_run_the_installed_command(self):
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "https://github.com/example/p.git"], check=True)
        result = self.install("--git-repo", str(self.repo), "--install-git-hooks", "true")
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = (self.repo / ".git" / "hooks" / "post-commit").read_text()
        self.assertIn(f"'{self.tool}' git-hook --event post-commit", hook)


class InstallScriptTest(Temp):
    """scripts/install runs `uv tool install --editable` on the clone, after removing the old link to it."""

    def setUp(self):
        super().setUp()
        self.stub = self.base / "stub-bin"
        self.stub.mkdir()
        self.calls = self.base / "uv-calls"
        self.bin_dir = self.home / ".local" / "bin"
        uv = self.stub / "uv"
        uv.write_text(
            "#!/bin/sh\n"
            f'if [ "$1 $2 $3" = "tool dir --bin" ]; then echo {self.bin_dir}; exit 0; fi\n'
            f'echo "$@" >> {self.calls}\n',
            encoding="utf-8",
        )
        uv.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{self.stub}{os.pathsep}{os.environ['PATH']}")

    def run_install(self, *args):
        return subprocess.run(
            [str(ROOT / "scripts" / "install"), *args], env=self.env, text=True, capture_output=True
        )

    def test_it_runs_an_editable_tool_install_of_the_clone(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().split("\n")[0], f"tool install --editable {ROOT}")
        self.assertIn("agent-memory install", result.stdout)

    def test_it_removes_the_old_link_to_the_clone_first(self):
        self.bin_dir.mkdir(parents=True)
        link = self.bin_dir / "agent-memory"
        link.symlink_to(ROOT / "bin" / "agent-memory")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.is_symlink())
        self.assertIn("Removed the old link", result.stdout)

    def test_it_leaves_another_agent_memory_alone(self):
        self.bin_dir.mkdir(parents=True)
        other = self.bin_dir / "agent-memory"
        other.write_text("#!/bin/sh\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(other.read_text(), "#!/bin/sh\n")

    def test_without_uv_it_says_how_to_get_it(self):
        bare = self.base / "bare"
        bare.mkdir()
        for name in ("sh", "dirname", "readlink", "pwd"):
            found = shutil.which(name)
            if found:
                (bare / name).symlink_to(found)
        env = dict(self.env, PATH=str(bare))
        result = subprocess.run([str(ROOT / "scripts" / "install")], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs uv", result.stderr)


class ReleaseCheckTest(Temp):
    """scripts/release-check: the tag, pyproject.toml and the compose files, packaged copy included, name one version."""

    def setUp(self):
        super().setUp()
        self.copy = self.base / "repo-copy"
        (self.copy / "scripts").mkdir(parents=True)
        (self.copy / "scripts" / "agent_memory_assets").mkdir()
        # __init__.py too: release-check reads the pin's pattern from it.
        assets = ("scripts/agent_memory_assets/__init__.py", "scripts/agent_memory_assets/compose.yaml")
        for name in ("pyproject.toml", "compose.yaml", "compose.team.yaml", *assets):
            shutil.copy2(ROOT / name, self.copy / name)
        shutil.copy2(ROOT / "scripts" / "release-check", self.copy / "scripts" / "release-check")

    def check(self, *args, root=None):
        script = (root or self.copy) / "scripts" / "release-check"
        return subprocess.run([sys.executable, str(script), *args], text=True, capture_output=True)

    def version(self):
        return self.check("--version").stdout.strip()

    def test_this_checkout_is_consistent(self):
        version = self.check("--version", root=ROOT).stdout.strip()
        result = self.check(f"v{version}", root=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), version)

    def test_a_tag_for_another_version_is_refused(self):
        result = self.check("v99.0.0")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"pyproject.toml says version {self.version()}", result.stderr)

    def test_a_compose_file_left_at_the_old_version_is_refused(self):
        path = self.copy / "compose.team.yaml"
        path.write_text(path.read_text().replace(f"agent-memory-server:{self.version()}", "agent-memory-server:0.0.9"))
        result = self.check(f"v{self.version()}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("compose.team.yaml pins ghcr.io/roune/agent-memory-server at 0.0.9", result.stderr)
        self.assertNotIn("compose.yaml pins", result.stderr)

    def test_a_compose_file_without_the_pin_is_refused(self):
        path = self.copy / "compose.yaml"
        path.write_text(path.read_text().replace("${AGENT_MEMORY_IMAGE:-ghcr.io/roune/agent-memory-server:", "agent-memory-server:dev #"))
        result = self.check(f"v{self.version()}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("compose.yaml pins ghcr.io/roune/agent-memory-server at none", result.stderr)

    def test_no_tag_is_a_usage_error(self):
        self.assertEqual(self.check().returncode, 2)

    def test_a_packaged_copy_that_differs_is_refused(self):
        path = self.copy / "scripts" / "agent_memory_assets" / "compose.yaml"
        path.write_text(path.read_text() + "# edited\n")
        result = self.check(f"v{self.version()}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("scripts/agent_memory_assets/compose.yaml differs from compose.yaml", result.stderr)
        self.assertNotIn("pins", result.stderr)

    def test_a_packaged_copy_pinning_another_version_is_refused(self):
        # The copy differs from the root file here too, so both messages appear.
        path = self.copy / "scripts" / "agent_memory_assets" / "compose.yaml"
        path.write_text(path.read_text().replace(f"agent-memory-server:{self.version()}", "agent-memory-server:0.0.9"))
        result = self.check(f"v{self.version()}")
        self.assertEqual(result.returncode, 1)
        self.assertIn("scripts/agent_memory_assets/compose.yaml pins ghcr.io/roune/agent-memory-server at 0.0.9", result.stderr)
        self.assertIn("differs from compose.yaml", result.stderr)


class PackagedComposeTest(unittest.TestCase):
    def test_the_packaged_copy_is_the_root_file(self):
        packaged = ROOT / "scripts" / "agent_memory_assets" / "compose.yaml"
        self.assertEqual(packaged.read_bytes(), (ROOT / "compose.yaml").read_bytes())

    def test_compose_text_reads_it_through_importlib_resources(self):
        import agent_memory_assets

        self.assertEqual(agent_memory_assets.compose_bytes(), (ROOT / "compose.yaml").read_bytes())
        self.assertEqual(agent_memory_assets.compose_text(), (ROOT / "compose.yaml").read_text(encoding="utf-8"))

    def test_every_package_in_pyproject_is_allowed_into_the_docker_build(self):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            packages = tomllib.load(handle)["tool"]["setuptools"]["packages"]
        lines = (ROOT / ".dockerignore").read_text().splitlines()
        for package in packages:
            self.assertIn(f"!scripts/{package}/", lines)


class ComposeFilesTest(unittest.TestCase):
    def service(self, name, service):
        text = (ROOT / name).read_text()
        return text.split(f"\n  {service}:\n", 1)[1].split("\n  qdrant:\n")[0]

    def test_compose_yaml_pins_the_image_and_keeps_build_for_a_clone(self):
        server = self.service("compose.yaml", "server")
        self.assertIn("image: ${AGENT_MEMORY_IMAGE:-ghcr.io/roune/agent-memory-server:", server)
        self.assertIn("\n    build: .\n", server)
        self.assertIn('AGENT_MEMORY_AUTH: "off"', server)
        self.assertIn('- "127.0.0.1:8765:8765"', server)

    def test_compose_team_yaml_has_auth_on_and_needs_the_team_hosts(self):
        text = (ROOT / "compose.team.yaml").read_text()
        server = self.service("compose.team.yaml", "server")
        self.assertNotIn("AGENT_MEMORY_AUTH:", text)
        self.assertNotRegex(text, r"(?m)^\s+build:")
        self.assertIn('AGENT_MEMORY_ALLOWED_HOSTS: "127.0.0.1:8765,${AGENT_MEMORY_ALLOWED_HOSTS:?', server)
        self.assertIn('- "${AGENT_MEMORY_BIND:-127.0.0.1}:8765:8765"', server)
        qdrant = text.split("\n  qdrant:\n", 1)[1].split("\n  server:\n")[0]
        self.assertNotIn("ports:", qdrant)


if __name__ == "__main__":
    unittest.main()
