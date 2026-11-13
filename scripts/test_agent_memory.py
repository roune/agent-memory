#!/usr/bin/env python3
"""Integration tests: use a temp HOME and temp repo (subprocess)."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_MEMORY = ["python3", str(ROOT / "scripts" / "agent_memory.py")]


def run_agent_memory(env, args, cwd=None, stdin=None):
    return subprocess.run(
        AGENT_MEMORY + args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        input=stdin,
    )


def memory_rows(store):
    import sqlite3

    path = Path(store) / "data" / "memories.sqlite"
    if not path.is_file():
        return []
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM memory")]
    finally:
        connection.close()


class AgentMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        cfg = self.home / ".config" / "agent-memory"
        cfg.mkdir(parents=True)
        (cfg / "identity").write_text(
            "org = TestOrg\nteam = TestTeam\nperson = tester\n", encoding="utf-8"
        )

        # A gh that always fails, first on PATH, so no test reaches GitHub.
        self.stub_bin = Path(self.tmp.name) / "stub-bin"
        self.stub_bin.mkdir()
        gh = self.stub_bin / "gh"
        gh.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        gh.chmod(0o755)

    def _env(self, store):
        e = os.environ.copy()
        e["HOME"] = str(self.home)
        e["AGENT_MEMORY_DIR"] = str(store)
        e["PATH"] = str(self.stub_bin) + os.pathsep + e.get("PATH", "")
        return e

    def _env_without_gh(self, store):
        """PATH with only git and python3, so gh is not found at all."""
        bare = Path(self.tmp.name) / "bare-bin"
        bare.mkdir(exist_ok=True)
        for name in ("git", "python3"):
            target = bare / name
            if not target.exists():
                target.symlink_to(shutil.which(name))
        e = self._env(store)
        e["PATH"] = str(bare)
        return e

    def _land_bundles(self, store):
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((store / "inbox" / "git").glob("**/bundle.json"))
        ]

    def _init_repo(self, store):
        r = Path(self.tmp.name) / "repo"
        r.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=r, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@e.st"], cwd=r, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=r, check=True)
        (r / "README").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "README"], cwd=r, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=r, check=True)
        # Remote for stable repo id
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/proj.git"],
            cwd=r,
            check=True,
        )
        return r

    def test_git_hook_review_leaves_inbox(self):
        store = Path(self.tmp.name) / "store"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)

        run_agent_memory(
            env,
            [
                "install",
                "--yes",
                "--git-repo",
                str(repo),
                "--install-git-hooks",
                "true",
                "--install-auto-approve",
                "false",
            ],
        )

        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=repo, env=env, check=True)
        subprocess.run(
            ["git", "commit", "-m", "add a file\n\nBody line."],
            cwd=repo,
            env=env,
            check=True,
        )

        inbox = store / "inbox" / "git"
        bundles = list(inbox.glob("**/bundle.json"))
        self.assertEqual(len(bundles), 1, "expected one inbox bundle")
        data = json.loads(bundles[0].read_text(encoding="utf-8"))
        self.assertEqual(data.get("tool"), "git")
        self.assertTrue((data.get("draft") or {}).get("text"))
        memories = list((store / "memories").glob("*.json"))
        self.assertEqual(len(memories), 0)

    def test_git_hook_auto_approve_writes_memory(self):
        store = Path(self.tmp.name) / "store2"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)

        run_agent_memory(
            env,
            [
                "install",
                "--yes",
                "--git-repo",
                str(repo),
                "--install-git-hooks",
                "true",
                "--install-auto-approve",
                "true",
            ],
        )

        (repo / "b.txt").write_text("y\n", encoding="utf-8")
        subprocess.run(["git", "add", "b.txt"], cwd=repo, env=env, check=True)
        subprocess.run(["git", "commit", "-m", "second commit"], cwd=repo, env=env, check=True)

        self.assertEqual(len(memory_rows(store)), 1)
        self.assertEqual(list((store / "memories").glob("*.json")), [])
        inbox = store / "inbox" / "git"
        bundles = list(inbox.glob("**/bundle.json"))
        self.assertEqual(len(bundles), 0)

    def test_config_flips_auto_approve(self):
        store = Path(self.tmp.name) / "store3"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)

        run_agent_memory(
            env,
            [
                "install",
                "--yes",
                "--git-repo",
                str(repo),
                "--install-git-hooks",
                "true",
                "--install-auto-approve",
                "false",
            ],
        )
        run_agent_memory(env, ["config", str(repo), "--auto-approve", "true"])
        (repo / "c.txt").write_text("z\n", encoding="utf-8")
        subprocess.run(["git", "add", "c.txt"], cwd=repo, env=env, check=True)
        subprocess.run(["git", "commit", "-m", "third"], cwd=repo, env=env, check=True)
        self.assertEqual(len(memory_rows(store)), 1)
        self.assertEqual(list((store / "memories").glob("*.json")), [])

    def _install(self, env, repo, hooks="true", auto="false"):
        result = run_agent_memory(
            env,
            [
                "install",
                "--yes",
                "--git-repo",
                str(repo),
                "--install-git-hooks",
                hooks,
                "--install-auto-approve",
                auto,
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def _commit(self, repo, env, path, message):
        path.write_text(path.name + "\n", encoding="utf-8")
        subprocess.run(["git", "add", path.name], cwd=repo, env=env, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo, env=env, check=True)

    def test_post_merge_files_every_new_commit(self):
        store = Path(self.tmp.name) / "store-merge"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)
        subprocess.run(["git", "checkout", "-b", "feature"], cwd=repo, check=True)
        self._commit(repo, env, repo / "one.txt", "ff one")
        self._commit(repo, env, repo / "two.txt", "ff two")
        subprocess.run(["git", "checkout", "main"], cwd=repo, check=True)
        self._install(env, repo, hooks="true", auto="false")
        merged = subprocess.run(
            ["git", "merge", "--ff-only", "feature"],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(merged.returncode, 0, merged.stderr)
        texts = [
            json.loads(path.read_text(encoding="utf-8"))["draft"]["text"]
            for path in (store / "inbox" / "git").glob("**/bundle.json")
        ]
        self.assertTrue(any("ff one" in text for text in texts), texts)
        self.assertTrue(any("ff two" in text for text in texts), texts)
        hook = repo / ".git" / "hooks" / "post-merge"
        again = subprocess.run([str(hook), "0"], cwd=repo, env=env, text=True, capture_output=True)
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_install_keeps_existing_hook_and_uses_hooks_path(self):
        store = Path(self.tmp.name) / "store-hooks"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)
        marker = repo / "user-hook-ran"
        hooks = repo / ".githooks"
        hooks.mkdir()
        user_hook = hooks / "post-commit"
        user_hook.write_text(f"#!/bin/sh\necho ran > {marker}\n", encoding="utf-8")
        user_hook.chmod(0o755)
        subprocess.run(
            ["git", "config", "core.hooksPath", ".githooks"],
            cwd=repo,
            check=True,
        )
        self._install(env, repo, hooks="true", auto="false")
        self.assertTrue(user_hook.with_name("post-commit.agent-memory-user").is_file())
        self.assertIn("agent-memory-git-hook", user_hook.read_text(encoding="utf-8"))
        self.assertFalse((repo / ".git" / "hooks" / "post-commit").exists())
        self._commit(repo, env, repo / "kept.txt", "kept hook")
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "ran")
        self.assertEqual(len(list((store / "inbox" / "git").glob("**/bundle.json"))), 1)

    def test_worktree_install_writes_common_hooks(self):
        store = Path(self.tmp.name) / "store-wt"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)
        wt = Path(self.tmp.name) / "wt"
        subprocess.run(["git", "worktree", "add", "-b", "wt-branch", str(wt)], cwd=repo, check=True)
        self._install(env, wt, hooks="true", auto="false")
        self.assertTrue((repo / ".git" / "hooks" / "post-commit").is_file())
        self.assertFalse((wt / ".git" / "hooks" / "post-commit").exists())
        self._commit(wt, env, wt / "from-wt.txt", "from worktree")
        bundles = list((store / "inbox" / "git").glob("**/bundle.json"))
        self.assertEqual(len(bundles), 1)

    def test_session_end_does_not_approve_old_head(self):
        store = Path(self.tmp.name) / "store-old-head"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)
        self._install(env, repo, hooks="false", auto="true")
        payload = json.dumps(
            {
                "session_id": "sess-old",
                "cwd": str(repo),
                "last_assistant_message": "Done, the tests pass.",
            }
        )
        result = run_agent_memory(
            env,
            ["capture", "--tool", "cursor", "--event", "session-end"],
            cwd=repo,
            stdin=payload,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(memory_rows(store), [])
        self.assertEqual(list((store / "memories").glob("*.json")), [])
        bundles = list((store / "inbox").glob("**/bundle.json"))
        self.assertEqual(len(bundles), 1)

    def test_session_end_approves_commit_message_from_this_session(self):
        store = Path(self.tmp.name) / "store-session-commit"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)
        self._install(env, repo, hooks="false", auto="true")
        start = json.dumps({"session_id": "sess-new", "cwd": str(repo)})
        started = run_agent_memory(
            env,
            ["capture", "--tool", "cursor", "--event", "stop"],
            cwd=repo,
            stdin=start,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self._commit(repo, env, repo / "during.txt", "session commit")
        end = json.dumps(
            {
                "session_id": "sess-new",
                "cwd": str(repo),
                "last_assistant_message": "Done, the tests pass.",
            }
        )
        ended = run_agent_memory(
            env,
            ["capture", "--tool", "cursor", "--event", "session-end"],
            cwd=repo,
            stdin=end,
        )
        self.assertEqual(ended.returncode, 0, ended.stderr)
        rows = memory_rows(store)
        self.assertEqual(len(rows), 1)
        self.assertEqual(list((store / "memories").glob("*.json")), [])
        document = rows[0]
        self.assertIn("session commit", document["text"])
        self.assertNotIn("Done, the tests pass", document["text"])
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(document["commit_sha"], head)

    def test_land_includes_unpushed_local_commits(self):
        store = Path(self.tmp.name) / "store-land"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env(store)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=repo, check=True)
        subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
            cwd=repo,
            check=True,
        )
        self._commit(repo, env, repo / "local.txt", "local only")
        result = run_agent_memory(env, ["land", str(repo), "--since", "48"], cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        texts = [
            json.loads(path.read_text(encoding="utf-8"))["draft"]["text"]
            for path in (store / "inbox" / "git").glob("**/bundle.json")
        ]
        self.assertTrue(any("local only" in text for text in texts), texts)

    def test_land_works_without_gh(self):
        store = Path(self.tmp.name) / "store-nogh"
        store.mkdir()
        repo = self._init_repo(store)
        env = self._env_without_gh(store)
        self._commit(repo, self._env(store), repo / "one.txt", "one")
        result = run_agent_memory(env, ["land", str(repo), "--since", "48"], cwd=repo)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        texts = [bundle["draft"]["text"] for bundle in self._land_bundles(store)]
        self.assertTrue(any("one" in text for text in texts), texts)

    def test_review_form_submits_commit(self):
        spec = importlib.util.spec_from_file_location(
            "agent_memory_under_test",
            ROOT / "scripts" / "agent_memory.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sha = "a" * 40
        page = module.render_page(
            [
                {
                    "session_id": "abc",
                    "tool": "git",
                    "repo": "/tmp/repo",
                    "ended_at": "2026-11-06T00:00:00+00:00",
                    "draft": {"kind": "change", "domain": "code", "text": "hello", "commit": sha},
                    "land": {
                        "remote_repo": "github.com/example/proj",
                        "commits": [{"sha": sha, "subject": "subj"}],
                    },
                    "last_assistant_message": "",
                }
            ],
            False,
        )
        approve = page.split('action="/approve"', 1)[1].split("</form>", 1)[0]
        self.assertIn('name="commit"', approve)
        self.assertIn(f'value="{sha}"', approve)

    def test_embed_loads_memories_without_new_stamps(self):
        store = Path(self.tmp.name) / "embed-store"
        memories = store / "memories"
        memories.mkdir(parents=True)
        old = {
            "id": "old-change-1",
            "kind": "change",
            "domain": "code",
            "repo": "github.com/example/proj",
            "tool": "git",
            "session_id": "1",
            "text": "older note",
            "commit": "abc1234def",
        }
        (memories / "old.json").write_text(json.dumps(old), encoding="utf-8")
        previous = os.environ.get("AGENT_MEMORY_DIR")
        previous_home = os.environ.get("HOME")
        os.environ["AGENT_MEMORY_DIR"] = str(store)
        os.environ["HOME"] = str(self.home)
        try:
            spec = importlib.util.spec_from_file_location(
                "embed_under_test",
                ROOT / "scripts" / "embed.py",
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            imported = module.memory_store.import_json()
            self.assertEqual(imported, 0)
            loaded = module.load_memories()
        finally:
            if previous is None:
                os.environ.pop("AGENT_MEMORY_DIR", None)
            else:
                os.environ["AGENT_MEMORY_DIR"] = previous
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home
        self.assertEqual(loaded[0]["id"], "old-change-1")
        payload = {
            "org": loaded[0].get("org") or "",
            "team": loaded[0].get("team") or "",
            "person": loaded[0].get("person") or "",
        }
        self.assertEqual(payload, {"org": "", "team": "", "person": ""})
        self.assertFalse(memories.exists())
        self.assertTrue((store / "memories.imported").is_dir())


if __name__ == "__main__":
    unittest.main()
