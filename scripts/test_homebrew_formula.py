"""PR 4b of docs/distribution-plan.md: scripts/homebrew-formula and the release's homebrew job.

The script runs as a subprocess, the way CI and the release job run it. It
writes only to stdout or to a file in a temporary folder.
"""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "homebrew-formula"
TEMPLATE = ROOT / "packaging" / "homebrew" / "agent-memory.rb"

URL = (
    "https://files.pythonhosted.org/packages/aa/bb/"
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789ab/roune_agent_memory-0.2.0.tar.gz"
)
SHA256 = "a6434aeab0887cfec662655c702f451582e8fbd431b4272e1b254a63945fe8ca"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def job(name):
    """The text of one job in release.yml, up to the next job."""
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    start = text.index(f"\n  {name}:\n")
    after = re.search(r"\n  [a-z-]+:\n", text[start + 1 :])
    return text[start : start + 1 + after.start()] if after else text[start:]


class RenderTest(unittest.TestCase):
    def test_it_sets_url_and_sha256_and_keeps_every_other_byte(self):
        result = run("--url", URL, "--sha256", SHA256)
        self.assertEqual(result.returncode, 0, result.stderr)
        template = TEMPLATE.read_text().splitlines(keepends=True)
        rendered = result.stdout.splitlines(keepends=True)
        self.assertEqual(len(rendered), len(template))
        changed = [(old, new) for old, new in zip(template, rendered) if old != new]
        self.assertEqual(
            [new for _old, new in changed],
            [f'  url "{URL}"\n', f'  sha256 "{SHA256}"\n'],
        )
        self.assertEqual([old.split('"')[0] for old, _new in changed], ["  url ", "  sha256 "])

    def test_out_writes_the_same_text_to_a_file(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "agent-memory.rb"
            result = run("--url", URL, "--sha256", SHA256, "--out", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(out.read_text(), run("--url", URL, "--sha256", SHA256).stdout)

    def test_a_file_url_needs_local(self):
        url = "file:///tmp/dist/roune_agent_memory-0.1.0.tar.gz"
        refused = run("--url", url, "--sha256", SHA256)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("--local", refused.stderr)
        allowed = run("--local", "--url", url, "--sha256", SHA256)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn(f'  url "{url}"\n', allowed.stdout)

    def test_bad_input_is_refused_and_nothing_is_written(self):
        cases = {
            "http": ["--url", URL.replace("https://", "http://"), "--sha256", SHA256],
            "no scheme": ["--url", URL.removeprefix("https://"), "--sha256", SHA256],
            "local is not enough for http": ["--local", "--url", URL.replace("https://", "http://"), "--sha256", SHA256],
            "another file name": ["--url", URL.replace("roune_agent_memory-0.2.0", "agent-memory-0.2.0"), "--sha256", SHA256],
            "a wheel": ["--url", URL.replace(".tar.gz", "-py3-none-any.whl"), "--sha256", SHA256],
            "a quote": ["--url", URL.replace("/aa/", '/a"a/'), "--sha256", SHA256],
            "short sha256": ["--url", URL, "--sha256", SHA256[:-1]],
            "upper-case sha256": ["--url", URL, "--sha256", SHA256.upper()],
            "not hex": ["--url", URL, "--sha256", "g" * 64],
            "a newline": ["--url", URL, "--sha256", SHA256 + "\n"],
            "no sha256": ["--url", URL],
            "no url": ["--sha256", SHA256],
        }
        for name, args in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as folder:
                out = Path(folder) / "agent-memory.rb"
                result = run(*args, "--out", str(out))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
                self.assertFalse(out.exists())


class TemplateTest(unittest.TestCase):
    def test_it_is_the_formula_the_plan_describes(self):
        text = TEMPLATE.read_text()
        self.assertEqual(len(re.findall(r'^  url "', text, re.MULTILINE)), 1)
        self.assertEqual(len(re.findall(r'^  sha256 "', text, re.MULTILINE)), 1)
        self.assertIn('license "PolyForm-Noncommercial-1.0.0"', text)
        self.assertIn("include Language::Python::Virtualenv", text)
        self.assertIn("virtualenv_install_with_resources", text)
        self.assertRegex(text, r'depends_on "python@3\.\d+"')
        # Homebrew reads the version from the URL; audit calls a version line redundant.
        self.assertNotRegex(text, r"^  version ", re.MULTILINE)
        self.assertNotIn("post_install", text)
        self.assertIn('refute_path_exists testpath/".claude"', text)

    def test_the_script_stays_out_of_the_wheel_and_the_image(self):
        import tomllib

        with open(ROOT / "pyproject.toml", "rb") as handle:
            setuptools = tomllib.load(handle)["tool"]["setuptools"]
        self.assertNotIn("homebrew-formula", setuptools["py-modules"])
        # The image's allowlist takes scripts/*.py, which this file isn't.
        self.assertIn("!scripts/*.py", (ROOT / ".dockerignore").read_text().splitlines())
        self.assertNotIn("homebrew", (ROOT / ".dockerignore").read_text())


class ReleaseJobTest(unittest.TestCase):
    def test_homebrew_runs_after_pypi_and_reads_the_secret_only_through_env(self):
        text = job("homebrew")
        needs = re.search(r"\n    needs: \[([^\]]*)\]", text)
        self.assertIsNotNone(needs)
        self.assertIn("pypi", [name.strip() for name in needs.group(1).split(",")])
        self.assertNotIn("id-token", text)
        self.assertIn("    permissions:\n      contents: read\n", text)
        self.assertNotIn("write", re.search(r"    permissions:\n((?:      .*\n)+)", text).group(1))
        uses = [line for line in text.splitlines() if "secrets.HOMEBREW_TAP_TOKEN" in line]
        self.assertTrue(uses)
        for line in uses:
            self.assertRegex(line, r"^          [A-Z_]+: \$\{\{ secrets\.HOMEBREW_TAP_TOKEN \}\}$")
        # A job-level `if:` can't read secrets, so the steps check an output.
        self.assertNotRegex(text, r"\n    if:")
        self.assertIn("steps.token.outputs.set == 'yes'", text)

    def test_homebrew_takes_the_sdist_from_pypi_and_opens_the_tap_pr(self):
        text = job("homebrew")
        self.assertIn("https://pypi.org/pypi/roune-agent-memory/{version}/json", text)
        self.assertIn("digests", text)
        self.assertIn("scripts/homebrew-formula --url", text)
        self.assertNotIn("--local", text)
        self.assertIn("gh pr create --repo \"$tap\"", text)
        self.assertIn("tap=roune/homebrew-tap", text)
        self.assertIn('branch="agent-memory-$VERSION"', text)
        self.assertIn("github-actions[bot]", text)
        self.assertNotIn("Co-authored-by", text)
        self.assertIn("gh workflow run publish.yml --repo $tap -f pull_request=$number", text)
        self.assertIn("persist-credentials: false", text)

    def test_the_homebrew_ci_job_installs_the_rendered_template(self):
        text = (ROOT / ".github" / "workflows" / "test.yml").read_text()
        start = text.index("\n  homebrew:\n")
        after = re.search(r"\n  [a-z-]+:\n", text[start + 1 :])
        ci = text[start : start + 1 + after.start()]
        self.assertIn("runs-on: macos-latest", ci)
        self.assertIn("uv build --sdist", ci)
        self.assertIn("scripts/homebrew-formula --local --url \"file://$sdist\"", ci)
        self.assertIn("brew install --build-from-source", ci)
        self.assertIn("brew test", ci)
        self.assertIn("brew audit --strict --new", ci)
        self.assertNotIn("--online", ci.replace("without --online", ""))
        self.assertIn("install --yes", ci)


if __name__ == "__main__":
    unittest.main()
