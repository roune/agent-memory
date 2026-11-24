#!/usr/bin/env python3
"""The cutover: Approve, Reject, land and the git hook go through the server, with the outbox.

Standard library only. A stub server on 127.0.0.1 plays the agent-memory
server (stub_server.py). Every test has a temporary HOME and AGENT_MEMORY_DIR,
so nothing touches the real ~/.config, data/ or Qdrant.
"""

import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agent_memory
import outbox
import server_client
from stub_server import SilentServer, StubServer

CLI = [sys.executable, str(SCRIPTS / "agent_memory.py")]
REPO = "github.com/example/proj"
A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)


def mode_bits(path):
    return stat.S_IMODE(path.stat().st_mode)


class Case(unittest.TestCase):
    """A temporary HOME and AGENT_MEMORY_DIR, an identity file, and a stub server the server file points at."""

    token = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.state = Path(self.tmp.name) / "state"
        self.config = self.home / ".config" / "agent-memory"
        self.config.mkdir(parents=True)
        self.state.mkdir()
        (self.config / "identity").write_text("org = Org\nteam = Team\nperson = tester\n", encoding="utf-8")
        patches = [
            mock.patch.dict(os.environ, {"HOME": str(self.home), "AGENT_MEMORY_DIR": str(self.state)}),
            # agent_memory reads HOME once, when it is imported.
            mock.patch.object(agent_memory, "HOME", self.home),
            # The page and the hooks start a detached sync; in-process tests run it by hand.
            mock.patch.object(agent_memory, "spawn_sync", lambda: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = self.stub(token=self.token)
        self.connect(self.server.url, self.token)

    def stub(self, **kwargs):
        server = StubServer(**kwargs)
        self.addCleanup(server.close)
        return server

    def connect(self, url, token=None):
        server_client.write_config(url, token, "shared")

    def env(self):
        values = dict(os.environ)
        values.update(HOME=str(self.home), AGENT_MEMORY_DIR=str(self.state))
        return values

    def bundle(self, session_id="s1", shas=(A,), pull_request=None, tool="git"):
        bundle = {
            "tool": tool,
            "session_id": session_id,
            "repo": str(Path(self.tmp.name) / "clone"),
            "ended_at": "2026-11-20T10:00:00+00:00",
            "draft": {"kind": "change", "domain": "code", "text": "why it changed", "commit": shas[-1]},
            "land": {"remote_repo": REPO, "pull_request": pull_request, "commits": [{"sha": sha, "subject": "s"} for sha in shas]},
        }
        agent_memory.write_json(agent_memory.session_dir(tool, session_id) / "bundle.json", bundle)
        return bundle

    def bundle_exists(self, session_id="s1", tool="git"):
        return (agent_memory.session_dir(tool, session_id) / "bundle.json").is_file()

    def queued(self):
        return [item for _, item in outbox.queued(self.state)]

    def failed(self):
        return [item for _, item in outbox.failed(self.state)]

    def queue(self, text, url=None, session_id=None):
        """One approved learning in the outbox, as an offline Approve leaves it."""
        body = {
            "kind": "learning", "domain": "code", "text": text, "repo": REPO, "tool": "git",
            "session_id": session_id or text.replace(" ", "-"), "org": "Org", "team": "Team", "person": "tester",
            "occurred_at": "2026-11-20T10:00:00+00:00",
        }
        return outbox.add(self.state, url or self.server.url, "memory", body)

    def sync(self, retry=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = agent_memory.sync_cmd(argparse.Namespace(retry_failed=retry, quiet=False))
        return code, out.getvalue()


def dead_url():
    """A loopback URL nothing listens on: connections are refused at once."""
    holder = ThreadingHTTPServer(("127.0.0.1", 0), None)
    url = f"http://127.0.0.1:{holder.server_port}"
    holder.server_close()
    return url


class ApproveTest(Case):
    def test_approve_while_the_server_is_up_posts_and_deletes_the_bundle(self):
        result = agent_memory.approve(self.bundle(shas=(A, B), pull_request=4), "change", "code", "why it changed", commit=B)
        self.assertEqual(result, {"status": "sent", "id": "tester-change-s1", "reason": None})
        [body] = self.server.posts("/v1/memories")
        self.assertNotIn("id", body, "the server builds the id")
        self.assertEqual((body["commits"], body["pull_request"], body["person"]), ([A, B], 4, "tester"))
        self.assertEqual(self.server.rows["tester-change-s1"]["text"], "why it changed")
        self.assertFalse(self.bundle_exists())
        self.assertEqual(self.queued(), [])

    def test_approve_while_it_is_down_queues_only_the_request_body_and_deletes_the_bundle(self):
        url = dead_url()
        self.connect(url)
        bundle = self.bundle()
        bundle["transcript_copy"] = "transcript"
        bundle["last_assistant_message"] = "secret reply"
        result = agent_memory.approve(bundle, "change", "code", "why it changed", commit=A)
        self.assertEqual(result["status"], "queued")
        self.assertIn("did not answer", result["reason"])
        self.assertFalse(self.bundle_exists())
        [(path, item)] = outbox.queued(self.state)
        self.assertEqual(mode_bits(path), 0o600)
        self.assertEqual(set(item), {"server_url", "op", "body", "created_at", "attempts", "last_error", "unless_settled"})
        # sync asks the server about the commit once more before it sends.
        self.assertEqual(item["unless_settled"], {"repo": REPO, "shas": [A]})
        self.assertEqual((item["server_url"], item["op"]), (url, "memory"))
        self.assertEqual(
            set(item["body"]),
            {"kind", "domain", "text", "repo", "tool", "session_id", "org", "team", "person", "occurred_at", "commit", "commits"},
        )
        self.assertNotIn("secret reply", path.read_text())
        self.assertNotIn("transcript", path.read_text())

    def test_a_5xx_queues_too(self):
        self.server.fail_path["/v1/memories"] = (503, {"error": "store failed"})
        result = agent_memory.approve(self.bundle(), "change", "code", "why", commit=A)
        self.assertEqual(result["status"], "queued")
        self.assertFalse(self.bundle_exists())
        self.assertEqual(len(self.queued()), 1)

    def test_a_4xx_keeps_the_bundle_and_queues_nothing(self):
        self.server.fail_path["/v1/memories"] = (400, {"error": "unknown field: x"})
        with self.assertRaises(agent_memory.Refused) as raised:
            agent_memory.approve(self.bundle(), "change", "code", "why", commit=A)
        self.assertIn("unknown field: x", str(raised.exception))
        self.assertTrue(self.bundle_exists())
        self.assertEqual(self.queued(), [])

    def test_a_409_carries_the_stored_text(self):
        # A session bundle: a git bundle whose commit is kept never gets this far (Approve rechecks it).
        agent_memory.approve(self.bundle(tool="cursor"), "change", "code", "the first text", commit=A)
        with self.assertRaises(agent_memory.Refused) as raised:
            agent_memory.approve(self.bundle(tool="cursor"), "change", "code", "a second text", commit=A)
        self.assertEqual(raised.exception.stored, "the first text")
        self.assertTrue(self.bundle_exists(tool="cursor"))

    def test_queued_items_go_first_so_the_order_is_kept(self):
        self.queue("older note")
        agent_memory.approve(self.bundle(), "change", "code", "newer", commit=A)
        self.assertEqual([body["text"] for body in self.server.posts("/v1/memories")], ["older note", "newer"])

    def test_while_the_queue_is_stuck_approve_queues_behind_it(self):
        self.server.fail_path["/v1/memories"] = (503, {"error": "down"})
        self.queue("older note")
        result = agent_memory.approve(self.bundle(), "change", "code", "newer", commit=A)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(len(self.server.posts("/v1/memories")), 1, "one try for the whole queue")
        self.assertEqual([item["body"]["text"] for item in self.queued()], ["older note", "newer"])

    def test_without_a_server_approve_keeps_the_bundle(self):
        (self.config / "server").unlink()
        with self.assertRaisesRegex(server_client.ConfigError, "agent-memory connect"):
            agent_memory.approve(self.bundle(), "change", "code", "why", commit=A)
        self.assertTrue(self.bundle_exists())


class AuthOnApproveTest(Case):
    token = "am_laptop"

    def test_the_token_sets_person_and_org_and_team_default_to_its_own(self):
        (self.config / "identity").write_text("org = Elsewhere\nteam = Other\nperson = someone-else\n", encoding="utf-8")
        result = agent_memory.approve(self.bundle(), "change", "code", "why", commit=A)
        [body] = self.server.posts("/v1/memories")
        self.assertNotIn("person", body)
        self.assertNotIn("org", body)
        self.assertNotIn("team", body)
        self.assertEqual(result["id"], "tester-change-s1")
        self.assertEqual(self.server.rows["tester-change-s1"]["org"], "Org")

    def test_a_revoked_token_keeps_the_bundle_and_says_to_connect_again(self):
        self.connect(self.server.url, "am_revoked")
        with self.assertRaisesRegex(agent_memory.Refused, "connect again"):
            agent_memory.approve(self.bundle(), "change", "code", "why", commit=A)
        self.assertTrue(self.bundle_exists())
        self.assertEqual(self.queued(), [])


class IdentityRefusalTest(Case):
    token = "am_laptop"

    def test_an_org_the_token_may_not_write_goes_to_failed_and_the_queue_goes_on(self):
        for text, org in (("wrong org", "Elsewhere"), ("fine", "")):
            body = {
                "kind": "learning", "domain": "code", "text": text, "repo": REPO, "tool": "git",
                "session_id": text.replace(" ", "-"), "occurred_at": "2026-11-20T10:00:00+00:00",
            }
            if org:
                body["org"] = org
            outbox.add(self.state, self.server.url, "memory", body)
        code, out = self.sync()
        self.assertEqual(code, 1)
        [item] = self.failed()
        self.assertEqual((item["status"], item["body"]["text"]), (403, "wrong org"))
        self.assertIn("can't write org", item["error"])
        self.assertEqual(self.queued(), [])
        self.assertIn("tester-learning-fine", self.server.rows)
        result = agent_memory.approve(self.bundle(), "change", "code", "later", commit=A)
        self.assertEqual(result["status"], "sent", "a later Approve isn't held back")

    def test_a_403_for_the_token_still_keeps_the_queue(self):
        self.server.fail_path["/v1/memories"] = (403, {"error": "this token lacks the write scope (it has read)"})
        self.queue("one")
        self.sync()
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.failed(), [])


class RejectTest(Case):
    def test_reject_posts_the_commits_and_deletes_the_bundle(self):
        self.assertEqual(agent_memory.reject_bundle(self.bundle(shas=(A, B))), "sent")
        self.assertEqual(self.server.posts("/v1/rejections"), [{"repo": REPO, "shas": [A, B]}])
        self.assertFalse(self.bundle_exists())

    def test_reject_while_the_server_is_down_queues_and_deletes_the_bundle(self):
        self.connect(dead_url())
        self.assertEqual(agent_memory.reject_bundle(self.bundle(shas=(A,))), "queued")
        self.assertFalse(self.bundle_exists())
        self.assertEqual([(item["op"], item["body"]) for item in self.queued()], [("rejection", {"repo": REPO, "shas": [A]})])

    def test_a_refused_reject_keeps_the_bundle(self):
        self.server.fail_path["/v1/rejections"] = (400, {"error": "repo is required"})
        with self.assertRaises(agent_memory.Refused):
            agent_memory.reject_bundle(self.bundle())
        self.assertTrue(self.bundle_exists())


class SyncTest(Case):
    def test_sync_sends_in_order_and_deletes_what_was_sent(self):
        for text in ("one", "two", "three"):
            self.queue(text)
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual([body["text"] for body in self.server.posts("/v1/memories")], ["one", "two", "three"])
        self.assertEqual(self.queued(), [])
        self.assertIn("sent 3", out)

    def test_sync_stops_at_the_first_connection_error_and_keeps_the_rest(self):
        url = dead_url()
        for text in ("one", "two", "three"):
            self.queue(text, url=url)
        code, out = self.sync()
        self.assertEqual(code, 1)
        items = self.queued()
        self.assertEqual([item["body"]["text"] for item in items], ["one", "two", "three"])
        self.assertEqual([item["attempts"] for item in items], [1, 0, 0], "one try, not one per item")
        self.assertIn("did not answer", items[0]["last_error"])
        self.assertIn("3 waiting", out)

    def test_a_5xx_stops_too(self):
        for text in ("one", "two"):
            self.queue(text)
        self.server.fail_next = (500, {"error": "store failed"})
        self.sync()
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(len(self.queued()), 2)

    def test_one_server_down_does_not_hold_back_another(self):
        self.queue("to the dead one", url=dead_url())
        self.queue("to the live one")
        self.sync()
        self.assertEqual([body["text"] for body in self.server.posts("/v1/memories")], ["to the live one"])
        self.assertEqual([item["body"]["text"] for item in self.queued()], ["to the dead one"])

    def test_a_401_or_403_keeps_the_whole_queue(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.server.fail_path["/v1/memories"] = (status, {"error": "unknown or revoked token"})
                for text in ("one", "two"):
                    self.queue(text + str(status))
                code, out = self.sync()
                self.assertEqual(code, 1)
                self.assertEqual(len(self.queued()), 2)
                self.assertEqual(self.failed(), [])
                self.assertIn("agent-memory connect again", out)
                self.assertEqual(len(self.server.posts("/v1/memories")), 1)
                for path, _ in outbox.queued(self.state):
                    path.unlink()
                self.server.requests.clear()

    def test_a_409_moves_the_item_to_failed_with_the_stored_text_and_the_rest_goes_on(self):
        agent_memory.approve(self.bundle(), "learning", "code", "kept text")
        self.queue("other text", session_id="s1")
        self.queue("after it")
        code, out = self.sync()
        self.assertEqual(code, 1)
        self.assertEqual(self.queued(), [])
        [path_item] = outbox.failed(self.state)
        path, item = path_item
        self.assertEqual((item["status"], item["stored"], item["stored_id"]), (409, "kept text", "tester-learning-s1"))
        self.assertEqual(mode_bits(path), 0o600)
        self.assertIn("after-it", " ".join(self.server.rows))

    def test_another_4xx_moves_the_item_to_failed_and_retry_failed_brings_it_back(self):
        self.queue("bad one")
        self.server.fail_next = (400, {"error": "occurred_at needs a time zone"})
        self.sync()
        [item] = self.failed()
        self.assertEqual((item["status"], item["error"]), (400, "occurred_at needs a time zone"))
        self.assertEqual(self.queued(), [])
        code, out = self.sync(retry=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.failed(), [])
        self.assertIn("bad-one", " ".join(self.server.rows))

    def test_retry_failed_puts_items_back_in_their_old_place(self):
        first = self.queue("first")
        self.server.fail_next = (400, {"error": "bad"})
        self.sync()
        self.server.fail_path["/v1/memories"] = (503, {"error": "down"})
        self.queue("second")
        self.sync(retry=True)
        self.assertEqual([path.name for path, _ in outbox.queued(self.state)][0], first.name)

    def test_two_syncs_at_once_send_each_item_once(self):
        texts = [f"note {number}" for number in range(25)]
        for text in texts:
            self.queue(text)
        runs = [subprocess.Popen(CLI + ["sync"], env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for run in runs:
            run.communicate(timeout=60)
        sent = [body["text"] for body in self.server.posts("/v1/memories")]
        self.assertEqual(sorted(sent), sorted(texts))
        self.assertEqual(sent, texts, "and in order")
        self.assertEqual(self.queued(), [])

    def test_an_item_whose_commit_the_server_settled_is_dropped_unsent(self):
        self.server.rejections.add((REPO, A))
        body = {"kind": "change", "domain": "code", "text": "late", "repo": REPO, "tool": "cursor", "session_id": "x",
                "org": "Org", "team": "Team", "person": "tester", "occurred_at": "2026-11-20T10:00:00+00:00", "commit": A}
        outbox.add(self.state, self.server.url, "memory", body, unless_settled={"repo": REPO, "shas": [A]})
        outbox.add(self.state, self.server.url, "memory", {**body, "session_id": "y", "commit": B}, unless_settled={"repo": REPO, "shas": [B]})
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertIn("dropped 1", out)
        self.assertEqual([body["commit"] for body in self.server.posts("/v1/memories")], [B])
        self.assertEqual(self.queued(), [])

    def test_sync_with_an_empty_outbox_makes_no_call(self):
        code, out = self.sync()
        self.assertEqual((code, out.strip()), (0, "nothing to send"))
        self.assertEqual(self.server.requests, [])

    def test_flush_is_retired(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = agent_memory.flush_cmd(argparse.Namespace(repo=""))
        self.assertEqual(code, 1)
        self.assertIn("use `agent-memory sync`", err.getvalue())

    def test_the_legacy_database_file_gets_one_warning(self):
        (self.config / "database").write_text("mode = shared\n", encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(agent_memory, "_warned_legacy", False), contextlib.redirect_stderr(err):
            agent_memory.warn_legacy()
            agent_memory.warn_legacy()
        self.assertEqual(err.getvalue().count("no longer read"), 1)


class GitCase(Case):
    """A repo with a remote, and the CLI as a subprocess, as the hooks run it."""

    def setUp(self):
        super().setUp()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@e.st"],
            ["git", "config", "user.name", "Tester"],
            ["git", "remote", "add", "origin", "https://github.com/example/proj.git"],
        ):
            subprocess.run(command, cwd=self.repo, check=True)
        # A gh that always fails, so land never reaches GitHub.
        stub_bin = Path(self.tmp.name) / "bin"
        stub_bin.mkdir()
        (stub_bin / "gh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (stub_bin / "gh").chmod(0o755)
        self.path = str(stub_bin) + os.pathsep + os.environ.get("PATH", "")

    def env(self):
        values = super().env()
        values["PATH"] = self.path
        return values

    def commit(self, name):
        (self.repo / name).write_text(name + "\n", encoding="utf-8")
        subprocess.run(["git", "add", name], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"add {name}"], cwd=self.repo, check=True, env=self.env())
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, text=True, capture_output=True).stdout.strip()

    def cli(self, *args, timeout=60):
        return subprocess.run(CLI + list(args), cwd=self.repo, env=self.env(), text=True, capture_output=True, timeout=timeout)

    def bundles(self):
        return [json.loads(path.read_text()) for path in sorted((self.state / "inbox").glob("*/*/bundle.json"))]


class LandTest(GitCase):
    def test_land_skips_commits_kept_rejected_or_waiting_in_the_outbox(self):
        kept, rejected, waiting, new = (self.commit(name) for name in ("kept", "rejected", "waiting", "new"))
        self.server.rows["tester-change-x"] = {"id": "tester-change-x", "commits": [kept[:10]], "updated_at": "1", "text": "k"}
        self.server.rejections.add((REPO, rejected))
        outbox.add(self.state, dead_url(), "rejection", {"repo": REPO, "shas": [waiting[:12]]})
        result = self.cli("land", str(self.repo), "--since", "48")
        self.assertEqual(result.returncode, 0, result.stderr)
        filed = [commit["sha"] for bundle in self.bundles() for commit in bundle["land"]["commits"]]
        self.assertEqual(filed, [new])
        [status] = self.server.posts("/v1/commits/status")
        self.assertEqual(status["repo"], REPO)
        self.assertNotIn(waiting, status["shas"], "the outbox answers without the network")

    def test_land_offers_the_commits_when_the_server_does_not_answer(self):
        self.commit("one")
        self.connect(dead_url())
        result = self.cli("land", str(self.repo), "--since", "48")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.bundles()), 1)

    def test_land_sends_the_outbox_first(self):
        self.queue("waiting note")
        self.commit("one")
        result = self.cli("land", str(self.repo), "--since", "48")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.queued(), [])
        paths = [request["path"] for request in self.server.requests]
        self.assertEqual(paths[:2], ["/v1/memories", "/v1/commits/status"])


class SessionEndTest(GitCase):
    """capture's session-end auto-approve queues without a network call, and sync checks the commit."""

    def capture(self, event):
        payload = json.dumps({"session_id": "sess", "cwd": str(self.repo)})
        result = subprocess.run(
            CLI + ["capture", "--tool", "cursor", "--event", event], cwd=self.repo, env=self.env(),
            input=payload, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_commit_already_kept_on_the_server_is_not_kept_twice(self):
        self.commit("first")
        (self.repo / ".agent-memory.toml").write_text("auto_approve = true\n", encoding="utf-8")
        self.capture("stop")
        time.sleep(1.1)
        head = self.commit("during")
        self.server.rows["tester-change-other"] = {"id": "tester-change-other", "commits": [head], "updated_at": "1", "text": "k"}
        self.capture("session-end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.queued():
            time.sleep(0.05)
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.server.posts("/v1/memories"), [], "sync asked commits/status and dropped it")
        [check] = self.server.posts("/v1/commits/status")
        self.assertEqual(check, {"repo": REPO, "shas": [head]})

    def test_a_new_commit_is_kept(self):
        self.commit("first")
        (self.repo / ".agent-memory.toml").write_text("auto_approve = true\n", encoding="utf-8")
        self.capture("stop")
        time.sleep(1.1)
        head = self.commit("during")
        self.capture("session-end")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.server.rows:
            time.sleep(0.05)
        [row] = self.server.rows.values()
        self.assertEqual(row["commit"], head)


class GitHookTest(GitCase):
    def install(self, auto_approve):
        result = self.cli(
            "install", "--yes", "--git-repo", str(self.repo), "--install-git-hooks", "true",
            "--install-auto-approve", "true" if auto_approve else "false",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def outbox_files(self):
        return sorted(path.name for path in outbox.root(self.state).glob("*.json"))

    def test_auto_approve_writes_the_outbox_before_any_network_call_and_returns_within_2_seconds(self):
        self.install(auto_approve=True)
        self.commit("first")
        # The hook ran on that commit already; empty the queue and inbox, then run it by hand.
        shutil.rmtree(outbox.root(self.state), ignore_errors=True)
        shutil.rmtree(self.state / "inbox", ignore_errors=True)
        silent = SilentServer(on_accept=self.outbox_files)
        self.addCleanup(silent.close)
        self.connect(silent.url)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True).stdout.strip()

        started = time.monotonic()
        result = self.cli("git-hook", "--event", "post-commit", timeout=10)
        elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 2.0)
        self.assertTrue(silent.accepted, "the hook made its one commits/status call")
        first_seen = silent.accepted[0][1]
        self.assertEqual(len(first_seen), 1, "the outbox was written before the first connection")
        [item] = self.queued()
        self.assertEqual((item["op"], item["body"]["commit"], item["server_url"]), ("memory", head, silent.url))
        # The status call timed out, so sync must ask again before it sends.
        self.assertEqual(item["unless_settled"], {"repo": REPO, "shas": [head]})
        self.assertEqual(self.bundles(), [])
        # The detached sync is waiting on the silent server too. Close it, so
        # that sync fails fast, records its try, and is done before cleanup.
        silent.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (self.queued() and self.queued()[0]["attempts"]):
            time.sleep(0.05)
        self.assertEqual(self.queued()[0]["attempts"], 1)

    def test_auto_approve_with_the_server_up_is_delivered_by_the_detached_sync(self):
        self.install(auto_approve=True)
        head = self.commit("first")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not self.server.rows:
            time.sleep(0.05)
        [row] = self.server.rows.values()
        self.assertEqual(row["commits"], [head])
        self.assertEqual(self.queued(), [])

    def test_a_commit_kept_while_the_hooks_status_call_timed_out_gets_no_second_memory(self):
        self.install(auto_approve=True)
        original = StubServer.route
        calls = []

        def slow_first_status(stub, method, path, query, body):
            if path == "/v1/commits/status" and not calls:
                calls.append(body)
                # A teammate keeps the commit meanwhile, and the hook's call times out.
                stub.rows["mate-change-x"] = {"id": "mate-change-x", "commits": list(body["shas"]), "updated_at": "1", "text": "k"}
                time.sleep(1.5)
            return original(stub, method, path, query, body)

        with mock.patch.object(StubServer, "route", slow_first_status):
            self.commit("first")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and self.queued():
                time.sleep(0.05)
        self.assertEqual(len(calls), 1, "the hook made its status call")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.server.posts("/v1/memories"), [], "sync asked again and dropped the item")
        self.assertEqual(list(self.server.rows), ["mate-change-x"])

    def test_without_auto_approve_a_bundle_is_filed_and_nothing_is_queued(self):
        self.install(auto_approve=False)
        self.commit("first")
        self.assertEqual(len(self.bundles()), 1)
        self.assertEqual(self.queued(), [])

    def test_a_commit_the_server_settled_is_not_filed(self):
        self.install(auto_approve=False)
        self.commit("first")
        shutil.rmtree(self.state / "inbox")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True, capture_output=True).stdout.strip()
        self.server.rejections.add((REPO, head))
        result = self.cli("git-hook", "--event", "post-commit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bundles(), [])

    def test_without_a_server_auto_approve_files_a_bundle_for_review(self):
        (self.config / "server").unlink()
        self.install(auto_approve=True)
        self.commit("first")
        self.assertEqual(len(self.bundles()), 1)
        self.assertEqual(self.queued(), [])


class ReviewPageCase(Case):
    """The page from the review server itself, over HTTP on 127.0.0.1."""

    def setUp(self):
        super().setUp()
        self.page = ThreadingHTTPServer(("127.0.0.1", 0), agent_memory.ReviewHandler)
        threading.Thread(target=self.page.serve_forever, daemon=True).start()
        self.addCleanup(self.page.server_close)
        self.addCleanup(self.page.shutdown)
        self.base = f"http://127.0.0.1:{self.page.server_port}"

    def get(self, path="/?all=1"):
        with urlopen(self.base + path, timeout=10) as response:
            return response.read().decode()

    def post(self, path, headers=None, **form):
        # What a browser sends from the page itself.
        headers = {"Origin": self.base} if headers is None else headers
        request = Request(self.base + path, data=urlencode(form).encode(), method="POST", headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.read().decode(), response.url
        except HTTPError as error:
            with contextlib.closing(error):
                return error.code, error.read().decode(), None


class ReviewPageTest(ReviewPageCase):
    def test_auth_off_uses_the_identity_file_and_says_connected(self):
        self.bundle()
        page = self.get()
        self.assertIn("Connected to", page)
        self.assertIn('name="person" type="text" value="tester">', page)

    def test_offline_says_so_and_approve_queues(self):
        self.connect(dead_url())
        self.bundle()
        self.assertIn("Offline", self.get())
        status, page, url = self.post("/approve", session_id="s1", tool="git", kind="change", domain="code", text="why", commit=A)
        self.assertEqual(status, 200)
        self.assertTrue(url.endswith("/?done=queued"), url)
        self.assertIn("Approved and queued", page)
        self.assertIn("Queued: 1 waiting in the outbox, the oldest for", page)

    def test_a_409_shows_the_stored_text_and_keeps_the_bundle(self):
        agent_memory.approve(self.bundle(tool="cursor"), "change", "code", "the stored text", commit=A)
        self.bundle(tool="cursor")
        status, page, _ = self.post("/approve", session_id="s1", tool="cursor", kind="change", domain="code", text="new text", commit=A)
        self.assertEqual(status, 409)
        self.assertIn("the stored text", page)
        self.assertIn("The bundle is kept", page)
        self.assertTrue(self.bundle_exists(tool="cursor"))

    def test_failed_items_are_listed_with_their_error_and_the_409_text(self):
        agent_memory.approve(self.bundle(), "learning", "code", "kept text")
        self.queue("clashing text", session_id="s1")
        self.queue("bad <b>one</b>")
        self.server.fail_path["/v1/memories"] = (400, {"error": "occurred_at needs a time zone"})
        outbox.sync(self.state, agent_memory.client_for)
        del self.server.fail_path["/v1/memories"]
        page = self.get()
        self.assertIn("Failed", page)
        self.assertIn("occurred_at needs a time zone", page)
        self.assertIn("bad &lt;b&gt;one&lt;/b&gt;", page)
        self.assertIn("agent-memory sync --retry-failed", page)

    def test_another_host_or_origin_is_refused(self):
        self.bundle()
        port = self.page.server_port
        for host in (f"127.0.0.1.attacker.test:{port}", "127.0.0.1", f"localhost:{port}"):
            with self.subTest(host=host):
                request = Request(self.base + "/?all=1", headers={"Host": host})
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=10)
                raised.exception.close()
                self.assertEqual(raised.exception.code, 403)
        form = {"session_id": "s1", "tool": "git", "kind": "change", "domain": "code", "text": "planted", "commit": A}
        for headers in ({"Origin": "http://attacker.test"}, {}, {"Referer": "http://attacker.test/"}, {"Origin": "null"}):
            with self.subTest(headers=headers):
                status, _, _ = self.post("/approve", headers=headers, **form)
                self.assertEqual(status, 403)
        self.assertEqual(self.server.posts("/v1/memories"), [])
        self.assertTrue(self.bundle_exists())
        status, _, _ = self.post("/approve", headers={"Referer": self.base + "/?all=1"}, **form)
        self.assertEqual(status, 200, "a Referer from the page itself is enough")

    def test_reject_goes_through_the_server(self):
        self.bundle()
        status, page, url = self.post("/reject", session_id="s1", tool="git")
        self.assertEqual(status, 200)
        self.assertTrue(url.endswith("/?done=rejected"), url)
        self.assertEqual(self.server.posts("/v1/rejections"), [{"repo": REPO, "shas": [A]}])


class AuthOnReviewPageTest(ReviewPageTest):
    token = "am_laptop"

    def test_auth_off_uses_the_identity_file_and_says_connected(self):
        pass

    def test_whoami_fills_org_and_team_and_person_is_read_only(self):
        self.bundle()
        page = self.get()
        self.assertIn("Connected to", page)
        self.assertIn("as tester in Org/Team", page)
        self.assertIn('<select name="org"><option selected>Org</option></select>', page)
        self.assertIn('<select name="team"><option selected>Team</option></select>', page)
        self.assertIn('name="person" type="text" value="tester" readonly>', page)

    def test_offline_with_a_token_leaves_identity_to_the_token(self):
        self.connect(dead_url(), "am_laptop")
        self.bundle()
        page = self.get()
        self.assertIn('placeholder="set by your token" readonly', page)
        self.assertNotIn('value="tester"', page, "the identity file is only a hint with auth on")


class TokenWithAuthOffTest(ReviewPageCase):
    """A token in the server file doesn't mean auth is on: whoami decides."""

    def test_connect_with_a_token_to_a_server_with_auth_off_saves_no_token(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("am_spare\n")), contextlib.redirect_stdout(out):
            agent_memory.connect_cmd(argparse.Namespace(url=self.server.url, token_stdin=True, mode=None, repo=""))
        self.assertIn("auth off, so the token is not saved", out.getvalue())
        self.assertIsNone(server_client.read_config()["token"])
        agent_memory.approve(self.bundle(), "change", "code", "why", commit=A)
        self.assertEqual(self.server.posts("/v1/memories")[-1]["person"], "tester")

    def test_the_page_uses_the_identity_file_when_whoami_says_auth_is_off(self):
        self.connect(self.server.url, "am_left_over")
        self.bundle()
        page = self.get()
        self.assertIn('name="person" type="text" value="tester">', page)
        self.assertNotIn("set by your token", page)

    def test_per_repo_mode_shows_each_bundle_the_identity_of_its_own_server(self):
        team = self.stub(token="am_team", person="miguel", org="Acme", team="Platform")
        server_client.write_config(self.server.url, None, "per-repo")
        server_client.map_repo(REPO, team.url, "am_team")
        self.bundle()
        page = self.get()
        self.assertIn('<select name="org"><option selected>Acme</option></select>', page)
        self.assertIn('name="person" type="text" value="miguel" readonly>', page)
        self.assertEqual([request["path"] for request in team.requests], ["/v1/whoami"])


class EmbedWrapperTest(unittest.TestCase):
    """scripts/embed with a docker on PATH that only records its arguments."""

    def run_embed(self, *args):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "docker.log"
            docker = Path(folder) / "docker"
            docker.write_text(f'#!/bin/sh\necho "$@" >> {log}\n', encoding="utf-8")
            docker.chmod(0o755)
            env = dict(os.environ, PATH=folder + os.pathsep + os.environ.get("PATH", ""))
            result = subprocess.run([str(SCRIPTS / "embed"), *args], env=env, text=True, capture_output=True)
            calls = log.read_text().splitlines() if log.is_file() else []
        return result, calls

    def test_repo_is_refused_and_nothing_runs(self):
        result, calls = self.run_embed("--rebuild", "--repo", REPO)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--repo is not supported", result.stderr)
        self.assertEqual(calls, [])

    def test_rebuild_stops_reindexes_and_starts(self):
        result, calls = self.run_embed("--rebuild")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            ["compose stop server", "compose run --rm server agent-memory-server reindex --rebuild", "compose start server"],
        )

    def test_prune_drops_the_legacy_collection_too(self):
        _, calls = self.run_embed("--prune")
        self.assertEqual(calls[1], "compose run --rm server agent-memory-server reindex --prune --yes --include-legacy")

    def test_plain_embed_only_says_approve_embeds(self):
        result, calls = self.run_embed()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Approve embeds now", result.stdout)
        self.assertEqual(calls, [])


class SetupTest(unittest.TestCase):
    """scripts/setup in a copy of the clone, with docker and curl that only record, and a temp HOME."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clone = Path(self.tmp.name) / "clone"
        (self.clone / "scripts").mkdir(parents=True)
        shutil.copy2(SCRIPTS / "setup", self.clone / "scripts" / "setup")
        shutil.copy2(SCRIPTS / "server_client.py", self.clone / "scripts" / "server_client.py")
        shutil.copy2(ROOT / "compose.yaml", self.clone / "compose.yaml")
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "calls.log"
        for name in ("docker", "curl"):
            tool = self.bin / name
            tool.write_text(f'#!/bin/sh\necho "{name} $@" >> {self.log}\n', encoding="utf-8")
            tool.chmod(0o755)

    def setup_run(self):
        env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""))
        env.pop("AGENT_MEMORY_DIR", None)
        return subprocess.run([str(self.clone / "scripts" / "setup")], env=env, text=True, capture_output=True, timeout=60)

    def test_setup_starts_both_services_waits_for_health_and_writes_the_server_file(self):
        result = self.setup_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls[0], "docker compose up -d --build")
        self.assertEqual(calls[1], "curl -fsS http://127.0.0.1:8765/v1/health")
        server = self.home / ".config" / "agent-memory" / "server"
        self.assertEqual(mode_bits(server), 0o600)
        self.assertIn("url = http://127.0.0.1:8765", server.read_text())
        self.assertIn("mode = shared", server.read_text())
        self.assertTrue((self.clone / "data" / "server").is_dir())

    def test_an_existing_server_file_is_left_alone(self):
        server = self.home / ".config" / "agent-memory" / "server"
        server.parent.mkdir(parents=True)
        server.write_text("url = https://memory.example.internal\ntoken = am_x\n", encoding="utf-8")
        result = self.setup_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(server.read_text(), "url = https://memory.example.internal\ntoken = am_x\n")

    def test_the_legacy_per_repo_mode_carries_over(self):
        config = self.home / ".config" / "agent-memory"
        config.mkdir(parents=True)
        (config / "database").write_text("mode = per-repo\n", encoding="utf-8")
        self.setup_run()
        self.assertIn("mode = per-repo", (config / "server").read_text())


class ComposeTest(unittest.TestCase):
    def test_qdrant_has_no_ports_and_the_server_has_no_profile(self):
        text = (ROOT / "compose.yaml").read_text()
        qdrant, server = text.split("\nservices:\n", 1)[1].split("\n  server:\n")
        self.assertNotIn("ports:", qdrant)
        self.assertNotIn("6333:", text)
        self.assertNotIn("data/shared", text)
        self.assertNotIn("profiles", text)
        self.assertIn('- "127.0.0.1:8765:8765"', server)


if __name__ == "__main__":
    unittest.main()
