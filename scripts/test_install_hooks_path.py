#!/usr/bin/env python3
"""install, config and uninstall: where the git hooks go, and what install says about them.

Standard library only. Every test has a temporary HOME and scratch git repos,
so the real ~/.claude, ~/.cursor, ~/.codex and Pi are never read or written.
A Cursor-managed hooks folder is faked in the temp dir: a .dispatcher marked
@cursor-managed that runs the original hooks path and then <hook>.cursor*
files, hook names linked to it, and .cursor-original-hooks-path.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory

DISPATCHER = """#!/bin/sh
# @cursor-managed
# test stand-in for Cursor's dispatcher
set -e
HOOKS_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_NAME="$(basename "$0")"
ORIGINAL=""
if [ -f "$HOOKS_DIR/.cursor-original-hooks-path" ]; then
  ORIGINAL="$(cat "$HOOKS_DIR/.cursor-original-hooks-path")"
fi
if [ -n "$ORIGINAL" ] && [ -x "$ORIGINAL/$HOOK_NAME" ]; then
  "$ORIGINAL/$HOOK_NAME" "$@"
fi
for hook in "$HOOKS_DIR"/$HOOK_NAME.cursor*; do
  if [ -x "$hook" ]; then "$hook" "$@"; fi
done
"""
CO_AUTHOR = "#!/bin/sh\n# @cursor-managed\necho cursor-co-author >> \"$CURSOR_LOG\"\n"


def git(repo, *args, env=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True, capture_output=True, env=env)


def tree(folder):
    """Every entry in folder: a link's target, or a file's bytes."""
    found = {}
    for path in sorted(Path(folder).iterdir()):
        found[path.name] = ("link", os.readlink(path)) if path.is_symlink() else ("file", path.read_bytes())
    return found


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))
        self.home = self.base / "home"
        self.home.mkdir()
        self.log = self.base / "ran.log"
        for patcher in (
            mock.patch.dict(os.environ, {
                "HOME": str(self.home),
                "AGENT_MEMORY_DIR": str(self.base / "state"),
                "GIT_CONFIG_GLOBAL": str(self.base / "gitconfig"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "CURSOR_LOG": str(self.log),
            }),
            mock.patch.object(agent_memory, "HOME", self.home),
            mock.patch.object(agent_memory, "present_tools", lambda: list(self.tools)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tools = []
        self.tool = self.command(self.base / "tool-bin" / "agent-memory", "new")
        self.use(self.tool)

    def command(self, path, label):
        """A stand-in agent-memory: it logs that it ran, and nothing else."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho "{label} $*" >> "$CURSOR_LOG"\n', encoding="utf-8")
        path.chmod(0o755)
        return path

    def use(self, command):
        patcher = mock.patch.object(agent_memory, "hook_bin", lambda: command)
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, name="repo"):
        repo = self.base / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.email", "t@example.invalid")
        git(repo, "config", "user.name", "T")
        return repo

    def cursor_folder(self, repo, original=None):
        """A Cursor-managed hooks folder for repo, set as its core.hooksPath. original=None records .git/hooks."""
        folder = self.home / ".cursor" / "agent-hooks" / "L1Rlc3Q"
        folder.mkdir(parents=True)
        (folder / ".dispatcher").write_text(DISPATCHER, encoding="utf-8")
        (folder / ".dispatcher").chmod(0o755)
        os.symlink(".dispatcher", folder / "commit-msg")
        (folder / "commit-msg.cursor.co-author").write_text(CO_AUTHOR, encoding="utf-8")
        (folder / "commit-msg.cursor.co-author").chmod(0o755)
        recorded = str(repo / ".git" / "hooks") if original is None else original
        (folder / ".cursor-original-hooks-path").write_text(recorded, encoding="utf-8")
        git(repo, "config", "core.hooksPath", str(folder))
        return folder

    def run_cli(self, *argv, answer=None):
        """Run agent-memory in-process. Returns (exit code, stdout)."""
        args = agent_memory.build_parser().parse_args(list(argv))
        out = io.StringIO()
        code = 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            if answer is not None:
                stack.enter_context(mock.patch("sys.stdin.isatty", return_value=True))
                stack.enter_context(mock.patch("builtins.input", return_value=answer))
            try:
                result = args.func(args)
                code = 0 if result is None else result
            except SystemExit as error:
                code = error.code
                if isinstance(code, str):
                    out.write(code + "\n")
                    code = 1
        return code, out.getvalue()

    def install_git(self, repo, *extra):
        return self.run_cli("install", "--yes", *extra, "--git-repo", str(repo), "--install-git-hooks", "true")

    def commit(self, repo, message="change"):
        (repo / "file.txt").write_text(message, encoding="utf-8")
        git(repo, "add", "file.txt")
        git(repo, "commit", "-q", "-m", message)

    def ran(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


class CursorFolderTest(Case):
    """Finding 21 / 14: core.hooksPath is a folder Cursor manages."""

    def test_scripts_go_to_the_original_path_and_cursor_links_them(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        cursor_before = tree(folder)

        code, out = self.install_git(repo)
        self.assertEqual(code, 0, out)
        self.assertIn("which Cursor manages", out)

        hooks = repo / ".git" / "hooks"
        for name in ("post-commit", "post-merge"):
            self.assertIn(f"'{self.tool}' git-hook --event {name}", (hooks / name).read_text())
            self.assertTrue((folder / name).is_symlink())
            self.assertEqual(os.readlink(folder / name), ".dispatcher")
        # Cursor's own files are as they were; only our two links were added.
        after = tree(folder)
        self.assertEqual({k: v for k, v in after.items() if k not in ("post-commit", "post-merge")}, cursor_before)
        self.assertEqual(agent_memory.git_hooks_installed(str(repo)), True)

        self.commit(repo)
        self.assertIn("new git-hook --event post-commit", self.ran())
        self.assertIn("cursor-co-author", self.ran(), "Cursor's own hook still runs")

    def test_an_older_install_in_cursors_folder_moves_out_of_it(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        # What install wrote before this fix: plain files in Cursor's folder.
        with mock.patch.object(agent_memory, "git_hook_layout", lambda r: (folder, None)), \
                contextlib.redirect_stdout(io.StringIO()):
            agent_memory.write_git_hooks(str(repo), self.tool)
        self.assertFalse((folder / "post-commit").is_symlink())
        self.assertTrue(agent_memory.git_hooks_installed(str(repo)), "an older install still counts as installed")

        code, out = self.install_git(repo)
        self.assertEqual(code, 0, out)
        for name in ("post-commit", "post-merge"):
            self.assertEqual(os.readlink(folder / name), ".dispatcher")
            self.assertIsNotNone(agent_memory.git_hook_command(repo / ".git" / "hooks" / name))
        self.commit(repo)
        self.assertEqual(self.ran().count("new git-hook --event post-commit"), 1)

    def test_a_missing_link_reports_not_installed(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        self.assertEqual(self.install_git(repo)[0], 0)
        (folder / "post-commit").unlink()  # Cursor rebuilt its folder without it
        self.assertFalse(agent_memory.git_hooks_installed(str(repo)))
        code, out = self.run_cli("config", str(repo))
        self.assertIn("git_hooks (installed): False", out)

    def test_uninstall_removes_exactly_what_install_wrote(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        # Cursor already links post-merge itself, and the repo has its own post-commit.
        os.symlink(".dispatcher", folder / "post-merge")
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        (hooks / "post-commit").write_text("#!/bin/sh\necho user-hook >> \"$CURSOR_LOG\"\n", encoding="utf-8")
        (hooks / "post-commit").chmod(0o755)
        cursor_before = tree(folder)
        hooks_before = tree(hooks)

        self.assertEqual(self.install_git(repo)[0], 0)
        self.commit(repo)
        self.assertIn("user-hook", self.ran(), "the repo's own hook runs after ours")

        code, out = self.run_cli("uninstall")
        self.assertEqual(code, 0, out)
        self.assertIn(f"removed agent-memory git hooks (post-commit, post-merge) for {repo}", out)
        self.assertEqual(tree(folder), cursor_before, "Cursor's post-merge link stays; ours is gone")
        self.assertEqual(tree(hooks), hooks_before)
        self.assertFalse(agent_memory.git_hooks_record_path().exists())

    def test_config_false_removes_the_links_too(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        before = tree(folder)
        self.assertEqual(self.install_git(repo)[0], 0)
        code, out = self.run_cli("config", str(repo), "--git-hooks", "false")
        self.assertEqual(code, 0, out)
        self.assertEqual(tree(folder), before)
        self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())

    def test_config_false_from_a_subfolder_or_worktree_removes_the_links(self):
        repo = self.repo()
        self.commit(repo, "init")
        folder = self.cursor_folder(repo)
        before = tree(folder)
        (repo / "sub").mkdir()
        worktree = self.base / "wt"
        git(repo, "worktree", "add", "-q", "-b", "wt", str(worktree))
        for where in (repo / "sub", worktree):
            self.assertEqual(self.install_git(repo)[0], 0)
            code, out = self.run_cli("config", str(where), "--git-hooks", "false")
            self.assertEqual(code, 0, out)
            self.assertEqual(tree(folder), before, where)
            self.assertFalse(agent_memory.git_hooks_record_path().exists(), where)

    def test_a_broken_record_does_not_stop_uninstall(self):
        repo = self.repo()
        self.cursor_folder(repo)
        self.assertEqual(self.install_git(repo)[0], 0)
        agent_memory.git_hooks_record_path().write_text("{not json", encoding="utf-8")
        with mock.patch.object(agent_memory.editors, "remove_all", return_value=[]) as remove_all:
            code, out = self.run_cli("uninstall")
        self.assertEqual(code, 0, out)
        self.assertIn("git hooks left in place", out)
        remove_all.assert_called_once()

    def older_install(self, repo, folder, command=None):
        """What install wrote before this fix: plain files in Cursor's folder, with no record."""
        with mock.patch.object(agent_memory, "git_hook_layout", lambda r: (folder, None)), \
                mock.patch.object(agent_memory, "read_git_hooks_record", lambda: {}), \
                mock.patch.object(agent_memory, "write_git_hooks_record", lambda record: None), \
                contextlib.redirect_stdout(io.StringIO()):
            agent_memory.write_git_hooks(str(repo), command or self.tool)

    def test_cursors_link_an_older_install_renamed_is_given_back(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        os.symlink(".dispatcher", folder / "post-commit")  # Cursor's own link
        self.older_install(repo, folder)
        self.assertTrue((folder / "post-commit.agent-memory-user").is_symlink())

        code, out = self.install_git(repo)
        self.assertEqual(code, 0, out)
        self.assertEqual(os.readlink(folder / "post-commit"), ".dispatcher")
        self.assertFalse(os.path.lexists(folder / "post-commit.agent-memory-user"))
        self.assertNotIn(str(folder / "post-commit"), json.loads(agent_memory.git_hooks_record_path().read_text())[str(repo)]["links"])
        self.commit(repo)
        self.assertEqual(self.ran().count("new git-hook --event post-commit"), 1)

    def test_plain_install_moves_an_older_install_out_of_cursors_folder(self):
        # Same command as now, no record: only the clone or config can find it.
        repo = self.repo()
        folder = self.cursor_folder(repo)
        self.older_install(repo, folder)
        code, out = self.run_cli("config", str(repo))
        self.assertIn(f"The git hooks are files in {folder}", out)
        self.assertIn(f"agent-memory config {repo} --git-hooks true", out)
        self.assertEqual(self.run_cli("config", str(repo), "--git-hooks", "true")[0], 0)
        self.assertEqual(os.readlink(folder / "post-commit"), ".dispatcher")
        self.assertNotIn("The git hooks are files", self.run_cli("config", str(repo))[1])

    def test_a_known_repo_with_an_older_install_moves_on_plain_install(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        self.older_install(repo, folder)
        agent_memory.write_git_hooks_record({str(repo): {"dirs": [str(folder)], "links": []}})
        self.tools = ["claude-code"]
        code, out = self.run_cli("install", "--yes")
        self.assertEqual(code, 0, out)
        for name in ("post-commit", "post-merge"):
            self.assertEqual(os.readlink(folder / name), ".dispatcher")

    def test_a_missing_dispatcher_is_refused(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        (folder / ".dispatcher").unlink()
        before = tree(folder)
        code, out = self.install_git(repo)
        self.assertNotEqual(code, 0)
        self.assertIn("its .dispatcher is missing", out)
        self.assertEqual(tree(folder), before)

    def test_an_original_path_naming_cursors_folder_is_refused(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        (folder / ".cursor-original-hooks-path").write_text(str(folder), encoding="utf-8")
        before = tree(folder)
        code, out = self.install_git(repo)
        self.assertNotEqual(code, 0)
        self.assertIn("names Cursor's own folder", out)
        self.assertEqual(tree(folder), before)

    def test_a_relative_original_path_in_a_linked_worktree_is_a_clear_error(self):
        repo = self.repo()
        self.commit(repo, "init")
        self.cursor_folder(repo, original=".git/hooks")
        worktree = self.base / "wt"
        git(repo, "worktree", "add", "-q", "-b", "wt", str(worktree))
        code, out = self.install_git(worktree)
        self.assertNotEqual(code, 0)
        self.assertIn("could not create the git hooks directory", out)

    def test_no_original_path_is_refused_and_nothing_is_written(self):
        repo = self.repo()
        folder = self.cursor_folder(repo, original="")
        before = tree(folder)
        code, out = self.install_git(repo)
        self.assertNotEqual(code, 0)
        self.assertIn("doesn't write its hooks into Cursor's folder", out)
        self.assertEqual(tree(folder), before)
        self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())

    def test_a_foreign_file_in_cursors_folder_is_refused(self):
        repo = self.repo()
        folder = self.cursor_folder(repo)
        (folder / "post-commit").write_text("#!/bin/sh\necho someone\n", encoding="utf-8")
        before = tree(folder)
        code, out = self.install_git(repo)
        self.assertNotEqual(code, 0)
        self.assertIn("neither agent-memory's nor a link", out)
        self.assertEqual(tree(folder), before)
        self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())


class OtherHooksPathTest(Case):
    def test_a_hooks_path_outside_git_hooks_is_named(self):
        repo = self.repo()
        git(repo, "config", "core.hooksPath", ".githooks")
        code, out = self.install_git(repo)
        self.assertEqual(code, 0, out)
        self.assertIn(f"core.hooksPath is {repo / '.githooks'}, not", out)
        self.assertTrue((repo / ".githooks" / "post-commit").is_file())

    def test_a_relative_hooks_path_from_a_subfolder_is_the_work_tree_one(self):
        repo = self.repo()
        git(repo, "config", "core.hooksPath", ".githooks")
        (repo / "a" / "b").mkdir(parents=True)
        code, out = self.install_git(repo / "a" / "b")
        self.assertEqual(code, 0, out)
        self.assertTrue((repo / ".githooks" / "post-commit").is_file())
        self.assertFalse((repo / "a" / "b" / ".githooks").exists())
        self.assertIn(f"not {repo / '.git' / 'hooks'}.", out)

    def test_the_default_hooks_path_says_nothing_extra(self):
        repo = self.repo()
        code, out = self.install_git(repo)
        self.assertEqual(code, 0, out)
        self.assertNotIn("core.hooksPath", out)


class RepointTest(Case):
    """Findings 13 and 22: every hook install marked runs the installed command, and the message is true."""

    def old_clone(self, git_repo=True):
        """A clone (bin/agent-memory, scripts/agent_memory.py) the editor hooks ran, with git hooks that run it."""
        clone = self.repo("clone") if git_repo else self.base / "clone"
        clone.mkdir(exist_ok=True)
        old_bin = self.command(clone / "bin" / "agent-memory", "old")
        (clone / "scripts").mkdir()
        (clone / "scripts" / "agent_memory.py").write_text("", encoding="utf-8")
        for name in (".claude", ".codex", ".cursor/hooks", ".pi/agent/extensions/agent-memory"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": f"{old_bin} capture --tool claude-code --event stop"}]}],
        }}), encoding="utf-8")
        (self.home / ".codex" / "hooks.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": f"{old_bin} capture --tool codex --event stop"}]}],
        }}), encoding="utf-8")
        (self.home / ".cursor" / "hooks" / "agent-memory.sh").write_text(
            f"#!/bin/sh\n# {agent_memory.MARKER}\nexec {old_bin} capture --tool cursor\n", encoding="utf-8")
        (self.home / ".pi" / "agent" / "extensions" / "agent-memory" / "index.ts").write_text(
            f'// {agent_memory.MARKER}\nconst child = spawn("{old_bin}", ["capture"]);\n', encoding="utf-8")
        self.tools = ["claude-code", "codex", "cursor", "pi"]
        if git_repo:
            # As an older `config <clone> --git-hooks true` run from the clone wrote them.
            with mock.patch.object(agent_memory, "read_git_hooks_record", lambda: {}), \
                    mock.patch.object(agent_memory, "write_git_hooks_record", lambda record: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                agent_memory.write_git_hooks(str(clone), old_bin)
        return clone, old_bin

    def every_hook_command(self):
        return set(map(str, agent_memory.hooked_commands(self.home)))

    def test_install_repoints_the_old_clones_git_hooks_and_says_nothing_stale(self):
        clone, old_bin = self.old_clone()
        code, out = self.run_cli("install", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.every_hook_command(), {str(self.tool)})
        for name in ("post-commit", "post-merge"):
            self.assertEqual(agent_memory.git_hook_command(clone / ".git" / "hooks" / name), self.tool)
        self.assertIn(f"pointed the git hooks in {clone} at {self.tool}", out)
        self.assertNotIn("still run", out)
        self.commit(clone)
        self.assertEqual(self.ran(), ["new git-hook --event post-commit"])

    def test_install_repoints_a_recorded_repo_after_an_upgrade(self):
        repo = self.repo()
        older = self.command(self.base / "older-bin" / "agent-memory", "older")
        self.use(older)
        self.assertEqual(self.install_git(repo)[0], 0)
        self.use(self.tool)
        self.tools = ["claude-code"]
        code, out = self.run_cli("install", "--yes")
        self.assertEqual(code, 0, out)
        for name in ("post-commit", "post-merge"):
            self.assertEqual(agent_memory.git_hook_command(repo / ".git" / "hooks" / name), self.tool)
        self.assertNotIn("still run", out)

    def test_a_submodule_is_repointed_after_an_upgrade(self):
        lib = self.repo("lib")
        self.commit(lib, "lib")
        top = self.repo("top")
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sub")
        sub = top / "sub"
        self.use(self.command(self.base / "older-bin" / "agent-memory", "older"))
        self.assertEqual(self.install_git(sub)[0], 0)
        self.use(self.tool)
        self.tools = ["claude-code"]
        code, out = self.run_cli("install", "--yes")
        self.assertEqual(code, 0, out)
        hooks = agent_memory.git_hooks_dir(str(sub))
        self.assertEqual(agent_memory.git_hook_command(hooks / "post-commit"), self.tool)
        self.assertNotIn("still run", out)

    def test_hooks_that_could_not_be_repointed_are_named_after_the_writes(self):
        clone, old_bin = self.old_clone()
        # The clone's hooks folder became one we refuse to write into.
        hooks = clone / ".git" / "hooks"
        folder = self.cursor_folder(clone, original="")
        for name in ("post-commit", "post-merge"):
            os.replace(hooks / name, folder / name)
        code, out = self.run_cli("install", "--yes")
        self.assertEqual(code, 0, out)
        line = f"Git hooks in {clone} still run {old_bin}."
        self.assertIn(line, out)
        self.assertGreater(out.index(line), out.index("wrote pi"), "said after the writes")
        self.assertEqual(self.every_hook_command(), {str(self.tool)})

    def test_declining_the_editor_hooks_leaves_git_hooks_and_says_so(self):
        clone, old_bin = self.old_clone()
        code, out = self.run_cli("install", answer="n")
        self.assertEqual(code, 0, out)
        self.assertEqual(agent_memory.git_hook_command(clone / ".git" / "hooks" / "post-commit"), old_bin)
        self.assertIn(f"Git hooks in {clone} still run {old_bin}.", out)

    def test_a_clone_without_git_hooks_gets_no_message(self):
        self.old_clone(git_repo=False)
        # Without AGENT_MEMORY_DIR, as on a laptop: the clone's state moves to the temp HOME.
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.base / "xdg")}):
            os.environ.pop("AGENT_MEMORY_DIR")
            code, out = self.run_cli("install", "--yes")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Git hooks", out)
        self.assertEqual(self.every_hook_command(), {str(self.tool)})

    def test_git_repo_hooks_written_in_the_same_run_are_not_called_stale(self):
        clone, old_bin = self.old_clone()
        code, out = self.install_git(clone)
        self.assertEqual(code, 0, out)
        self.assertNotIn("still run", out)
        self.assertEqual(agent_memory.git_hook_command(clone / ".git" / "hooks" / "post-merge"), self.tool)


if __name__ == "__main__":
    unittest.main()
