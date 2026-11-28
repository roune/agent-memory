"""PR 5 of docs/distribution-plan.md: scripts/install.sh.

The script runs under sh with a PATH made of fakes: a `uv` that records its
arguments and writes the one file a real `uv tool install` would, a `curl`
that stands in for uv's installer, and sometimes a `uname`. HOME is a
temporary folder, and each run gets a new session so /dev/tty can't be opened,
the way it can't be under cron or CI. Nothing touches the network or a real
home folder.
"""

import os
import select
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

try:
    import pty
except ImportError:  # no pty on this platform
    pty = None
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install.sh"

FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$FAKE_LOG"
case "$1 $2" in
  "tool dir") echo "$HOME/.local/bin" ;;
  "tool install")
    cat > /dev/null
    mkdir -p "$HOME/.local/bin"
    echo fake > "$HOME/.local/bin/agent-memory"
    ;;
esac
"""

# What `curl -LsSf https://astral.sh/uv/install.sh` prints: a script that
# records the variable it was run with, and drops a uv into HOME.
FAKE_CURL = """#!/bin/sh
echo "curl $*" >> "$FAKE_LOG"
cat <<'INSTALLER'
echo "installer UV_NO_MODIFY_PATH=${UV_NO_MODIFY_PATH:-unset}" >> "$FAKE_LOG"
mkdir -p "$HOME/.local/bin"
cp "$FAKE_UV_SOURCE" "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
INSTALLER
"""

FAKE_UNAME = '#!/bin/sh\necho "${FAKE_UNAME:-Linux}"\n'


def write_exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def snapshot(root):
    return {
        str(p.relative_to(root)): p.read_bytes() if p.is_file() else None
        for p in sorted(root.rglob("*"))
    }


class InstallShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="install-sh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.log = self.tmp / "log"
        self.log.touch()
        # Real tools the script needs, linked in; no real uv or curl.
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        for name in ("sh", "env", "cat", "mkdir", "cp", "chmod", "uname"):
            found = shutil.which(name)
            self.assertIsNotNone(found, name)
            (self.tools / name).symlink_to(found)
        self.fake_uv = self.tmp / "fake-uv"
        write_exe(self.fake_uv, FAKE_UV)
        # A script with a version filled in, as the release job makes it.
        self.script = self.tmp / "install.sh"
        self.script.write_text(SCRIPT.read_text().replace("@VERSION@", "0.2.0"))

    def put(self, name, text):
        (self.tools / name).unlink(missing_ok=True)
        write_exe(self.tools / name, text)

    def with_uv(self):
        self.put("uv", FAKE_UV)

    def run_script(self, *args, script=None, extra_path="", stdin=None, **env):
        environ = {
            "PATH": str(self.tools) + extra_path,
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_UV_SOURCE": str(self.fake_uv),
        }
        environ.update(env)
        return subprocess.run(
            ["/bin/sh", *(["-s", "--"] if stdin is not None else [str(script or self.script)]), *args],
            env=environ,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            start_new_session=True,
        )

    def calls(self):
        return self.log.read_text().splitlines()

    def test_installs_the_pinned_version_and_prints_the_next_steps(self):
        self.with_uv()
        before = snapshot(self.home)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            ["uv tool install roune-agent-memory==0.2.0", "uv tool dir --bin"],
        )
        out = result.stdout
        self.assertLess(out.index("agent-memory setup"), out.index("agent-memory install"))
        # uv's bin folder isn't on this PATH, so the export line is there.
        self.assertIn(f'export PATH="{self.home}/.local/bin:$PATH"', out)
        # Only what the fake uv wrote.
        self.assertEqual(before, {})
        self.assertEqual(
            snapshot(self.home),
            {".local": None, ".local/bin": None, ".local/bin/agent-memory": b"fake\n"},
        )
        # And it ran neither command it only names.
        self.assertNotIn("setup", " ".join(self.calls()))

    def test_no_export_line_when_the_bin_folder_is_on_path(self):
        self.with_uv()
        result = self.run_script(extra_path=f":{self.home}/.local/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("export PATH", result.stdout)

    def test_a_copy_without_a_version_stops_and_a_flag_fills_it(self):
        self.with_uv()
        result = self.run_script(script=SCRIPT)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--version", result.stderr)
        self.assertEqual(self.calls(), [])
        result = self.run_script("--version", "0.3.1", script=SCRIPT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0], "uv tool install roune-agent-memory==0.3.1")

    def test_missing_uv_without_yes_or_a_terminal_prints_the_command(self):
        self.put("curl", FAKE_CURL)
        before = snapshot(self.home)
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh",
            result.stderr,
        )
        self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.home), before)

    def test_yes_runs_the_uv_installer_without_editing_shell_files(self):
        self.put("curl", FAKE_CURL)
        result = self.run_script("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(),
            [
                "curl -LsSf https://astral.sh/uv/install.sh",
                "installer UV_NO_MODIFY_PATH=1",
                "uv tool install roune-agent-memory==0.2.0",
                "uv tool dir --bin",
            ],
        )
        self.assertEqual(
            sorted(snapshot(self.home)),
            [".local", ".local/bin", ".local/bin/agent-memory", ".local/bin/uv"],
        )
        # The script put uv's folder on its own PATH, but not on the user's.
        self.assertIn(f'export PATH="{self.home}/.local/bin:$PATH"', result.stdout)

    def test_run_from_a_pipe_nothing_eats_the_rest_of_the_script(self):
        # curl | sh: the script is stdin, and the fake uv reads stdin.
        self.put("curl", FAKE_CURL)
        result = self.run_script("--yes", stdin=self.script.read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-2:], ["uv tool install roune-agent-memory==0.2.0", "uv tool dir --bin"])
        self.assertIn("agent-memory setup", result.stdout)

    def test_a_truncated_script_runs_nothing(self):
        self.with_uv()
        text = self.script.read_text()
        cut = text[: text.index("\nmain \"$@\"")]
        result = self.run_script("--yes", stdin=cut)
        self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.home), {})

    def ask_on_a_terminal(self, answer):
        """Pipe the script into sh under a pty and answer the question."""
        self.put("curl", FAKE_CURL)
        env = {
            "PATH": str(self.tools),
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_UV_SOURCE": str(self.fake_uv),
        }
        pid, fd = pty.fork()
        if pid == 0:
            os.execve("/bin/sh", ["/bin/sh", "-c", f"cat '{self.script}' | sh"], env)
        out, sent, end = b"", False, time.time() + 30
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                out += data
            if not sent and b"[y/N]" in out:
                os.write(fd, answer.encode() + b"\n")
                sent = True
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), out.decode(errors="replace")

    @unittest.skipIf(pty is None, "no pty on this platform")
    def test_the_question_is_read_from_the_terminal(self):
        code, out = self.ask_on_a_terminal("y")
        self.assertEqual(code, 0, out)
        self.assertIn("uv tool install roune-agent-memory==0.2.0", self.calls())
        self.assertIn("installer UV_NO_MODIFY_PATH=1", self.calls())

    @unittest.skipIf(pty is None, "no pty on this platform")
    def test_no_or_an_empty_answer_installs_nothing_and_prints_the_command(self):
        for answer in ("n", ""):
            with self.subTest(answer=answer):
                code, out = self.ask_on_a_terminal(answer)
                self.assertEqual(code, 1, out)
                self.assertIn("env UV_NO_MODIFY_PATH=1 sh", out)
                self.assertEqual(self.calls(), [])
                self.assertEqual(snapshot(self.home), {})

    def test_native_windows_is_refused_with_the_wsl_link(self):
        self.with_uv()
        self.put("uname", FAKE_UNAME)
        for name in ("MINGW64_NT-10.0", "MSYS_NT-10.0", "CYGWIN_NT-10.0"):
            with self.subTest(uname=name):
                result = self.run_script(FAKE_UNAME=name)
                self.assertEqual(result.returncode, 1)
                self.assertIn("docs/deploy.md#windows-wsl-2", result.stderr)
                self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.home), {})

    def test_an_unknown_option_is_refused(self):
        result = self.run_script("--nope")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--nope", result.stderr)


class ReleaseJobTest(unittest.TestCase):
    def test_github_release_runs_after_pypi_with_write_on_that_job_only(self):
        import re

        text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        start = text.index("\n  github-release:")
        after = re.search(r"\n  [a-z-]+:\n", text[start + 1 :])
        job = text[start : start + 1 + after.start()] if after else text[start:]
        self.assertIn("needs: [check, pypi]", job)
        self.assertIn("contents: write", job)
        self.assertEqual(text.count("contents: write"), 1)
        self.assertIn("--verify-tag", job)
        self.assertIn("--prerelease", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("--generate-notes", job)
        for name in ("release/install.sh", "compose.yaml", "compose.team.yaml"):
            self.assertIn(name, job)
        self.assertEqual(SCRIPT.read_text().count("@VERSION@"), 1)


if __name__ == "__main__":
    unittest.main()
