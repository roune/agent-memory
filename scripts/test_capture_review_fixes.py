#!/usr/bin/env python3
"""Capture and review-page fixes from the v0.1.0 battle test (findings 6, 7, 9 and 12).

Transcripts here are small synthetic fixtures, never real sessions.
"""

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent_memory.py"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))


def load_module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_jsonl(path, entries):
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def parse(text):
    return datetime.fromisoformat(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.store = self.root / "store"
        self.store.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for command in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@e.st"],
            ["git", "config", "user.name", "Tester"],
            ["git", "commit", "-q", "--allow-empty", "-m", "init"],
            ["git", "remote", "add", "origin", "git@github.com:example/proj.git"],
        ):
            subprocess.run(command, cwd=self.repo, check=True, capture_output=True)

    def env(self):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["AGENT_MEMORY_DIR"] = str(self.store)
        env["BROWSER"] = "true"
        return env


class CaptureDatesTest(Base):
    """Finding 6: a late capture keeps the session's own times."""

    def capture(self, tool, event, payload):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "capture", "--tool", tool, "--event", event],
            cwd=self.repo,
            env=self.env(),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.store / "capture.log"
        self.assertFalse(log.is_file() and "failed" in log.read_text(), log.read_text() if log.is_file() else "")
        session = payload.get("session_id") or payload["conversation_id"]
        return json.loads((self.store / "inbox" / tool / session / "bundle.json").read_text())

    def test_claude_code_session_end_reads_the_transcript_times(self):
        transcript = self.root / "old.jsonl"
        write_jsonl(
            transcript,
            [
                {"type": "derived-from", "sessionId": "s1"},
                {"type": "queue-operation", "timestamp": "2026-10-01T09:00:05.100Z"},
                {"type": "user", "timestamp": "2026-10-01T09:00:06.000Z", "message": {"content": "SECRET-PROMPT"}},
                # Lines are not strictly in order: the earliest wins.
                {"type": "attachment", "timestamp": "2026-10-01T09:00:04.900Z"},
                {"type": "assistant", "timestamp": "2026-10-01T11:30:00.500Z", "message": {"content": "SECRET-REPLY"}},
                {"type": "custom-title", "customTitle": "x"},
            ],
        )
        bundle = self.capture(
            "claude-code",
            "SessionEnd",
            {"session_id": "s1", "cwd": str(self.repo), "transcript_path": str(transcript), "hook_event_name": "SessionEnd"},
        )
        self.assertEqual(bundle["started_at"], "2026-10-01T09:00:04+00:00")
        self.assertEqual(bundle["ended_at"], "2026-10-01T11:30:00+00:00")
        # Only the times are read: the transcript's text stays in the copy, out of bundle.json.
        raw = (self.store / "inbox" / "claude-code" / "s1" / "bundle.json").read_text()
        self.assertNotIn("SECRET", raw)

    def test_codex_rollout_uses_top_level_timestamps_only(self):
        transcript = self.root / "rollout.jsonl"
        write_jsonl(
            transcript,
            [
                {
                    "timestamp": "2026-09-14T08:00:00.000Z",
                    "type": "session_meta",
                    "payload": {"timestamp": "1999-01-01T00:00:00Z"},
                },
                {"timestamp": "2026-09-14T08:45:10.250+02:00", "type": "response_item", "payload": {}},
            ],
        )
        bundle = self.capture(
            "codex",
            "session-end",
            {"session_id": "c1", "cwd": str(self.repo), "transcript_path": str(transcript)},
        )
        # The second line is earlier once its +02:00 offset is applied.
        self.assertEqual(bundle["started_at"], "2026-09-14T06:45:10+00:00")
        self.assertEqual(bundle["ended_at"], "2026-09-14T08:00:00+00:00")

    def test_cursor_transcript_without_times_falls_back_to_now(self):
        transcript = self.root / "cursor.jsonl"
        write_jsonl(
            transcript,
            [
                {"role": "user", "message": {"content": "hi"}},
                {"role": "assistant", "message": {"content": "hello"}},
                {"type": "status", "status": "completed"},
            ],
        )
        before = datetime.now(timezone.utc) - timedelta(seconds=2)
        bundle = self.capture(
            "cursor",
            "sessionEnd",
            {"conversation_id": "k1", "workspace_roots": [str(self.repo)], "transcript_path": str(transcript)},
        )
        after = datetime.now(timezone.utc) + timedelta(seconds=2)
        for key in ("started_at", "ended_at"):
            self.assertTrue(before <= parse(bundle[key]) <= after, bundle[key])

    def test_a_later_capture_without_the_transcript_reads_the_copy(self):
        transcript = self.root / "old.jsonl"
        write_jsonl(
            transcript,
            [
                {"type": "user", "timestamp": "2026-08-01T10:00:00Z"},
                {"type": "assistant", "timestamp": "2026-08-01T10:20:00Z"},
            ],
        )
        payload = {"session_id": "s2", "cwd": str(self.repo), "transcript_path": str(transcript)}
        self.capture("claude-code", "Stop", payload)
        transcript.unlink()
        bundle = self.capture("claude-code", "SessionEnd", payload)
        self.assertEqual(bundle["started_at"], "2026-08-01T10:00:00+00:00")
        self.assertEqual(bundle["ended_at"], "2026-08-01T10:20:00+00:00")

    def test_a_large_transcript_is_read_at_its_head_and_tail(self):
        module = load_module("agent_memory_times_test")
        module.TRANSCRIPT_TIME_WINDOW = 256
        path = self.root / "big.jsonl"
        pad = [{"type": "progress", "pad": "x" * 40} for _ in range(20)]
        # A time in the middle, outside both windows, would change the answer if it were read.
        write_jsonl(
            path,
            [{"type": "user", "timestamp": "2026-07-01T00:00:00Z"}]
            + pad
            + [{"type": "user", "timestamp": "2020-01-01T00:00:00Z"}]
            + pad
            + [{"type": "assistant", "timestamp": "2026-07-01T05:00:00Z"}],
        )
        self.assertGreater(path.stat().st_size, 2 * module.TRANSCRIPT_TIME_WINDOW)
        self.assertEqual(
            module.transcript_times(path),
            ("2026-07-01T00:00:00+00:00", "2026-07-01T05:00:00+00:00"),
        )

    def test_unreadable_or_odd_lines_give_no_times(self):
        module = load_module("agent_memory_odd_times_test")
        path = self.root / "odd.jsonl"
        path.write_text(
            'not json\n[1, 2]\n{"timestamp": 5}\n{"timestamp": "yesterday"}\n{"timestamp": "2026-01-01T00:00:00"}\n',
            encoding="utf-8",
        )
        self.assertEqual(module.transcript_times(path), (None, None))
        self.assertEqual(module.transcript_times(self.root / "missing.jsonl"), (None, None))


class ReviewPageTest(Base):
    def page(self, item):
        module = load_module("agent_memory_page_test")
        os.environ["AGENT_MEMORY_DIR"] = str(self.store)
        page = module.render_page([item], True)
        return module, page, page.split('action="/approve"', 1)[1].split("</form>", 1)[0]

    def item(self, **extra):
        item = {
            "session_id": "s1",
            "tool": "claude-code",
            "repo": str(self.repo),
            "started_at": "2026-11-01T00:00:00+00:00",
            "ended_at": "2026-11-01T01:00:00+00:00",
            "draft": {"kind": "learning", "domain": "code", "text": "short"},
            "last_assistant_message": "",
        }
        item.update(extra)
        return item

    def setUp(self):
        super().setUp()
        saved = os.environ.get("AGENT_MEMORY_DIR")
        self.addCleanup(
            lambda: os.environ.pop("AGENT_MEMORY_DIR", None)
            if saved is None
            else os.environ.__setitem__("AGENT_MEMORY_DIR", saved)
        )

    # Finding 7
    def test_a_session_bundle_prefills_the_remote_repo_id_from_origin(self):
        _, _, form = self.page(self.item())
        self.assertIn('name="remote_repo" type="text" value="github.com/example/proj"', form)

    def test_the_land_remote_repo_still_wins(self):
        _, _, form = self.page(self.item(land={"remote_repo": "github.com/other/repo"}))
        self.assertIn('name="remote_repo" type="text" value="github.com/other/repo"', form)

    def test_a_deleted_repo_leaves_the_field_empty(self):
        _, _, form = self.page(self.item(repo=str(self.root / "gone")))
        self.assertIn('name="remote_repo" type="text" value=""', form)

    # Finding 9
    def test_the_long_text_note_is_on_every_form_and_hidden_while_short(self):
        module, page, form = self.page(self.item())
        self.assertIn("This text is long", form)
        self.assertIn('data-long-note aria-live="polite" hidden>', form)
        self.assertIn(f'data-long-chars="{module.LONG_TEXT_CHARS}"', form)
        self.assertIn('querySelectorAll("textarea[data-long-chars]")', page)
        self.assertIn('addEventListener("input"', page)

    def test_a_long_draft_shows_the_note_without_js(self):
        module = load_module("agent_memory_long_test")
        text = "x" * (module.LONG_TEXT_CHARS + 1)
        _, _, form = self.page(self.item(draft={"kind": "learning", "domain": "code", "text": text}))
        self.assertIn('data-long-note aria-live="polite">This text is long', form)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ReviewStopTest(Base):
    """Finding 12: kill <pid> runs the cleanup, so review.json goes."""

    def start(self):
        state = self.store / "review.json"
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "review", "--port", str(free_port())],
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        def reap():
            if process.poll() is None:
                process.kill()
            process.communicate()

        self.addCleanup(reap)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if state.is_file():
                try:
                    pid = json.loads(state.read_text()).get("pid")
                except ValueError:
                    pid = None
                if pid == process.pid:
                    # Once the page answers, serve_forever is running.
                    port = json.loads(state.read_text())["port"]
                    try:
                        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                        return process, state
                    except OSError:
                        pass
            if process.poll() is not None:
                self.fail(process.stderr.read())
            time.sleep(0.05)
        self.fail("review page did not start")

    def stop_with(self, signum):
        process, state = self.start()
        process.send_signal(signum)
        _, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 128 + signum, err)
        self.assertFalse(state.exists())

    def test_sigterm_removes_review_json(self):
        self.stop_with(signal.SIGTERM)

    def test_sighup_removes_review_json(self):
        self.stop_with(signal.SIGHUP)

    def test_a_newer_pages_review_json_is_left(self):
        module = load_module("agent_memory_state_test")
        state = self.store / "review.json"
        state.write_text(json.dumps({"pid": os.getpid() + 1, "port": 1}), encoding="utf-8")
        module.drop_review_state(state)
        self.assertTrue(state.exists())
        state.write_text(json.dumps({"pid": os.getpid(), "port": 1}), encoding="utf-8")
        module.drop_review_state(state)
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
