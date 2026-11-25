# Distribution plan

Status: approved by Miguel on 24 November 2026, after his interview answers the same day and an Opus review of this plan. Each PR below says what it builds.

Amended on 25 November 2026, while the PRs were built:

- **Homebrew is tested in CI, not on a laptop.** Miguel chose this. The formula's source is a template in this repo, `packaging/homebrew/agent-memory.rb`. A macOS CI job installs it from a locally built sdist on every PR. The release job renders it and opens the bump PR on the tap. So PR 4a holds only the tap's scaffolding, and the manual fallback renders with `scripts/homebrew-formula` instead of `brew bump-formula-pr`.
- **mise works with `mise activate`, not with shims.** Shims are refused by PR 1's install check. A later release could accept them.
- **The version warning names the right fix.** A newer server says to upgrade the CLI. A remote server is named without the `setup` advice.

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
| PyPI name | **Try `agent-memory`, fall back to `roune-agent-memory`.** PyPI's own code settles the try: it refuses `agent-memory` (see [Names](#names)). So the package is **`roune-agent-memory`**, and the command stays `agent-memory`. PR 1 renames the distribution, because it's the first PR that reads the name. |
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

- **PyPI.** There's no project called `agent-memory`, but PyPI refuses a new name that is "too similar to an existing project" ([choosing a name](https://docs.pypi.org/project-management/choosing-a-name/)).
  - The rule is in Warehouse's code. `ProjectService` in `warehouse/packaging/services.py` raises `ProjectNameUnavailableSimilarError` when `ultranormalize_name(new) == ultranormalize_name(existing)`. Pending publishers are matched by the same function.
  - `ultranormalize_name` drops `.`, `_` and `-`, and maps `l` and `i` to `1` and `o` to `0`. That makes `agent-memory` the same name as `agentmemory` (v0.4.8, 2023), so it's refused.
- **Names that are taken:** `agentmemory`, `agentmem`, `agent-memory-cli` (another coding-agent memory tool, from November 2026), `agent-memory-kit`, `agent-recall`, and `agent-memory-server`, which is [redis/agent-memory-server](https://github.com/redis/agent-memory-server).
- **The Redis package clashes in two places.** It installs a console script named `agent-memory`, and a top-level Python package named `agent_memory_server`. Both are the same names as ours. Isolated installs (uv tool, pipx, Homebrew) keep them apart, but two tools on one `PATH` don't. See [Risks](#risks-to-watch).
- **Names that are free:** `roune-agent-memory`, `kept-memory` and `agent-keep` have no project. `agent-mem` isn't free, because it's the same name as the taken `agentmem` under the rule above.
- **Homebrew.** There's no `agent-memory` formula or cask. It has to be a tap anyway: homebrew-core takes only licenses compatible with the Debian Free Software Guidelines ([Acceptable Formulae](https://docs.brew.sh/Acceptable-Formulae)), and PolyForm Noncommercial 1.0.0 restricts commercial use.
- **winget and Scoop Main.** Neither has an `agent-memory` package. Both are out of this plan.

### POSIX-only code

The server runs in a Linux container, so its own `fcntl` lock (`agent_memory_server/lock.py`) is fine. The CLI has these POSIX-only parts:

| Where | What | On native Windows |
| --- | --- | --- |
| `outbox.py:35` | `import fcntl`, no guard; `flock` around the queue | the CLI can't start, because `agent_memory.py` imports `outbox` |
| `local_state.py:37` | `fcntl`, guarded | the state move runs unlocked |
| `agent_memory.py` `review_alive` | `os.kill(pid, 0)` to check that the review page lives | `0` is `CTRL_C_EVENT` there, so it sends Ctrl-C to that process group instead of checking it |
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
uv tool install roune-agent-memory         │  the CLI on PATH, and the next steps printed.
pipx install roune-agent-memory            │  Nothing else is written.
curl -LsSf …/install.sh | sh               ┘
        │
agent-memory setup     # the server in Docker: ~/.local/share/agent-memory/server/{compose.yaml,.env,data/}
        │              # waits for /v1/health, writes ~/.config/agent-memory/server, offers connect --editor
agent-memory install   # editor hooks, after a yes (unchanged)
agent-memory connect --editor claude-code   # if setup's offer was declined
```

The Homebrew formula keeps the name `agent-memory`, because the tap's own name keeps it apart from other formulae. A clone keeps `./scripts/install` and `./scripts/setup`, which build the image from source.

## `agent-memory setup`

`setup` does what `scripts/setup` does, for a tool install. It reads and checks everything first and writes nothing until every check has passed. A refusal leaves the folder exactly as it was.

**Checks, with no writes:**

1. **Check Docker.** It needs `docker` on `PATH`, a working `docker compose version`, and a `docker info` that reaches the daemon. Each failure prints one line saying what to install or start (Docker Desktop, OrbStack, colima, or Docker Engine in WSL), then exits 1.
2. **Pick the folder.**
   - The default is `${XDG_DATA_HOME:-~/.local/share}/agent-memory/server/`.
   - `--dir <path>` picks another. `~/.config/agent-memory/setup` (`dir = …`) records it, so a rerun without `--dir` finds it.
   - `AGENT_MEMORY_DIR` doesn't move this folder, because that variable is for the CLI's own state.
3. **Read what's there.** `setup` keeps a sidecar, `<dir>/.setup.json`, with the version and the sha256 of the `compose.yaml` it last wrote. From the sidecar and the current `compose.yaml` it learns:
   - whether `setup` wrote that `compose.yaml`: the file's sha256 equals the sidecar's;
   - which image it pins, parsed with the regex `scripts/release-check` uses.
4. **Refuse a downgrade.** If the pinned version is newer than this CLI's, `setup` stops and says to upgrade the CLI. `--force` overrides it.
   - Versions compare as tuples of ints, so `packaging` isn't needed.
   - A pin it can't parse, such as a custom `AGENT_MEMORY_IMAGE` tag, counts as "unknown". `setup` says so and goes on.
   - A newer server may have written data an older image can't read.
5. **Check that port 8765 is free, and who holds it.**
   1. It lists the running compose projects (`docker compose ls --format json`), each with its config files.
   2. If a project called `agent-memory-server` runs from another folder, `setup` refuses. One way that happens is the manual steps in `deploy.md`, which use a folder of that name; another is a team host. `setup` names that folder and says to pass `--dir <it>`, or to stop that project. Otherwise `up` would recreate those containers on an empty `data/`, or switch a team server's auth off.
   3. If no project of ours runs but `127.0.0.1:8765/v1/health` answers, another server holds the port, most likely a clone's `./scripts/setup`. `setup` names the command that stops it (`docker compose down` in that clone).
   4. Both cases exit 1.

**Writes, only after every check has passed:**

6. **Write `compose.yaml`** from the copy inside the package (`importlib.resources`). That copy is byte-for-byte the repo's `compose.yaml`, pinned to this version's image.
   - `setup` overwrites a file it wrote itself, as step 3 found.
   - Any other file is saved first as `compose.yaml.<timestamp>.bak`, and `setup` says so. Settings belong in `.env`, such as `AGENT_MEMORY_IMAGE` or `AGENT_MEMORY_EMBEDDER`.
   - Then it updates `.setup.json`.
7. **Create the folders.** The server folder gets mode 0700, then `data/server` and `data/qdrant`. They must exist before Docker starts, because on Linux Docker would otherwise create them owned by root.
8. **Write `.env`** with mode 0600. It adds `AGENT_MEMORY_UID` and `AGENT_MEMORY_GID` from `os.getuid()` and `os.getgid()` when they're missing, and keeps every other line.

**Start, then connect:**

9. **Pull and start.** It runs `docker compose -p agent-memory-server -f <dir>/compose.yaml --project-directory <dir> pull`, then `up -d --no-build`.
   - The fixed project name keeps `setup`'s containers apart from a clone's, whose project is named after the clone folder.
   - `-f` stops compose from searching parent folders for another file.
   - `--no-build` matters because the packaged file keeps `build: .` for clones, and this folder has no Dockerfile.
   - **Failures:**
     - `setup` checks each exit code. After `pull` it runs `docker image inspect` on the pinned image, because compose can warn, not fail, on a pull error for a service that also has `build:`.
     - A missing image prints one line: the image name, and that GHCR may be unreachable or the package not public yet.
     - A failed bind mount, as with snap Docker or a folder Docker Desktop doesn't share, prints one line that suggests `--dir` with a visible folder (see [Risks](#risks-to-watch)).
10. **Wait for `/v1/health`** for up to 120 seconds. On a timeout it prints the last 40 lines of the `server` and `qdrant` logs, as `scripts/setup` does, and exits 1.
11. **Write the CLI's server config**, `~/.config/agent-memory/server` with `url = http://127.0.0.1:8765`, if it's missing. It calls `server_client.write_config(url, None, server_client.legacy_mode())`, as `scripts/setup` does. If the file exists and names another URL, `setup` leaves it alone and prints that URL and the `connect --url` command.
12. **Offer the editors, then print the next steps.**
    - It prints `agent-memory install` (hooks, after a yes) and `agent-memory connect --editor <found>`. `<found>` is what `present_tools()` detects, minus `editors.NO_MCP` (Pi).
    - It asks once, `Add MCP to claude-code, cursor? [y/N]`, only when:
      - it runs in a terminal,
      - `--no-input` isn't set,
      - something was found, and
      - the CLI's server config names this server.
    - When the config names another server, it only prints, because `connect_editors` would register that other server.
    - A yes runs `connect --editor`, which shows its diff and asks again.
    - `setup` never writes editor files itself.

`setup` is safe to rerun, and rerunning is the upgrade step. It never runs `docker compose down -v` and never deletes `data/`.

`setup --print-dir` prints the folder. The final message names the compose commands for logs and for stopping (`docker compose -p agent-memory-server -f <dir>/compose.yaml logs`, and `down`). Server subcommands such as `status`, `logs` and `stop` are a later choice (see [Out of this plan](#out-of-this-plan)).

On native Windows `setup` never runs, because of PR 1's guard. In WSL it takes the Linux path.

## Upgrades

| Install | Upgrade the CLI | Then |
| --- | --- | --- |
| Homebrew | `brew upgrade agent-memory` | `agent-memory setup` |
| PyPI with uv | `uv tool upgrade roune-agent-memory` | `agent-memory setup` |
| PyPI with pipx | `pipx upgrade roune-agent-memory` | `agent-memory setup` |
| mise | `mise upgrade pipx:roune-agent-memory` | `agent-memory install`, then `agent-memory setup` |
| `install.sh` | run it again | `agent-memory setup` |
| uv from git | `uv tool upgrade roune-agent-memory` | `agent-memory setup` |
| a clone | `git pull` | `./scripts/setup` |

- **What the warning says.** `/v1/health` gains `"version": "<x.y.z>"`, read from the package metadata in the image. `setup` and `review` compare it with the CLI's own version. When the two differ, they print one line: "the server runs 0.2.0 and this CLI is 0.3.0: run `agent-memory setup` to upgrade the server". A server with no `version` field is older than 0.2.0.
- **Hooks stay quiet.** `capture`, `git-hook` and `sync --quiet` never warn, because they must stay fast and silent.
- **The hooks keep working after an upgrade.** They run the command as it was called, not the resolved path ([deploy.md](deploy.md#git-hooks-in-a-folder-cursor-manages)).
  - Homebrew's `$(brew --prefix)/bin/agent-memory` and uv's and pipx's bin links stay put across an upgrade. PR 4a tests the Homebrew case, where the real file sits under the versioned `Cellar/`.
  - mise is the exception. With `mise activate`, `PATH` holds a versioned install folder (`…/mise/installs/pipx-roune-agent-memory/<version>/bin`), so after a mise upgrade, run `agent-memory install` again to point the hooks at the new version. The docs say this, and suggest mise's shims folder as a `PATH` entry that doesn't move. That folder layout still has to be checked against mise's docs in PR 3.
- **Switching channels**, for example from the git install to Homebrew:
  1. Uninstall the old one (`uv tool uninstall …`).
  2. Install the new one.
  3. Run `agent-memory install` once, so that the hooks it wrote point at the new command. It already rewrites only its own marked entries.

  The inbox, outbox and config live in the XDG folders, so they stay where they are.

## PRs, in order

Before PR 1, Miguel does two things. Neither is a PR.

- **Release v0.1.0.** Tag v0.1.0 and make the GHCR package public, as [deploy.md](deploy.md#releasing) says. The manual no-clone steps then work.
- **Claim the PyPI name.**
  - Register a pending publisher for `roune-agent-memory` (`roune/agent-memory`, `release.yml`, environment `pypi`), and create the `pypi` environment in the repo settings.
  - A pending publisher doesn't reserve the name. Registering it early still means PyPI's form checks the name now, rather than at the v0.2.0 tag.
  - If the form refuses `roune-agent-memory`, pick another name before PR 1. The plan's commands change with it.

| PR | Needs |
| --- | --- |
| 0 This plan | nothing |
| 1 The name, versions and the WSL guard | 0 |
| 2a The packaged `compose.yaml` | 1 |
| 2b `agent-memory setup` | 2a |
| 2c Setup docs, the version warning, and CI end to end | 2b |
| 3 PyPI | 2c |
| 4a The tap repo | 3 |
| 4b Homebrew in the release | 4a |
| 5 `install.sh` and the GitHub release | 3 |
| v0.2.0 release | 2a–5 |

PR 4 and PR 5 don't depend on each other. PRs 3, 4b and 5 all edit `release.yml`, so the later ones rebase.

### PR 0: This plan

- **Scope:** `docs/distribution-plan.md`, linked from the README's "Where to read next" and from `AGENTS.md`.
- **Tests:** none.
- **Migration:** none.

### PR 1: The name, versions and the WSL guard

- **Scope:**
  - **The distribution name.** `[project] name = "roune-agent-memory"`, then `uv lock`. One constant, `DIST`, in `agent_memory.py`, used for every `importlib.metadata` lookup and every install hint:
    - `agent_memory_server/__init__.py`'s "needs the server extra" message;
    - `hook_bin`'s error;
    - the README's and `deploy.md`'s `uv tool install git+…` lines, which keep working with the new name.
  - **`agent-memory --version`** prints the CLI's version: `importlib.metadata.version(DIST)`, or `pyproject.toml` when it runs from a clone without an install.
  - **`/v1/health` gains `version`**, from the same lookup. `docs/server.md` documents the field.
  - **A native-Windows guard.**
    - On native Windows (`os.name == "nt"`), `main()` exits 2 before anything else: "agent-memory runs on macOS, Linux and WSL 2. On Windows, install it inside WSL: <docs link>".
    - `outbox.py`'s `fcntl` import gets the same `try`/`except ImportError` guard as `local_state.py`. Then the guard, not an `ImportError`, is what a Windows user sees.
  - **`install` refuses to write hooks from a run that doesn't last.**
    - It refuses unless the running command is the `agent-memory` on `PATH`: `shutil.which("agent-memory")` and `sys.argv[0]` must name the same file (`os.path.samefile`). A run from the clone's `bin/agent-memory` is also allowed.
    - That covers `uvx`, `uvx --no-cache` and `pipx run` without knowing their cache paths. The message says to install the CLI first.
  - **`docs/deploy.md` gains a "Windows (WSL 2)" section.**
    - Install the CLI inside WSL. Docker comes from Docker Desktop's WSL integration or from Docker Engine in WSL.
    - The editors have to run in WSL too, such as Claude Code or Codex in a WSL shell, or Cursor or VS Code attached to WSL. A hook in the Windows-side `%USERPROFILE%\.cursor` can't run a CLI that lives in WSL.
- **Tests:**
  - `--version` matches `pyproject.toml`.
  - `DIST` equals `[project] name`.
  - Every install hint names `DIST`.
  - `/v1/health` reports the package version (server suite).
  - With `os.name` patched to `"nt"`, `main()` exits 2 with the WSL message and writes nothing.
  - `outbox` imports with `fcntl` hidden.
  - `install` refuses when run from a copy that isn't the one on `PATH` (a temp folder), writes nothing, and says why. From the tool bin on `PATH`, and from the clone's `BIN`, it installs as it does today.
- **Migration:** only Miguel has a git install today.
  - uv names a tool after its distribution, so the old install is still called `agent-memory`. To switch:
    1. `uv tool uninstall agent-memory`
    2. `uv tool install git+https://github.com/roune/agent-memory`, which is now `roune-agent-memory`
    3. `agent-memory install`
  - A clone's editable install changes in the same way through `./scripts/install --force`.
  - The hook path, `~/.local/bin/agent-memory`, doesn't change.

### PR 2a: The packaged `compose.yaml`

- **Scope:**
  - A new package, `scripts/agent_memory_assets/` (`__init__.py` and `compose.yaml`), listed in `pyproject.toml` with `package-data`.
  - `scripts/release-check` checks that copy's pin as well, and that it's byte-for-byte the root `compose.yaml`. The root file stays where it is, because `deploy.md`'s download URLs and `scripts/setup` use it.
- **Tests:**
  - The packaged copy equals the root file.
  - `release-check` fails when they differ, or when the copy pins another version.
  - In `test.yml`'s `package` job, the built wheel contains `agent_memory_assets/compose.yaml`, and `importlib.resources` reads it from an installed wheel.
- **Migration:** none.

### PR 2b: `agent-memory setup`

- **Scope:**
  - The command as [described above](#agent-memory-setup), in a new module, `scripts/server_setup.py`, with a thin `setup` subcommand in `agent_memory.py`.
  - Docker calls go through one injectable runner, so the tests need no Docker.
  - Its `--help`. Nothing else changes in the docs yet.
- **Tests.** Each uses a fake Docker runner, a temporary `HOME` and `XDG_DATA_HOME`, and `stub_server.py` or a fake for `/v1/health`:
  - **A first run:**
    - It creates the folder with mode 0700, a `compose.yaml` equal to the packaged one, `.setup.json`, `data/server`, `data/qdrant`, and a `.env` with mode 0600 and the uid and gid.
    - It runs `compose version`, `ls`, `pull`, `image inspect` and `up -d --no-build`, in that order, with `-p agent-memory-server -f <dir>/compose.yaml --project-directory <dir>`.
  - **Config:** it writes the server config, with `legacy_mode()`, when the config is missing, and leaves an existing one alone, printing it.
  - **A rerun:**
    - It keeps the `.env` lines and `data/`.
    - It replaces a `compose.yaml` whose hash matches `.setup.json`.
    - It backs up a hand-edited `compose.yaml`, and a byte-identical file downloaded by hand when there's no sidecar.
  - **Downgrade:** refused without `--force`, and it goes ahead with it. An unparseable pin is reported, and setup goes on.
  - **`--dir`** is remembered, and a rerun without it uses the same folder.
  - **Refusals change nothing.** The folder tree is the same before and after, and no compose call follows the check, for each of these cases:
    - the `agent-memory-server` project runs from another folder;
    - port 8765 answers for another project;
    - a downgrade;
    - each Docker failure.
  - **Pull failures:** a failed `pull`, or a missing image after `pull`, exits 1 with the image name, and `up` isn't run.
  - **A health timeout** exits 1, and the logs command runs.
  - **The editor offer.** `connect_editors` is faked, and these cases are covered:
    - without a terminal, setup only prints;
    - in a terminal, `n` runs nothing;
    - `y` calls `connect_editors` with the editors it found, minus Pi;
    - a server config that names another URL means no question at all.
  - **Editor files stay untouched.** Nothing under `~/.claude`, `~/.claude.json`, `~/.cursor`, `~/.codex` or `~/.pi` is created or changed, whatever the answers. The real `connect` is faked away.
- **Migration:** none. A clone keeps `./scripts/setup`. Anyone who followed the manual no-clone steps runs `agent-memory setup --dir <that folder>`.
  - `setup` keeps the `.env` and `data/` there.
  - It saves the downloaded `compose.yaml` as `.bak`, since it has no sidecar.
  - It runs the containers as the `agent-memory-server` project. A folder called `agent-memory-server`, as in `deploy.md`, already had that project name.

### PR 2c: Setup docs, the version warning, and CI end to end

- **Scope:**
  - The version warning in `setup` and `review`.
  - `scripts/setup`: when port 8765 is already taken, it names the `agent-memory-server` project and the command that stops it, instead of Docker's "port is already allocated".
  - **README:** "Install" and "Quick start" lead with `agent-memory setup` for a tool install, and the clone path stays as the second option.
  - **`deploy.md`:**
    - The no-clone section becomes `uv tool install git+…`, then `agent-memory setup`.
    - The manual steps stay, under "Without `setup`", for people who want their own folder layout.
    - It gains the [Upgrades](#upgrades) table.
  - **CI**, in `test.yml`'s `release-image` job:
    1. Add `astral-sh/setup-uv` and build the wheel.
    2. Install the wheel and run `agent-memory setup` against the image that job builds. `AGENT_MEMORY_IMAGE` selects it; compose reads it from the environment.
    3. Check `/v1/health`, the config file, and that a second `setup` leaves `memories.sqlite` in place.
- **Tests:**
  - A mismatched `version` from `/v1/health` gives the one-line warning in `setup` and `review`.
  - A missing `version` says the server is older than 0.2.0.
  - `capture`, `git-hook` and `sync --quiet` never make the extra call.
  - The CI job above.
- **Migration:** none.

### PR 3: PyPI

- **Scope:**
  - **`pyproject.toml`:**
    - `[project.urls]`: Homepage, Source, Issues and Documentation.
    - Classifiers: `Operating System :: MacOS`, `Operating System :: POSIX :: Linux` and `Environment :: Console`.
    - `keywords`.
    - No `License ::` classifier. setuptools 77 and later refuse one next to the SPDX `license` expression, which stays.
  - **The README on PyPI.** PyPI doesn't resolve relative links. Either the hero image and the doc links become absolute `https://github.com/roune/agent-memory/blob/main/…` URLs, or the long description becomes a short `docs/pypi.md` that links to the README. The PR picks one and says why.
  - **`release.yml`:**
    - A `build` job runs `uv build` and uploads `dist/` as an artifact.
    - A `pypi` job (`needs: [build, verify]`, `environment: pypi`, `id-token: write` on that job only) downloads `dist/` and runs `pypa/gh-action-pypi-publish`, pinned to a commit SHA.
    - `pypi` runs after `verify`, so a release whose image failed never reaches PyPI. An upload can't be taken back.
  - **`test.yml`:** the `package` job also runs `uvx twine check --strict dist/*`, so metadata or README problems fail on a PR, not on the tag.
  - **Docs**, in the README and `deploy.md`:
    - `uv tool install roune-agent-memory` first, then `pipx install roune-agent-memory`.
    - Trying it without installing: `uvx --from roune-agent-memory agent-memory setup`. `install` refuses such a run (PR 1), so install the CLI before connecting hooks.
    - mise: `mise use -g pipx:roune-agent-memory`. mise's pipx backend uses uv when uv is installed. The mise upgrade note from [Upgrades](#upgrades) goes here too.
    - The server-extra hint becomes `uv tool install 'roune-agent-memory[server]'`.
    - `deploy.md`'s "Releasing" names the one-time pending publisher and the `pypi` environment.
- **Tests:**
  - `twine check` passes in CI.
  - `actionlint` passes on `release.yml`, run in the PR. The workflow runs only on a tag, so its first real run is the v0.2.0 tag, as with the GHCR job.
- **Migration:** none. The rename happened in PR 1.

### PR 4a: The tap repo (`roune/homebrew-tap`)

- **Scope:** a new repo, made with `brew tap-new roune/homebrew-tap`, with its default bottle workflows. Miguel creates the repo, and this PR fills it.
  - **`Formula/agent-memory.rb`:**
    - `url` is the PyPI sdist of the release, with its `sha256`.
    - `license "PolyForm-Noncommercial-1.0.0"`.
    - `include Language::Python::Virtualenv`, `depends_on "python@3.13"` (or whichever Python is current in Homebrew then), and `virtualenv_install_with_resources`.
    - Homebrew's Python 3.12+ ships no setuptools, so the build backend likely needs a `resource "setuptools"` or a `python-setuptools` build dependency. `brew install --build-from-source` decides which.
  - **`caveats`:**
    - `agent-memory setup` starts the server, and it needs Docker: Docker Desktop, OrbStack or colima. Docker isn't a formula dependency, because the cask can't be one, and people choose their own runtime.
    - `agent-memory install` connects editor hooks, after a yes.
  - **No `post_install`.** The formula writes only inside Homebrew's prefix.
  - **A `test do` block** with `HOME=testpath`:
    - `agent-memory --version` names the formula's version.
    - `agent-memory --help` runs.
    - `testpath/".claude"` is still absent afterwards.
- **Tests:**
  - The tap's CI runs `brew audit --strict --online`, `brew install --build-from-source` and `brew test`, on macOS arm64 and on Linux.
  - A one-off check, written up in the PR: after `brew install`, `agent-memory install --yes` in a scratch `HOME` writes hooks that run `$(brew --prefix)/bin/agent-memory`, not a `Cellar/` path.
- **Migration:** none.

### PR 4b: Homebrew in the release

- **Scope:**
  - **A `homebrew` job** in `release.yml`, after `pypi`. It waits for the sdist to show on PyPI, then opens a PR on `roune/homebrew-tap` that sets `url` and `sha256`.
    - It uses a fine-grained token that can write only to the tap repo, kept in the secret `HOMEBREW_TAP_TOKEN`. The job is skipped when the secret is unset.
    - Miguel merges that PR with `brew pr-pull`, so the bottles are published.
  - **Install docs:** the README and `deploy.md` list `brew install roune/tap/agent-memory`, and `brew trust` if Homebrew asks for it.
  - **"Releasing" gains:**
    - the token setup;
    - the manual fallback, `brew bump-formula-pr --url <sdist> roune/tap/agent-memory`.
- **Tests:** a small script edits the `url` and `sha256`, and a unit test checks it (a sample formula in, the edited formula out). The job itself first runs on the v0.2.0 tag.
- **Migration:** none.

### PR 5: `install.sh` and the GitHub release

- **Scope:**
  - **`scripts/install.sh`**, POSIX sh, for people who have neither uv nor Homebrew:
    1. **Native Windows** (`MINGW*`, `MSYS*`, `CYGWIN*`): it prints the WSL instructions and exits 1.
    2. **uv missing:**
       - It asks before installing uv. Under `curl | sh` stdin is the pipe, so it reads the answer from `/dev/tty` when `[ -r /dev/tty ]`.
       - `--yes` skips the question; the docs show it as `| sh -s -- --yes`.
       - With no terminal and no `--yes`, it prints the uv install command and exits 1.
       - It runs uv's own installer (`https://astral.sh/uv/install.sh`) with `UV_NO_MODIFY_PATH=1`, so that installer doesn't edit shell rc files. Whether that variable still works is checked against uv's installer docs in this PR.
    3. **The install:** `uv tool install 'roune-agent-memory==<version>'`, with the version filled in when the release is built.
    4. **Next steps:** it prints `agent-memory setup`, then `agent-memory install`, and the `export PATH=…` line if uv's bin folder isn't on `PATH`.
    5. **Limits:** it never runs `setup` or `install`, and it never writes outside uv's own folders.
  - **`release.yml`** gains a `github-release` job after `pypi`, with `contents: write` on that job only. It creates the GitHub release for the tag, with `install.sh`, `compose.yaml` and `compose.team.yaml` attached. Step 7 of "Releasing" becomes automatic.
  - **Docs:** `curl -LsSf https://github.com/roune/agent-memory/releases/latest/download/install.sh | sh`, with a line suggesting people read the script first.
- **Tests:**
  - `shellcheck` on the script, in CI.
  - A test runs the script with a fake `uv` on `PATH` that records its arguments, and a fake `/dev/tty` answer where the shell allows one. It checks:
    - the pinned install;
    - that a missing `uv` without `--yes` and without a terminal prints and exits 1;
    - that `--yes` runs the uv installer with `UV_NO_MODIFY_PATH=1`;
    - the Windows refusal;
    - that nothing under a scratch `HOME` changes except what the fake uv writes.
- **Migration:** none.

### The v0.2.0 release

Once PRs 1 to 5 are merged:

1. Bump the version to 0.2.0 in `pyproject.toml` and the compose files. `release-check` covers the packaged `compose.yaml` now.
2. Tag the merge commit and push the tag.
3. The workflow then:
   1. publishes the image and verifies it;
   2. uploads to PyPI;
   3. opens the tap PR;
   4. creates the GitHub release.
4. Check it as a stranger would, in a scratch `HOME` on macOS and in WSL:
   - `brew install roune/tap/agent-memory`, then `agent-memory setup`, then `agent-memory install`;
   - the same with `uv tool install roune-agent-memory`.

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

- **The PyPI name.** A pending publisher doesn't reserve `roune-agent-memory`. Someone could register it, or a similar name, before the v0.2.0 upload. Registering the publisher before PR 1 lets PyPI's form check the name early. If it's taken by then, only the name constant and the docs change.
- **The Redis package on the same `PATH`.** `pip install agent-memory-server` (Redis) also installs a command named `agent-memory`, and a package `agent_memory_server`.
  - Isolated installs keep the packages apart, but only one `agent-memory` command can be first on `PATH`.
  - The docs recommend uv tool, pipx or Homebrew, never a plain `pip install` into a shared environment.
  - Our flat module names (`outbox`, `editors`, `embed`, …) have the same problem in a shared environment. Moving them into one package is a larger change, and isn't part of this plan.
- **PyPI is permanent.** A version can't be uploaded twice. The `pypi` job runs only after the image is verified, and `twine check` runs on every PR.
- **Docker isn't a package dependency.** `setup` is where a missing or stopped Docker shows up, so its three checks print exact next steps.
- **Port 8765, both ways.** A clone's server and `setup`'s server can't both run.
  - `setup` checks first and changes nothing when the port is taken.
  - The other way round, `./scripts/setup` in a clone fails with Docker's "port is already allocated" while `setup`'s server runs. PR 2c adds a line to `scripts/setup`: when port 8765 is taken, name the `agent-memory-server` project and the command that stops it.
- **The server folder is hidden, and Docker may not share it.**
  - Snap-installed Docker on Ubuntu can't bind-mount hidden folders under `$HOME`, so `~/.local/share/agent-memory/server/data` fails there.
  - Docker Desktop may not share a custom `XDG_DATA_HOME` outside its file-sharing list.
  - `setup` tells a failed mount apart, by its error text, from other `up` failures, and suggests `--dir` with a visible folder. The docs name both cases.
- **Skipping releases moves Qdrant several versions at once.** Qdrant may not read storage written more than one minor version back (unverified). If an upgrade leaves Qdrant unable to start:
  1. Move `data/qdrant` aside.
  2. Start again.
  3. Run `agent-memory-server reindex --rebuild` inside the server container. A plain `reindex` refuses, because `memories.sqlite` still marks every row as embedded. `memories.sqlite` is the source of truth, and Qdrant is derived from it.

  The Upgrades section in `deploy.md` gives those commands.
- **WSL and editors on the Windows side.** Hooks written by a CLI in WSL live in WSL's home. An editor running on the Windows side doesn't read them. The WSL docs say so, rather than claim it works.
- **Tap trust.** Newer Homebrew versions may ask people to trust a third-party tap. The docs give the command.
