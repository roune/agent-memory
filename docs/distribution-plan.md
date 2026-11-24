# Distribution plan

Status: written from Miguel's interview answers on 24 November 2026, and waiting for his approval. No PR below is built yet.

This plan covers how someone installs and starts agent-memory without a clone. Today that takes a uv install from git and then about seven manual steps from [deploy.md](deploy.md#one-laptop-without-a-clone): make a folder, download `compose.yaml`, create the data folders, write `.env`, pull, start, and connect. The plan replaces those steps with one command, `agent-memory setup`, and makes the CLI installable from PyPI and a Homebrew tap.

It ends in an ordered list of PRs, one per piece, in the style of [server-plan.md](server-plan.md). Each PR can be reviewed on its own. `python3 -m unittest discover -s scripts -p 'test_*.py'` and `scripts/test-server` pass at every commit.

## What stays locked

These come from [context.md](context.md) and the "Do not" list in `AGENTS.md`. This plan doesn't reopen them:

- **Hooks only after a yes.** A package's install step puts the CLI on `PATH` and prints the next steps, and nothing else. No formula, wheel or installer script writes into `~/.claude`, `~/.cursor`, `~/.codex` or Pi. `agent-memory install` stays a command the person runs, and it asks first.
- **The store.** "Without a clone, `data/` sits next to the `compose.yaml` you run." `agent-memory setup` keeps `compose.yaml`, `.env` and `data/` in one folder.
- **No runtime dependencies for capture or review.** The PyPI package keeps `dependencies = []`. Only the `[server]` extra has dependencies, and the image is what uses it.
- **Qdrant server, the default model and its revision.** Unchanged. `setup` runs the published image, which carries both.
- **No automatic retrieval, and no write tool on MCP.** Unchanged.
- **One PR per piece.**

No wording in `context.md` needs to change.

## Decisions from the interview

| Question | Decision |
| --- | --- |
| Channels | **PyPI**, then a **Homebrew tap** (`roune/homebrew-tap`). Besides those: a `curl \| sh` installer attached to each release, and docs for `uvx` and mise. `uv tool install git+…` keeps working. |
| Windows | **WSL 2 only.** On native Windows the CLI exits at once with a message pointing at WSL. Scoop and winget are out, because they would only serve native Windows. |
| PyPI name | **Try `agent-memory`, fall back to `roune-agent-memory`.** Register a pending publisher for `agent-memory`. If PyPI refuses that name, use `roune-agent-memory`. Either way the command is `agent-memory`. |
| v0.1.0 tag | **Tag it now**, and make the GHCR image public, so the no-clone steps in `deploy.md` work today. `setup`, PyPI and Homebrew ship in v0.2.0. |
| Where `setup` keeps the server | **`${XDG_DATA_HOME:-~/.local/share}/agent-memory/server/`**, next to the CLI's inbox and outbox. `--dir` picks another folder, and the choice is remembered. |
| Editors after `setup` | **Print the commands, and offer `connect --editor` once**, in a terminal. `connect` then shows its own diff and asks again, as it does today. |
| `install` and `setup` | **Separate commands.** Each prints the next step. There's no guided `init`. |
| An existing clone's server | **No migration.** Miguel is the only user so far, and his clone keeps `./scripts/setup`. `setup` only refuses to start a second server on port 8765 that someone else is already running. |
| Upgrades | **Run `agent-memory setup` again** after upgrading the CLI. It rewrites `compose.yaml` for the new version, keeps `.env` and `data/`, pulls the new image and restarts. `/v1/health` gains a `version`, and the CLI warns when it differs from its own. |
| Team server | **Docs only.** `setup` is for one laptop, with auth off. A team server keeps the steps in [deploy.md](deploy.md#a-team-server). |

## What was checked

These were checked on 24 November 2026.

### Names

- **PyPI.** There's no project called `agent-memory`, but PyPI refuses a new name that is "too similar to an existing project" ([choosing a name](https://docs.pypi.org/project-management/choosing-a-name/)). The page doesn't give the rule. `agentmemory` (v0.4.8, 2023) exists, so `agent-memory` may be refused. Only registering it will tell.
- **Names that are taken:** `agentmemory`, `agentmem`, `agent-memory-cli` (another coding-agent memory tool, from November 2026), `agent-memory-kit`, `agent-recall`, and `agent-memory-server`, which is [redis/agent-memory-server](https://github.com/redis/agent-memory-server).
- **The Redis package clashes in two places.** It installs a console script named `agent-memory`, and a top-level Python package named `agent_memory_server`. Both are the same names as ours. Isolated installs (uv tool, pipx, Homebrew) keep them apart, but two tools on one `PATH` don't. See [Risks](#risks-to-watch).
- **Names that are free:** `roune-agent-memory`, `kept-memory`, `agent-keep` and `agent-mem`.
- **Homebrew.** There's no `agent-memory` formula or cask. It has to be a tap anyway: homebrew-core takes only licenses compatible with the Debian Free Software Guidelines ([Acceptable Formulae](https://docs.brew.sh/Acceptable-Formulae)), and PolyForm Noncommercial 1.0.0 restricts commercial use.
- **winget and Scoop Main.** Neither has an `agent-memory` package. Both are out of this plan.

### POSIX-only code

The server runs in a Linux container, so its own `fcntl` lock (`agent_memory_server/lock.py`) is fine. The CLI has these POSIX-only parts:

| Where | What | On native Windows |
| --- | --- | --- |
| `outbox.py:35` | `import fcntl`, no guard; `flock` around the queue | the CLI can't start, because `agent_memory.py` imports `outbox` |
| `local_state.py:37` | `fcntl`, guarded | the state move runs unlocked |
| `agent_memory.py` `review_alive` | `os.kill(pid, 0)` to check that the review page lives | it **terminates** that process instead |
| `agent_memory.py` `spawn_sync` | `start_new_session=True` | it needs `DETACHED_PROCESS` creation flags instead |
| `editors.py` | `os.fchmod` | exists on Windows only from Python 3.13; `requires-python` is `>=3.11` |
| git hooks, Cursor hook | `#!/bin/sh` scripts | Git for Windows runs git hooks with its own sh; Cursor's `hooks/agent-memory.sh` needs a `.cmd` or PowerShell twin |
| Cursor's managed hooks folder | `os.symlink` to `.dispatcher` | needs Developer Mode or admin rights |
| hook commands for Claude Code and Codex | `shlex.quote` | the wrong quoting for cmd or PowerShell |
| modes 0600 and 0700 | `chmod` on the token, the outbox and the state folder | do nothing, so tokens aren't protected by mode |
| `bin/agent-memory`, `scripts/setup`, `scripts/install` | sh scripts; `id -u` | clone only. A Python `setup` must skip the uid and gid on Windows |

**Cost.** WSL 2 costs a guard and a docs section. Native Windows would be about one code PR for the table above, plus Windows versions of the Cursor and Pi hooks, plus a `windows-latest` CI job. Scoop or winget would come on top of that. This plan does WSL only. The table stays here for a later plan.

### What each channel needs

- **PyPI, trusted publishing** ([docs](https://docs.pypi.org/trusted-publishers/)):
  - A *pending publisher*, registered on PyPI's account page. It names the project, `roune/agent-memory`, the workflow `release.yml`, and the environment `pypi`.
  - It doesn't reserve the name until the first upload. If someone else registers the name first, the pending publisher stops working.
  - The upload job needs `id-token: write` and `environment: pypi`, and uses `pypa/gh-action-pypi-publish@release/v1`. The action's README asks for a separate build job that hands `dist/` over as an artifact, with `id-token: write` only on the upload job.
  - Attestations are on by default. A version can never be uploaded twice; it can only be yanked.
- **Homebrew tap** ([taps](https://docs.brew.sh/How-to-Create-and-Maintain-a-Tap), [Python formulae](https://docs.brew.sh/Language-Specific-Formulae)):
  - A repo named `homebrew-tap`, made with `brew tap-new roune/homebrew-tap`. Its default workflows build bottles, and `brew pr-pull` publishes them.
  - The formula uses `include Language::Python::Virtualenv`, `depends_on "python@3.x"` and `virtualenv_install_with_resources`. With no dependencies there are no `resource` blocks.
  - The docs don't say whether the build backend (setuptools) must be a resource. PR 4a finds out with `brew install --build-from-source`.
  - Users run `brew install roune/tap/agent-memory`. Homebrew may ask them to trust a third-party tap first (`brew trust`, [Tap Trust](https://docs.brew.sh/Tap-Trust)).
  - `brew upgrade` follows the formula's `url` and `sha256`.
- **winget and Scoop**, for the record:
  - winget accepts only exe, msi, msix or zip installers, not scripts ([winget-pkgs README](https://github.com/microsoft/winget-pkgs/blob/master/README.md)). A Python CLI would need a frozen exe for every release.
  - Scoop takes a JSON manifest in a bucket repo. It can `depends` on Python and run PowerShell after install.
  - Both need native Windows first.

## Target shape

```
brew install roune/tap/agent-memory        ┐
uv tool install agent-memory               │  the CLI on PATH, and the next steps printed.
pipx install agent-memory                  │  Nothing else is written.
curl -LsSf …/install.sh | sh               ┘
        │
agent-memory setup     # the server in Docker: ~/.local/share/agent-memory/server/{compose.yaml,.env,data/}
        │              # waits for /v1/health, writes ~/.config/agent-memory/server, offers connect --editor
agent-memory install   # editor hooks, after a yes (unchanged)
agent-memory connect --editor claude-code   # if setup's offer was declined
```

`agent-memory` above is the PyPI name if PyPI accepts it, and `roune-agent-memory` otherwise. A clone keeps `./scripts/install` and `./scripts/setup`, which build the image from source.

## `agent-memory setup`

`setup` does what `scripts/setup` does, for a tool install.

1. **Check Docker.** It needs `docker` on `PATH`, `docker compose version` to work, and `docker info` to reach the daemon. Each failure prints one line that says what to install or start (Docker Desktop, OrbStack, colima, or Docker Engine in WSL), then exits 1.
2. **Pick the folder.** The default is `${XDG_DATA_HOME:-~/.local/share}/agent-memory/server/`, created with mode 0700. With `--dir <path>`, it records the choice in `~/.config/agent-memory/setup` (`dir = …`), so a rerun without `--dir` finds it. `AGENT_MEMORY_DIR` doesn't move it, because that variable is for the CLI's own state.
3. **Write `compose.yaml`** from the copy inside the package (`importlib.resources`). That copy is byte-for-byte the repo's `compose.yaml`, pinned to this version's image.
   - A file there that `setup` wrote before is replaced. `setup` knows its own files by their content: the packaged file of some release.
   - Any other file is first saved as `compose.yaml.<timestamp>.bak`, and `setup` says so. Settings belong in `.env`, such as `AGENT_MEMORY_IMAGE` or `AGENT_MEMORY_EMBEDDER`.
4. **Refuse a downgrade.** If the image the old `compose.yaml` pins is newer than this CLI's version, `setup` stops and says to upgrade the CLI. `--force` overrides it. A newer server may have written data an older image can't read.
5. **Create the data folders**, `data/server` and `data/qdrant`, before Docker starts. On Linux, Docker would otherwise create them owned by root.
6. **Write `.env`** with mode 0600. It adds `AGENT_MEMORY_UID` and `AGENT_MEMORY_GID` from `os.getuid()` and `os.getgid()` when they're missing, and keeps every other line.
7. **Check that port 8765 is free.** If `127.0.0.1:8765/v1/health` already answers and `setup`'s own compose project isn't running, another server owns the port, most likely a clone's `./scripts/setup`. `setup` says so, names the command that stops it (`docker compose down` in that clone), and exits 1 without changing anything else.
8. **Start the server.** It runs `docker compose -p agent-memory-server --project-directory <dir> pull`, then `up -d --no-build`.
   - The fixed project name keeps `setup`'s containers apart from a clone's, whose project is named after the clone folder.
   - `--no-build` matters because the packaged file keeps the `build: .` line for clones. With it, compose never tries to build in a folder with no Dockerfile.
9. **Wait for `/v1/health`** for up to 120 seconds. On a timeout it prints the last 40 lines of `server` and `qdrant` logs, as `scripts/setup` does, and exits 1.
10. **Write the CLI's server config**, `~/.config/agent-memory/server` with `url = http://127.0.0.1:8765`, through `server_client.write_config`, but only if it's missing. If it exists and names another URL, `setup` leaves it and prints that URL and the `connect --url` command.
11. **Offer the editors, then print the next steps.**
    - It prints `agent-memory install` (hooks, after a yes) and `agent-memory connect --editor <found>`, where `<found>` is the editors `present_tools()` detects.
    - In a terminal it also asks once: `Add MCP to claude-code, cursor? [y/N]`. A yes runs `connect --editor`, which shows its diff and asks again.
    - Without a terminal, or with `--no-input`, it only prints.
    - `setup` never writes editor files itself.

`setup` is safe to run again: that's the upgrade step. It never runs `docker compose down -v` and never deletes `data/`.

`setup --print-dir` prints the folder. The final message names the compose command for logs and for stopping the server (`docker compose -p agent-memory-server --project-directory <dir> logs` and `down`). Server subcommands such as `status`, `logs` or `stop` are a later choice. See [Out of this plan](#out-of-this-plan).

On native Windows, `setup` never runs, because of the guard in PR 1. In WSL it's the Linux path.

## Upgrades

| Install | Upgrade the CLI | Then |
| --- | --- | --- |
| Homebrew | `brew upgrade agent-memory` | `agent-memory setup` |
| PyPI with uv | `uv tool upgrade <name>` | `agent-memory setup` |
| PyPI with pipx | `pipx upgrade <name>` | `agent-memory setup` |
| `install.sh` | run it again | `agent-memory setup` |
| uv from git | `uv tool upgrade agent-memory` | `agent-memory setup` |
| a clone | `git pull` | `./scripts/setup` |

- **What the warning says.** `/v1/health` gains `"version": "<x.y.z>"`, read from the package metadata in the image. `setup` and `review` compare it with the CLI's own version. When they differ, they print one line: "the server runs 0.2.0 and this CLI is 0.3.0: run `agent-memory setup` to upgrade the server". A server with no `version` field is older than 0.2.0.
- **Hooks stay quiet.** `capture`, `git-hook` and `sync --quiet` never warn, because they must stay fast and silent.
- **The hooks survive an upgrade.** They run the command as it was called, not the resolved path ([deploy.md](deploy.md#git-hooks-in-a-folder-cursor-manages)): `$(brew --prefix)/bin/agent-memory` for Homebrew, and uv's or pipx's bin link for those. All of these stay put across an upgrade. PR 4a tests the Homebrew case, because there the real file sits under the versioned `Cellar/`.
- **Switching channels**, for example from the git install to Homebrew:
  1. Uninstall the old one (`uv tool uninstall agent-memory`).
  2. Install the new one.
  3. Run `agent-memory install` once, so the hooks it wrote point at the new command. It already rewrites only its own marked entries.

  The inbox, outbox and config live in the XDG folders, so they stay.

## PRs, in order

Before PR 1, Miguel tags v0.1.0 and makes the GHCR package public, as [deploy.md](deploy.md#releasing) says. That's a release step, not a PR. After that, the manual no-clone steps work.

| PR | Needs |
| --- | --- |
| 0 This plan | nothing |
| 1 Versions and the WSL guard | 0 |
| 2 `agent-memory setup` | 1 |
| 3 PyPI | 2 |
| 4a The tap repo | 3 |
| 4b Homebrew in the release | 4a |
| 5 `install.sh` and the GitHub release | 3 |
| v0.2.0 release | 2–5 |

PRs 4 and 5 don't depend on each other.

### PR 0: This plan

- **Scope:** `docs/distribution-plan.md`.
- **Tests:** none.
- **Migration:** none.

### PR 1: Versions and the WSL guard

- **Scope:**
  - `agent-memory --version` prints the CLI's version, from `importlib.metadata`, or from `pyproject.toml` when it runs from a clone without an install.
  - `/v1/health` gains `version`. `docs/server.md` documents the field.
  - A native-Windows guard. On native Windows (`os.name == "nt"`), `main()` exits 2 before it does anything else: "agent-memory runs on macOS, Linux and WSL 2. On Windows, install it inside WSL: <docs link>".
  - `outbox.py` imports `fcntl` lazily, as `local_state.py` does, so that the guard, not an `ImportError`, is what a Windows user sees.
  - `install` refuses to write hooks from an ephemeral run, such as `uvx` or `pipx run`. That's when the running command lives under uv's cache (`UV_CACHE_DIR`, or `uv cache dir`'s default) or pipx's cache. The hooks would point at a path uv may prune. The message says to install the CLI first.
  - `docs/deploy.md` gains a "Windows (WSL 2)" section:
    - Install the CLI inside WSL, and use Docker Desktop's WSL integration or Docker Engine in WSL.
    - The editors must run in WSL too, for example Claude Code or Codex in a WSL shell, or Cursor or VS Code attached to WSL. A hook in the Windows-side `%USERPROFILE%\.cursor` can't run a CLI that lives in WSL.
- **Tests:**
  - `--version` matches `pyproject.toml`.
  - `/v1/health` reports the package version (server suite).
  - With `os.name` patched to `"nt"`, `main()` exits 2 with the WSL message and writes nothing.
  - `outbox` imports with `fcntl` hidden.
  - `install` with `sys.argv[0]` under a fake uv cache refuses, writes nothing, and says why. Under a uv tool bin, it still installs as today.
- **Migration:** none.

### PR 2: `agent-memory setup`

- **Scope:**
  - The command, as [described above](#agent-memory-setup), in a new module `scripts/server_setup.py`, with a thin `setup` subcommand in `agent_memory.py`.
  - Docker runs through one injectable runner, so the tests need no Docker.
  - The packaged `compose.yaml`: a new package, `scripts/agent_memory_assets/` (an `__init__.py` and `compose.yaml`), listed in `pyproject.toml` with `package-data`.
  - `scripts/release-check` checks that copy's pin too, and that it is byte-for-byte the root `compose.yaml`. The root file stays where it is, because `deploy.md`'s download URLs and `scripts/setup` use it.
  - The version warning in `setup` and `review`.
  - Docs:
    - In the README, "Install" and "Quick start" lead with `agent-memory setup` for a tool install. The clone path stays as the second option.
    - In `deploy.md`, the no-clone section becomes `uv tool install git+…`, then `agent-memory setup`. The manual steps stay as "Without `setup`" for people who want their own folder layout.
    - `deploy.md` also gains the [Upgrades](#upgrades) table.
- **Tests** (each with a fake Docker runner, a temporary `HOME` and `XDG_DATA_HOME`, and `stub_server.py` or a fake for `/v1/health`):
  - A first run creates the folder with mode 0700, `compose.yaml` equal to the packaged one, `data/server`, `data/qdrant`, and a `.env` with mode 0600 and the uid and gid. It runs `compose version`, `pull` and `up -d --no-build` in that order, with `-p agent-memory-server --project-directory <dir>`.
  - It writes the server config when it's missing, and leaves an existing one alone, printing it.
  - A rerun keeps `.env` lines and `data/`, replaces a `compose.yaml` that `setup` wrote, and backs up a hand-edited one.
  - A downgrade is refused without `--force` and goes ahead with it.
  - `--dir` is remembered, and a rerun without it uses the same folder.
  - Port 8765 already answering for another project → exit 1, and no compose call after the check.
  - A health timeout → exit 1, with the logs command run.
  - Each of the three Docker failures (`docker` missing, `compose` missing, daemon down) → its own message, and nothing written.
  - The editor offer: without a terminal, only printed. In a terminal, `n` runs nothing and `y` calls `connect_editors` with the detected editors.
  - Nothing under `~/.claude`, `~/.cursor`, `~/.codex` or `~/.pi` is created or changed, whatever the answers.
  - The packaged `compose.yaml` equals the root one, and `release-check` fails when they differ.
  - CI, in `test.yml`'s `release-image` job: install the wheel, then run `agent-memory setup` against the image that job builds, through `AGENT_MEMORY_IMAGE`, which compose reads from the environment. Then check `/v1/health`, the config file, and that a second `setup` leaves `memories.sqlite` in place.
- **Migration:** none. A clone keeps `./scripts/setup`. Anyone who followed the manual no-clone steps can run `agent-memory setup --dir <that folder>`. `setup` keeps the `.env` and `data/` there, saves the downloaded `compose.yaml` as `.bak`, and switches the containers to the `agent-memory-server` project. Those containers already carried that name if the folder was called `agent-memory-server`, as in `deploy.md`.

### PR 3: PyPI

- **Scope:**
  - **Before the PR**, Miguel registers a pending publisher on PyPI for `agent-memory` (`roune/agent-memory`, `release.yml`, environment `pypi`), and makes a `pypi` environment in the repo's settings. The PR description says whether PyPI accepted the name.
  - **If PyPI refuses the name:**
    - `[project] name` becomes `roune-agent-memory`, followed by `uv lock`. The command stays `agent-memory`.
    - Every install hint and doc that names the package changes with it: `agent_memory_server/__init__.py`'s "needs the server extra" message, `hook_bin`'s error, the README and `deploy.md`.
  - **`pyproject.toml`:**
    - `[project.urls]`: Homepage, Source, Issues and Documentation.
    - Classifiers: `Operating System :: MacOS`, `POSIX :: Linux`, `Environment :: Console`, and `License :: Other/Proprietary License`, with the SPDX `license` kept.
    - `keywords`.
  - **The README on PyPI.** PyPI doesn't resolve relative links. The hero image and the doc links become absolute `https://github.com/roune/agent-memory/blob/main/…` URLs, or the long description becomes a short `docs/pypi.md` that links to the README. The PR picks one and says why.
  - **`release.yml`:**
    - A `build` job runs `uv build` and uploads `dist/` as an artifact.
    - A `pypi` job (`needs: [build, verify]`, `environment: pypi`, `id-token: write` on that job only) downloads `dist/` and runs `pypa/gh-action-pypi-publish`, pinned to a commit SHA.
    - It runs after `verify`, so a release whose image failed never reaches PyPI. An upload can't be taken back.
  - **`test.yml`.** The `package` job also runs `uvx twine check --strict dist/*`, so metadata or README problems fail on a PR, not on the tag.
  - **Docs** in the README and `deploy.md`:
    - `uv tool install <name>` first, then `pipx install <name>`.
    - Trying it without installing: `uvx --from <name> agent-memory setup`. The docs say `install` refuses such a run (PR 1), so install the CLI before connecting hooks.
    - mise: `mise use -g pipx:<name>`. mise installs PyPI tools through its pipx backend, which uses uv when uv is present.
    - The server-extra hint becomes `uv tool install '<name>[server]'`.
    - `deploy.md`'s "Releasing" names the one-time pending-publisher and environment setup.
- **Tests:**
  - `twine check` passes in CI.
  - The wheel contains `agent_memory_assets/compose.yaml`.
  - A test reads every install hint string, and checks that it names the distribution from `pyproject.toml`.
  - `actionlint` passes on `release.yml`, run in the PR. The workflow runs only on a tag, so its first real run is the v0.2.0 tag, as with the GHCR job.
- **Migration:** a git install named `agent-memory` upgrades as before if the name was accepted. If the name became `roune-agent-memory`:
  1. `uv tool uninstall agent-memory`
  2. `uv tool install roune-agent-memory`
  3. `agent-memory install`

  uv won't put a second tool's `agent-memory` over the first. The hook path, `~/.local/bin/agent-memory`, stays the same.

### PR 4a: The tap repo (`roune/homebrew-tap`)

- **Scope:** a new repo, made with `brew tap-new roune/homebrew-tap`, with its default bottle workflows. Miguel creates the repo, and this PR fills it.
  - **`Formula/agent-memory.rb`:**
    - `url` points at the PyPI sdist of the release, with its `sha256`.
    - `license "PolyForm-Noncommercial-1.0.0"`.
    - `include Language::Python::Virtualenv`, `depends_on "python@3.13"` (or whichever Python is current in Homebrew then), and `virtualenv_install_with_resources`.
    - No `resource` blocks, unless `brew install --build-from-source` shows that setuptools has to be one.
  - **`caveats`:**
    - `agent-memory setup` starts the server, and it needs Docker: Docker Desktop, OrbStack or colima. Docker isn't a formula dependency, because the cask can't be one, and people pick their own runtime.
    - `agent-memory install` connects editor hooks, after a yes.
  - **No `post_install`.** The formula writes only inside Homebrew's prefix.
  - **A `test do` block**, run with `HOME=testpath`:
    - `agent-memory --version` names the formula's version, and `agent-memory --help` runs.
    - `testpath/".claude"` is still absent afterwards.
- **Tests:**
  - The tap's CI runs `brew audit --strict --online`, `brew install --build-from-source` and `brew test`, on macOS (arm64) and Linux.
  - A one-off check, written up in the PR: after `brew install`, `agent-memory install --yes` in a scratch `HOME` writes hooks that run `$(brew --prefix)/bin/agent-memory`, not a `Cellar/` path.
- **Migration:** none.

### PR 4b: Homebrew in the release

- **Scope:**
  - `release.yml` gains a `homebrew` job, after `pypi`. It waits for the sdist to show up on PyPI, then opens a PR on `roune/homebrew-tap` that sets `url` and `sha256`. The job is skipped if the secret is unset.
  - The job pushes with a fine-grained token that can write only to the tap repo, kept in the secret `HOMEBREW_TAP_TOKEN`.
  - Miguel merges the PR with `brew pr-pull`, so bottles are published.
  - The README and `deploy.md` list `brew install roune/tap/agent-memory`, and `brew trust` if Homebrew asks for it.
  - "Releasing" gains the token setup, and the manual fallback: `brew bump-formula-pr --url <sdist> roune/tap/agent-memory`.
- **Tests:** the URL and sha256 edit is a small script with a unit test (a sample formula in, the edited formula out). The job itself first runs on the v0.2.0 tag.
- **Migration:** none.

### PR 5: `install.sh` and the GitHub release

- **Scope:**
  - **`scripts/install.sh`**, POSIX sh, for people who have neither uv nor Homebrew:
    1. On native Windows (`MINGW*`, `MSYS*`, `CYGWIN*`), it prints the WSL instructions and exits 1.
    2. If `uv` is missing, it asks before it installs uv with uv's own installer (`https://astral.sh/uv/install.sh`). `--yes` skips the question. Without a terminal and without `--yes`, it prints the uv install command and exits 1.
    3. It runs `uv tool install '<name>==<version>'`, with the version filled in when the release is built.
    4. It prints the next steps: `agent-memory setup`, then `agent-memory install`, and the `PATH` hint if uv's bin folder isn't on `PATH`.
    5. It never runs `setup` or `install`, and never writes outside uv's own folders.
  - **`release.yml`** gains a `github-release` job after `pypi`, with `contents: write` on that job only. It creates the GitHub release for the tag, with `install.sh`, `compose.yaml` and `compose.team.yaml` attached. Step 7 of "Releasing" becomes automatic.
  - **Docs:** `curl -LsSf https://github.com/roune/agent-memory/releases/latest/download/install.sh | sh`, with a line on reading the script first.
- **Tests:**
  - `shellcheck` on the script in CI.
  - A test runs it with a fake `uv` on `PATH` that records its arguments. It checks the pinned install, that a missing `uv` without `--yes` prints and exits 1, the Windows refusal, and that nothing under a scratch `HOME` changes except what the fake uv writes.
- **Migration:** none.

### The v0.2.0 release

Once PRs 1 to 5 are merged:

1. Bump the version to 0.2.0 in `pyproject.toml` and the compose files. `release-check` now covers the packaged `compose.yaml` as well.
2. Tag the merge commit and push the tag.
3. The workflow publishes the image, verifies it, uploads to PyPI, opens the tap PR, and creates the GitHub release.
4. Check it as a stranger would, in a scratch `HOME` on macOS and in WSL:
   1. `brew install roune/tap/agent-memory`, `agent-memory setup` and `agent-memory install`.
   2. The same with `uv tool install <name>`.

## Out of this plan

- **Native Windows.** The table under [POSIX-only code](#posix-only-code) is the starting point for a later plan.
- **Scoop and winget**, which need native Windows.
- **homebrew-core**, because of the license.
- **Nix, AUR and conda-forge.** They're left to the community.
- **A team server through `setup`.** `compose.team.yaml` stays in the docs.
- **Server subcommands** (`status`, `logs`, `stop`). `setup` prints the compose commands for now.
- **A guided `init`** that runs `setup`, `install` and `connect` together.
- **Upgrading the server automatically.** An upgrade is always a `setup` the person runs.
- **Moving a clone's `data/` into `setup`'s folder.** Nobody but Miguel runs a clone-based server.

## Risks to watch

- **The PyPI name.** A pending publisher doesn't reserve `agent-memory`. Register it just before the first upload. If PyPI refuses it, PR 3 switches to `roune-agent-memory` before anything is published.
- **The Redis package on the same `PATH`.** `pip install agent-memory-server` (Redis) also installs a command named `agent-memory`, and a package `agent_memory_server`.
  - Isolated installs keep the packages apart, but only one `agent-memory` command can be first on `PATH`.
  - The docs recommend uv tool, pipx or Homebrew, never a plain `pip install` into a shared environment.
  - Our flat module names (`outbox`, `editors`, `embed`, …) have the same problem in a shared environment. Moving them into one package is a larger change, and isn't part of this plan.
- **PyPI is permanent.** A version can't be uploaded twice. The `pypi` job runs only after the image is verified, and `twine check` runs on every PR.
- **Docker isn't a package dependency.** `setup` is where a missing or stopped Docker shows up, so its three checks print exact next steps.
- **Port 8765.** A clone's server and `setup`'s server can't both run. `setup` checks first and changes nothing when the port is taken.
- **WSL and editors on the Windows side.** Hooks written by a CLI in WSL live in WSL's home. An editor running on the Windows side doesn't read them. The WSL docs say so, rather than claim it works.
- **Tap trust.** Newer Homebrew versions may ask people to trust a third-party tap. The docs give the command.
