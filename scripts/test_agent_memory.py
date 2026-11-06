#!/usr/bin/env python3
"""Integration tests: use a temp HOME and temp repo (subprocess)."""

import json
import os
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

    def _env(self, store):
        e = os.environ.copy()
        e["HOME"] = str(self.home)
        e["AGENT_MEMORY_DIR"] = str(store)
        return e

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

        memories = list((store / "memories").glob("*.json"))
        self.assertEqual(len(memories), 1)
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
        memories = list((store / "memories").glob("*.json"))
        self.assertEqual(len(memories), 1)



if __name__ == "__main__":
    unittest.main()
