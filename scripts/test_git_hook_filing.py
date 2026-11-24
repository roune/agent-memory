#!/usr/bin/env python3
"""What the git hook and land file: default-branch commits only, one memory per pull request.

These are the hook and land findings of the v0.1.0 battle test (16 to 20, and
the land items of 15). Standard library only. Every test has a temporary HOME
and AGENT_MEMORY_DIR and a stub server on 127.0.0.1 (test_cutover.Case), so
nothing touches the real ~/.config, data/ or Qdrant.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory
import outbox
import test_cutover

REPO = test_cutover.REPO
A, B = test_cutover.A, test_cutover.B


def wait_for(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class HookCase(test_cutover.GitCase):
    """A clone with the git hooks installed, committing through real git."""

    def git(self, *args, cwd=None, check=True):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=self.env(), text=True, capture_output=True, check=check
        )

    def commit_as(self, name, message, cwd=None, hooks=True, env=None):
        cwd = cwd or self.repo
        (cwd / name).write_text(name + "\n", encoding="utf-8")
        self.git("add", name, cwd=cwd)
        bypass = [] if hooks else ["-c", "core.hooksPath=/dev/null"]
        values = self.env()
        values.update(env or {})
        subprocess.run(["git", *bypass, "commit", "-q", "-m", message], cwd=cwd, env=values, check=True)
        return self.head(cwd)

    def head(self, cwd=None):
        return self.git("rev-parse", "HEAD", cwd=cwd).stdout.strip()

    def install(self, auto_approve=True):
        result = self.cli(
            "install", "--yes", "--git-repo", str(self.repo), "--install-git-hooks", "true",
            "--install-auto-approve", "true" if auto_approve else "false",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def settle(self):
        """Wait for the detached sync to empty the outbox, then return the stored memories."""
        self.assertTrue(wait_for(lambda: not self.queued()), self.queued())
        return list(self.server.rows.values())


class DefaultBranchOnlyTest(HookCase):
    """16: a commit on a feature branch is not filed, with or without auto_approve."""

    def test_a_feature_branch_commit_is_not_filed_and_a_main_commit_is(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=True)
        self.git("checkout", "-q", "-b", "feature-a")
        self.commit_as("a1", "work on A")
        self.commit_as("a2", "more work on A")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.bundles(), [])
        self.assertEqual(self.server.requests, [], "the hook stopped before any network call")

        self.git("checkout", "-q", "main")
        head = self.commit_as("m1", "fix on main")
        self.assertTrue(wait_for(lambda: self.server.rows))
        [row] = self.settle()
        self.assertEqual(row["commits"], [head])

    def test_without_auto_approve_a_feature_commit_files_no_bundle(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=False)
        self.git("checkout", "-q", "-b", "feature-a")
        self.commit_as("a1", "work on A")
        self.assertEqual(self.bundles(), [])

    def test_a_detached_head_is_not_filed(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=True)
        self.git("checkout", "-q", "--detach")
        self.commit_as("d1", "detached work")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.bundles(), [])

    def test_without_a_default_branch_nothing_is_filed_until_origin_head_names_one(self):
        self.commit_as("base", "base", hooks=False)
        self.git("branch", "-m", "main", "trunk")
        self.install(auto_approve=False)
        self.commit_as("t1", "on trunk")
        self.assertEqual(self.bundles(), [])
        self.assertIn("no local default branch", (self.state / "capture.log").read_text())

        self.git("update-ref", "refs/remotes/origin/trunk", "HEAD")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        head = self.commit_as("t2", "on trunk again")
        [bundle] = self.bundles()
        self.assertEqual([c["sha"] for c in bundle["land"]["commits"]], [head])
        self.assertEqual(bundle["git"]["branch"], "trunk")


class MergeTest(HookCase):
    """17, 18 and 19: merge commits are never filed, and a PR is one memory with its number."""

    def test_a_squash_merge_is_one_memory_with_its_pr_number_and_no_merge_message(self):
        self.commit_as("base", "base", hooks=False)
        self.git("checkout", "-q", "-b", "feature-c")
        self.commit_as("c1", "work on C", hooks=False)
        self.git("checkout", "-q", "main")
        self.git("-c", "core.hooksPath=/dev/null", "merge", "-q", "--no-ff", "feature-c",
                 "-m", "Merge pull request #101 from roune/feature-c")
        self.install(auto_approve=True)

        self.git("checkout", "-q", "-b", "feature-d")
        self.commit_as("d1", "work on D")
        self.git("checkout", "-q", "main")
        # git runs post-merge here with ORIG_HEAD at HEAD, the #101 merge commit.
        self.git("merge", "-q", "--squash", "feature-d")
        self.assertEqual(self.queued(), [])
        squash = self.commit_as("d-squash", "Add feature D (#102)\n\n* work on D")

        self.assertTrue(wait_for(lambda: self.server.rows))
        [row] = self.settle()
        self.assertEqual(row["pull_request"], 102)
        self.assertEqual(row["commits"], [squash])
        self.assertNotIn("Merge pull request", row["text"])
        self.assertEqual(self.bundles(), [])

    def test_a_merged_pull_request_is_one_memory_with_its_number_and_commits(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=True)
        self.git("checkout", "-q", "-b", "feature")
        first = self.commit_as("w1", "Add the widget model")
        second = self.commit_as("w2", "Show the widget")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #99 from roune/feature\n\nAdd the widget")

        self.assertTrue(wait_for(lambda: self.server.rows))
        [row] = self.settle()
        self.assertEqual(row["pull_request"], 99)
        self.assertEqual(row["commits"], [first, second])
        self.assertEqual(row["text"], "Add the widget\n\nAdd the widget model\n\nShow the widget")

    def test_a_conflicted_merge_concluded_by_commit_files_the_pr_not_the_merge_message(self):
        self.commit_as("base", "base", hooks=False)
        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "same.txt").write_text("feature\n", encoding="utf-8")
        self.git("add", "same.txt")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "Feature side")
        feature = self.head()
        self.git("checkout", "-q", "main")
        (self.repo / "same.txt").write_text("main\n", encoding="utf-8")
        self.git("add", "same.txt")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "Main side")
        self.install(auto_approve=False)

        merged = self.git("merge", "feature", "-m", "Merge pull request #5 from roune/feature", check=False)
        self.assertNotEqual(merged.returncode, 0, "the merge conflicts")
        (self.repo / "same.txt").write_text("both\n", encoding="utf-8")
        self.git("add", "same.txt")
        # post-commit, not post-merge, runs here, with HEAD a merge commit.
        self.git("commit", "-q", "--no-edit")

        [bundle] = self.bundles()
        self.assertEqual(bundle["land"]["pull_request"], 5)
        self.assertEqual([c["sha"] for c in bundle["land"]["commits"]], [feature])
        self.assertEqual(bundle["draft"]["text"], "Feature side")

    def test_a_pull_files_one_memory_per_pull_request(self):
        """A teammate squash-merges one PR and merge-commits another on GitHub; this clone pulls both."""
        origin = Path(self.tmp.name) / "origin.git"
        mate = Path(self.tmp.name) / "mate"
        mine = Path(self.tmp.name) / "mine"
        self.commit_as("base", "base", hooks=False)
        self.git("clone", "-q", "--bare", str(self.repo), str(origin))
        for clone in (mate, mine):
            self.git("clone", "-q", str(origin), str(clone), cwd=self.tmp.name)
            for key, value in (("user.email", "t@e.st"), ("user.name", "Tester")):
                self.git("config", key, value, cwd=clone)
        self.git("remote", "set-url", "origin", "https://github.com/example/proj.git", cwd=mine)
        self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=mine)
        self.repo = mine
        self.install(auto_approve=True)

        squash = self.commit_as("s", "Add the squashed thing (#7)", cwd=mate)
        self.git("checkout", "-q", "-b", "pr8", cwd=mate)
        one = self.commit_as("p1", "PR 8 part one", cwd=mate)
        two = self.commit_as("p2", "PR 8 part two", cwd=mate)
        self.git("checkout", "-q", "main", cwd=mate)
        self.git("merge", "-q", "--no-ff", "pr8", "-m", "Merge pull request #8 from roune/pr8\n\nTwo parts", cwd=mate)
        self.git("push", "-q", "origin", "main", cwd=mate)

        self.git("pull", "-q", "--ff-only", str(origin), "main", cwd=mine)
        self.assertTrue(wait_for(lambda: len(self.server.rows) == 2))
        rows = sorted(self.settle(), key=lambda row: row["pull_request"])
        self.assertEqual([(row["pull_request"], row["commits"]) for row in rows], [(7, [squash]), (8, [one, two])])
        self.assertNotIn("Merge pull request", rows[1]["text"])

    def test_a_merged_pr_with_a_commit_kept_elsewhere_files_only_the_rest(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=False)
        self.git("checkout", "-q", "-b", "feature")
        first = self.commit_as("w1", "Add the widget model")
        second = self.commit_as("w2", "Show the widget")
        self.server.rows["mate-change-x"] = {"id": "mate-change-x", "commits": [first], "updated_at": "1", "text": "k"}
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #99 from roune/feature")
        [bundle] = self.bundles()
        self.assertEqual(bundle["land"]["pull_request"], 99)
        self.assertEqual([c["sha"] for c in bundle["land"]["commits"]], [second])


class LandThenHookTest(HookCase):
    """20: a land bundle waiting in review and a hook that keeps the same commit make one memory."""

    def test_the_battle_test_case_ends_with_one_memory(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=True)
        w = self.commit_as("w", "Add W", hooks=False)
        result = self.cli("land", str(self.repo), "--since", "48")
        self.assertEqual(result.returncode, 0, result.stderr)
        [landed] = [b for b in self.bundles() if b["land"]["commits"][0]["sha"] == w]
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.git("merge", "-q", "--ff-only", w)
        # The hook found land's bundle for that same commit and left it for review.
        self.assertEqual(self.queued(), [])

        bundle = agent_memory.find_bundle(landed["session_id"], "git")
        agent_memory.approve(bundle, "change", "code", "Add W", commit=w)
        [row] = self.settle()
        self.assertEqual(row["commits"], [w])

    def test_a_bundle_whose_commit_the_hook_kept_meanwhile_is_dropped_at_approve(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=True)
        w = self.commit_as("w", "Add W", hooks=False)
        result = self.cli("land", str(self.repo), "--since", "48")
        self.assertEqual(result.returncode, 0, result.stderr)
        [landed] = [b for b in self.bundles() if b["land"]["commits"][0]["sha"] == w]
        # W then reaches main again as part of PR #7, a group of its own, and is auto-approved.
        self.git("reset", "-q", "--hard", "HEAD~1")
        self.git("branch", "f", w)
        self.commit_as("other", "Other work", hooks=False)
        self.git("merge", "-q", "--no-ff", "f", "-m", "Merge pull request #7 from roune/f")
        self.assertTrue(wait_for(lambda: self.server.rows))
        self.settle()

        bundle = agent_memory.find_bundle(landed["session_id"], "git")
        with self.assertRaises(agent_memory.AlreadySettled) as raised:
            agent_memory.approve(bundle, "change", "code", "Add W", commit=w)
        self.assertTrue(raised.exception.dropped)
        self.assertIn(w[:10], str(raised.exception))
        self.assertNotIn(landed["session_id"], [b["session_id"] for b in self.bundles()])
        [row] = self.server.rows.values()
        self.assertEqual(row["pull_request"], 7)


class ApproveRecheckTest(test_cutover.Case):
    """20: Approve asks the outbox and /v1/commits/status again for a git bundle's commits."""

    def keep(self, sha):
        self.server.rows["mate-change-x"] = {"id": "mate-change-x", "commits": [sha], "updated_at": "1", "text": "k"}

    def test_a_bundle_whose_commits_are_all_kept_is_dropped_and_nothing_is_stored(self):
        self.keep(A)
        with self.assertRaises(agent_memory.AlreadySettled) as raised:
            agent_memory.approve(self.bundle(shas=(A,)), "change", "code", "why", commit=A)
        self.assertTrue(raised.exception.dropped)
        self.assertIn("already kept or rejected", str(raised.exception))
        self.assertFalse(self.bundle_exists())
        self.assertEqual(self.server.posts("/v1/memories"), [])
        self.assertEqual(self.queued(), [])

    def test_a_kept_commit_is_taken_off_and_the_edited_text_is_kept(self):
        self.keep(A)
        with self.assertRaises(agent_memory.AlreadySettled) as raised:
            agent_memory.approve(self.bundle(shas=(B, A), pull_request=4), "change", "code", "edited text", commit=A)
        self.assertFalse(raised.exception.dropped)
        self.assertIn("taken off", str(raised.exception))
        bundle = agent_memory.find_bundle("s1", "git")
        self.assertEqual([c["sha"] for c in bundle["land"]["commits"]], [B])
        self.assertEqual((bundle["draft"]["commit"], bundle["draft"]["text"]), (B, "edited text"))
        self.assertEqual(self.server.posts("/v1/memories"), [])

        result = agent_memory.approve(bundle, "change", "code", "edited text", commit=B)
        self.assertEqual(result["status"], "sent")
        row = self.server.rows["tester-change-s1"]
        self.assertEqual((row["commits"], row["pull_request"]), ([B], 4))

    def test_a_commit_waiting_in_the_outbox_is_refused_and_the_bundle_kept(self):
        outbox.add(self.state, test_cutover.dead_url(), "rejection", {"repo": REPO, "shas": [A]})
        with self.assertRaises(agent_memory.AlreadySettled) as raised:
            agent_memory.approve(self.bundle(shas=(A,)), "change", "code", "edited", commit=A)
        self.assertFalse(raised.exception.dropped)
        self.assertIn("waiting in the outbox", str(raised.exception))
        self.assertEqual(agent_memory.find_bundle("s1", "git")["draft"]["text"], "edited")
        self.assertEqual(self.server.posts("/v1/memories"), [])

    def test_a_failed_outbox_item_never_drops_the_bundle(self):
        """A hook item that got a 4xx sits in failed/ and may never be sent."""
        path = outbox.add(self.state, self.server.url, "memory", {"commits": [A], "commit": A})
        outbox._move_to_failed(self.state, path, json.loads(path.read_text()), agent_memory.server_client.ServerError(403, "org"))
        with self.assertRaises(agent_memory.AlreadySettled) as raised:
            agent_memory.approve(self.bundle(shas=(A,)), "change", "code", "why", commit=A)
        self.assertFalse(raised.exception.dropped)
        self.assertTrue(self.bundle_exists())

    def test_offline_approve_queues_the_reviewed_text_without_a_drop_check(self):
        """sync must not drop text a person reviewed because one commit was rejected meanwhile."""
        self.connect(test_cutover.dead_url())
        result = agent_memory.approve(self.bundle(shas=(A, B)), "change", "code", "why", commit=B)
        self.assertEqual(result["status"], "queued")
        [item] = self.queued()
        self.assertNotIn("unless_settled", item)

    def test_a_bundle_listing_no_commits_is_refused_not_dropped(self):
        self.keep(A)
        bundle = self.bundle(shas=(A,))
        bundle["land"]["commits"] = []
        agent_memory.write_json(agent_memory.session_dir("git", "s1") / "bundle.json", bundle)
        with self.assertRaises(agent_memory.AlreadySettled) as raised:
            agent_memory.approve(bundle, "change", "code", "why", commit=A)
        self.assertFalse(raised.exception.dropped)
        self.assertTrue(self.bundle_exists())

    def test_the_recheck_uses_the_page_timeout(self):
        with unittest.mock.patch.object(agent_memory, "server_settled", return_value=set()) as settled:
            agent_memory.approve(self.bundle(shas=(A,)), "change", "code", "why", commit=A)
        self.assertEqual(settled.call_args.kwargs["timeout"], agent_memory.PAGE_TIMEOUT)

    def test_a_session_bundle_is_not_rechecked(self):
        self.keep(A)
        result = agent_memory.approve(self.bundle(shas=(A,), tool="cursor"), "change", "code", "the session's why", commit=A)
        self.assertEqual(result["status"], "sent")


class ReviewPageRecheckTest(test_cutover.ReviewPageCase):
    def test_the_page_says_why_and_the_bundle_is_gone(self):
        self.server.rows["mate-change-x"] = {"id": "mate-change-x", "commits": [A], "updated_at": "1", "text": "k"}
        self.bundle(shas=(A,))
        status, page, _ = self.post("/approve", session_id="s1", tool="git", kind="change", domain="code", text="why", commit=A)
        self.assertEqual(status, 409)
        self.assertIn("already kept or rejected", page)
        self.assertIn("bundle was dropped", page)
        self.assertFalse(self.bundle_exists())


class SyncDropTest(test_cutover.Case):
    """A PR the hook filed as one memory is dropped by sync only when all its commits are settled."""

    def item(self, check):
        body = {"kind": "change", "domain": "code", "text": "PR", "repo": REPO, "tool": "git", "session_id": "pr",
                "org": "Org", "team": "Team", "person": "tester", "occurred_at": "2026-11-20T10:00:00+00:00",
                "commit": B, "commits": [A, B]}
        outbox.add(self.state, self.server.url, "memory", body, unless_settled=check)

    def test_one_commit_kept_elsewhere_does_not_drop_the_pr(self):
        self.server.rows["mate-change-x"] = {"id": "mate-change-x", "commits": [A], "updated_at": "1", "text": "k"}
        self.item({"repo": REPO, "shas": [A, B], "all": True})
        self.sync()
        self.assertIn("tester-change-pr", self.server.rows)

    def test_every_commit_kept_drops_it(self):
        self.server.rows["mate-change-x"] = {"id": "mate-change-x", "commits": [A, B], "updated_at": "1", "text": "k"}
        self.item({"repo": REPO, "shas": [A, B], "all": True})
        self.sync()
        self.assertNotIn("tester-change-pr", self.server.rows)
        self.assertEqual(self.queued(), [])

    def test_without_all_one_commit_is_enough(self):
        self.server.rows["mate-change-x"] = {"id": "mate-change-x", "commits": [A], "updated_at": "1", "text": "k"}
        self.item({"repo": REPO, "shas": [A, B]})
        self.sync()
        self.assertNotIn("tester-change-pr", self.server.rows)


class GhMergedPrsTest(unittest.TestCase):
    def test_a_failed_list_falls_back_to_one_call_each(self):
        calls = []

        def fake_run(command, cwd=None):
            calls.append(command[:3])
            if command[:3] == ["gh", "pr", "view"]:
                return json.dumps({"title": "T", "body": "B"})
            return ""

        with unittest.mock.patch.object(agent_memory, "run", fake_run):
            prs = agent_memory.GhMergedPrs("/r", "2026-11-20T00:00:00+00:00")
            self.assertEqual(prs.describe("/r", 4), ("T", "B"))
            self.assertIsNone(prs.pr_for_sha(A))
        self.assertEqual(calls, [["gh", "pr", "list"], ["gh", "pr", "view"], ["gh", "pr", "list"]])

    def test_a_list_cut_at_the_limit_asks_for_what_it_misses(self):
        rows = [{"number": n, "title": f"t{n}", "body": "", "mergeCommit": {"oid": f"{n:040x}"}} for n in range(1, 4)]
        calls = []

        def fake_run(command, cwd=None):
            calls.append(command)
            if "--state" in command and "--search" in command and "merged:>=2026-11-19" in command:
                return json.dumps(rows)
            return ""

        with unittest.mock.patch.object(agent_memory, "run", fake_run), \
                unittest.mock.patch.object(agent_memory, "GH_PR_LIMIT", 3):
            prs = agent_memory.GhMergedPrs("/r", "2026-11-20T00:00:00+00:00")
            self.assertEqual(prs.pr_for_sha(f"{2:040x}"), 2)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(prs.pr_for_sha(A))
            self.assertEqual(len(calls), 2, "a miss in a cut list is asked for alone")
            self.assertEqual(prs.describe("/r", 3), ("t3", ""))
            self.assertEqual(len(calls), 2)


class CommitRecordsTest(HookCase):
    def test_an_empty_message_commit_on_main_is_filed(self):
        self.commit_as("base", "base", hooks=False)
        self.install(auto_approve=False)
        (self.repo / "e").write_text("e\n", encoding="utf-8")
        self.git("add", "e")
        self.git("commit", "-q", "--allow-empty-message", "-m", "")
        head = self.head()
        [bundle] = self.bundles()
        self.assertEqual([c["sha"] for c in bundle["land"]["commits"]], [head])

    def test_many_commits_are_read_in_batches(self):
        shas = [self.commit_as(f"f{i}", f"commit {i}", hooks=False) for i in range(3)]
        with unittest.mock.patch.object(agent_memory, "run", wraps=agent_memory.run) as run:
            records = agent_memory.commit_records(str(self.repo), shas * 400)
        self.assertEqual(run.call_count, 3, "1200 shas in batches of 500")
        self.assertEqual({record["sha"] for record in records}, set(shas))


class LandTest(HookCase):
    """15: land's dates come from the commits, and gh is asked once for the whole window."""

    def gh_listing(self, prs):
        folder = Path(self.tmp.name) / "gh-list-bin"
        folder.mkdir(exist_ok=True)
        self.gh_log = folder / "calls.log"
        (folder / "prs.json").write_text(json.dumps(prs), encoding="utf-8")
        script = folder / "gh"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.gh_log}"\n'
            'if [ "$1" = pr ] && [ "$2" = list ]; then\n'
            f'  cat "{folder / "prs.json"}"\n'
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        self.path = str(folder) + os.pathsep + self.path

    def test_started_at_is_the_oldest_commit_not_since(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        older, newer = now - timedelta(hours=3), now - timedelta(hours=2)
        for name, when in (("one", older), ("two", newer)):
            stamp = f"@{int(when.timestamp())} +0000"
            self.commit_as(name, f"{name} (#4)", hooks=False, env={"GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp})
        result = self.cli("land", str(self.repo), "--since", "48")
        self.assertEqual(result.returncode, 0, result.stderr)
        [bundle] = self.bundles()
        self.assertEqual(bundle["started_at"], older.isoformat())
        self.assertEqual(bundle["ended_at"], newer.isoformat())

    def test_gh_is_asked_once_for_every_group(self):
        orphans = [self.commit_as(f"o{i}", f"orphan {i}", hooks=False) for i in range(3)]
        four = self.commit_as("s4", "Four (#4)", hooks=False)
        self.commit_as("s5", "Five (#5)", hooks=False)
        self.gh_listing([
            {"number": 4, "title": "Four", "body": "Body four", "mergeCommit": {"oid": four}},
            {"number": 5, "title": "Five", "body": "Body five", "mergeCommit": None},
            {"number": 6, "title": "Six", "body": "Body six", "mergeCommit": {"oid": orphans[2]}},
        ])
        result = self.cli("land", str(self.repo), "--since", "48")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.gh_log.read_text().splitlines()
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("pr list --state merged", calls[0])
        self.assertEqual(sorted(b["land"]["pull_request"] or 0 for b in self.bundles()), [0, 0, 4, 5, 6])
        by_pr = {b["land"]["pull_request"]: b for b in self.bundles()}
        self.assertEqual(by_pr[6]["land"]["commits"][0]["sha"], orphans[2])
        self.assertEqual(by_pr[6]["draft"]["text"], "Six\n\nBody six\n\norphan 2")
        self.assertEqual(by_pr[4]["draft"]["text"], "Four\n\nBody four")


if __name__ == "__main__":
    unittest.main()
