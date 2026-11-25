"""PR 4b of docs/distribution-plan.md: scripts/homebrew-formula and the release's homebrew job.

The script runs as a subprocess, the way CI and the release job run it. It
writes only to stdout or to a file in a temporary folder.
"""

import json
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


def pypi(files, version="0.2.0"):
    """PyPI's JSON for a release, cut down to what the script reads."""
    return {"info": {"name": "roune-agent-memory", "version": version}, "urls": files}


SDIST = {
    "packagetype": "sdist",
    "filename": "roune_agent_memory-0.2.0.tar.gz",
    "url": URL,
    "digests": {"md5": "0" * 32, "sha256": SHA256},
}
WHEEL = {
    "packagetype": "bdist_wheel",
    "filename": "roune_agent_memory-0.2.0-py3-none-any.whl",
    "url": URL.replace(".tar.gz", "-py3-none-any.whl"),
    "digests": {"sha256": "c" * 64},
}


class PyPIJsonTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.json = Path(self.folder.name) / "pypi.json"
        self.out = Path(self.folder.name) / "agent-memory.rb"

    def render(self, data, version="0.2.0"):
        self.json.write_text(data if isinstance(data, str) else json.dumps(data))
        return run("--pypi-json", str(self.json), "--version", version, "--out", str(self.out))

    def test_the_sdist_is_picked_over_the_wheel(self):
        result = self.render(pypi([WHEEL, SDIST]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out.read_text(), run("--url", URL, "--sha256", SHA256).stdout)
        self.assertIn(URL, result.stderr)

    def test_what_isnt_there_yet_or_is_wrong_is_refused_and_nothing_is_written(self):
        cases = {
            "only the wheel": pypi([WHEEL]),
            "no files": pypi([]),
            "another file name": pypi([dict(SDIST, filename="agent-memory-0.2.0.tar.gz")]),
            "a url for another file": pypi([dict(SDIST, url=URL.replace("0.2.0.tar.gz", "0.1.0.tar.gz"))]),
            "another version": pypi([SDIST], version="0.1.0"),
            "no sha256": pypi([dict(SDIST, digests={"md5": "0" * 32})]),
            "an http url": pypi([dict(SDIST, url=URL.replace("https://", "http://"))]),
            "two sdists": pypi([SDIST, SDIST]),
            "not json": "<html>503 Service Unavailable</html>",
            "not a release": {"message": "Not Found"},
        }
        for name, data in cases.items():
            with self.subTest(name):
                result = self.render(data)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue(result.stderr)
                self.assertFalse(self.out.exists())

    def test_it_needs_version_and_takes_no_url(self):
        self.json.write_text(json.dumps(pypi([SDIST])))
        for args in (["--pypi-json", str(self.json)], ["--pypi-json", str(self.json), "--version", "0.2.0", "--url", URL]):
            with self.subTest(args):
                self.assertEqual(run(*args).returncode, 2)


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

    def test_the_placeholder_url_has_pypis_hashed_shape_and_renders(self):
        url = re.search(r'^  url "([^"]+)"', TEMPLATE.read_text(), re.MULTILINE).group(1)
        self.assertRegex(url, r"^https://files\.pythonhosted\.org/packages/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]+/roune_agent_memory-0\.0\.0\.tar\.gz$")
        sha256 = re.search(r'^  sha256 "([^"]+)"', TEMPLATE.read_text(), re.MULTILINE).group(1)
        result = run("--url", url, "--sha256", sha256)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, TEMPLATE.read_text())

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
        self.assertIn("https://pypi.org/pypi/roune-agent-memory/$VERSION/json", text)
        self.assertIn('scripts/homebrew-formula --pypi-json "$RUNNER_TEMP/pypi.json" --version "$VERSION"', text)
        self.assertNotIn("--local", text)
        self.assertIn("gh pr create --repo \"$tap\"", text)
        self.assertIn("tap=roune/homebrew-tap", text)
        self.assertIn('branch="agent-memory-$VERSION"', text)
        self.assertIn("github-actions[bot]", text)
        self.assertNotIn("Co-authored-by", text)
        self.assertIn("gh workflow run publish.yml --repo $tap -f pull_request=$number", text)
        self.assertIn("persist-credentials: false", text)

    def test_a_rerun_keeps_mains_bottles_and_repairs_the_body(self):
        text = job("homebrew")
        # Only the url and sha256 lines are compared, so main's bottle block doesn't count as a change.
        self.assertIn("""grep -E '^  (url|sha256) "'""", text)
        self.assertNotIn("git diff --cached --quiet", text)
        # The body goes through a file and the API for a new PR and an open one alike.
        self.assertIn('-F body=@"$RUNNER_TEMP/body.md"', text)
        self.assertEqual(text.count("gh api -X PATCH"), 1)
        self.assertNotIn("gh pr edit", text)
        self.assertNotIn("$(cat <<", text)

    def test_the_homebrew_ci_job_installs_the_rendered_template(self):
        text = (ROOT / ".github" / "workflows" / "test.yml").read_text()
        start = text.index("\n  homebrew:\n")
        after = re.search(r"\n  [a-z-]+:\n", text[start + 1 :])
        ci = text[start : start + 1 + after.start()]
        self.assertIn("runs-on: macos-latest", ci)
        self.assertIn("uv build --sdist", ci)
        self.assertIn("scripts/homebrew-formula --local --url \"file://$sdist\"", ci)
        self.assertIn("brew install --build-from-source --verbose roune/ci/agent-memory", ci)
        # The full name installs before any trust step; the short name after one.
        self.assertLess(ci.index("roune/ci/agent-memory\n"), ci.index("brew trust --formula roune/ci/agent-memory"))
        self.assertLess(ci.index("brew trust --formula"), ci.index("brew install --build-from-source agent-memory\n"))
        self.assertIn("brew test", ci)
        self.assertIn("brew audit --strict --new", ci)
        self.assertNotIn("--online", ci.replace("without --online", ""))
        self.assertIn("install --yes", ci)


if __name__ == "__main__":
    unittest.main()
