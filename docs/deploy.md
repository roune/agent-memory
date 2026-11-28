# Deploying agent-memory

There are three ways to run it. Each one has two parts: the `agent-memory` CLI on each laptop, and one server in Docker, which is `agent-memory-server` next to Qdrant.

| Setup | CLI | Server | Auth |
| --- | --- | --- | --- |
| [One laptop, from a clone](#one-laptop-from-a-clone) | `./scripts/install` (editable) | `./scripts/setup`, built from the clone | off, loopback only |
| [One laptop, without a clone](#one-laptop-without-a-clone) | `uv tool install git+https://github.com/roune/agent-memory` | `agent-memory setup`, with `compose.yaml` from the package and the published image | off, loopback only |
| [A team server](#a-team-server) | the same tool install on each laptop | `compose.team.yaml`, the published image | on |

The image is `ghcr.io/roune/agent-memory-server:<version>`, for `linux/amd64` and `linux/arm64`. `compose.yaml` and `compose.team.yaml` pin the version of their release. To run another image, set `AGENT_MEMORY_IMAGE` in `.env`.

## Where the CLI keeps its state

The CLI's own files live in `${XDG_DATA_HOME:-~/.local/share}/agent-memory/`:

| Path | What |
| --- | --- |
| `inbox/` | Bundles that wait for review, with copies of transcripts. They never leave the laptop. |
| `outbox/` | Approved requests that wait for the server. |
| `review.json` | The pid and port of a running review page. |
| `capture.log` | What the hooks did. |

The folder is created with mode 0700. `AGENT_MEMORY_DIR` puts these files somewhere else. Config stays in `~/.config/agent-memory/` (see [server.md](server.md#connecting-the-cli)).

Before the tool install, these files lived in the clone: `inbox/`, `outbox/`, `data/review.json` and `data/capture.log`. The first command after the upgrade moves them. `agent-memory install` also moves them from the clone whose `bin/agent-memory` the hooks ran. The move:

- never overwrites anything. A folder that exists in both places is merged entry by entry. A file whose name is already taken stays where it was, and the command says so.
- appends an old `capture.log` to the new one, and drops an old `review.json` when a new one exists.
- prints one line for each path it moved, and writes the same line to `capture.log`.
- does nothing on the next run, because nothing is left at the old paths.
- copies a file across file systems under a temporary name, then renames it, so a move cut short leaves no partial file.
- remembers each clone it moved from, in `.moved-from`, and checks it again on every command. An older CLI still running from that clone, such as a git hook, may file new bundles there.

The rest of the clone's `data/` stays where it is: the server's `data/server`, Qdrant's `data/qdrant`, and the old laptop store `data/memories.sqlite`, which `agent-memory migrate` reads.

## Upgrading the CLI to the tool install

1. Install the CLI:
   - From a clone, run `git pull` first, so the clone's own code is the new one too, then `./scripts/install`. It removes the old `~/.local/bin/agent-memory` link to the clone, then runs `uv tool install --editable .`.
   - Without a clone, run `uv tool install git+https://github.com/roune/agent-memory`. If `~/.local/bin/agent-memory` is the old link into a clone, remove it first. uv won't replace a file it didn't write.
2. Run `agent-memory install`. It updates the hooks it wrote, where they are, to run the installed command, and moves the old clone's state.
3. Git hooks: `install` also points the git hooks at the installed command in every repository it knows: the ones recorded in `~/.config/agent-memory/git-hooks` (each `install --git-repo` and `config --git-hooks true` adds one), and the old clone itself. After it has written everything, it names each repository whose git hooks still run another command. In each one, and in any repository set up before the record existed, run `agent-memory config <repo> --git-hooks true`. Until then those hooks run `<clone>/bin/agent-memory`. If that clone wasn't pulled, its old code files bundles in `<clone>/inbox` and auto-approved items in `<clone>/outbox`. The next `agent-memory` command moves them, because the clone is recorded.

`agent-memory install` updates only the entries it wrote:

- In `~/.claude/settings.json` and `~/.codex/hooks.json`, those are the entries whose command runs `agent-memory capture`.
- For Cursor and Pi, those are the files that carry the `agent-memory-hook` line: `~/.cursor/hooks/agent-memory.sh` and `~/.pi/agent/extensions/agent-memory/index.ts`.

Each entry keeps its place and its other keys, such as a `timeout`. Every other hook stays as it is. If a file at one of those paths has no marker, install stops before it writes anything and says which file it is.

The CLI's distribution name is `roune-agent-memory`, and uv names the tool after it. The command is still `agent-memory`. If you installed it under the old name, switch once:

```
uv tool uninstall agent-memory
uv tool install git+https://github.com/roune/agent-memory
agent-memory install
```

From a clone, run `./scripts/install --force` instead of the second line. The hook path, `~/.local/bin/agent-memory`, doesn't change.

`agent-memory install` refuses to write hooks from a run that won't last, such as `uvx` or `pipx run`: the copy it runs sits in a cache that gets cleaned. Install the CLI first, then run `agent-memory install`. `agent-memory --version` prints the installed version.

## Git hooks in a folder Cursor manages

Cursor can set a repository's `core.hooksPath` to `~/.cursor/agent-hooks/<id>/`. It owns that folder: hook names there link to its `.dispatcher`, which runs the hooks path the repository had before (named in `.cursor-original-hooks-path`, usually `.git/hooks`) and then Cursor's own `<hook>.cursor*` files. A file written into that folder can vanish when Cursor rebuilds it.

So when `install --git-repo` or `config --git-hooks true` finds that folder, it:

- writes `post-commit` and `post-merge` into the hooks path Cursor recorded, which survives a rebuild and still runs if Cursor resets `core.hooksPath`;
- links `post-commit` and `post-merge` in Cursor's folder to `.dispatcher`, as Cursor links its own hooks, when they aren't linked already;
- moves hooks an older install wrote into Cursor's folder out of it;
- changes none of Cursor's files, and stops before writing anything if Cursor recorded no hooks path, its dispatcher is missing, or another file holds one of those names.

`agent-memory config <repo>` reports `installed: False` if Cursor drops a link; run `config <repo> --git-hooks true` again. `config --git-hooks false` and `uninstall` remove only the scripts and links recorded in `~/.config/agent-memory/git-hooks`. A `Co-authored-by` trailer on commits in such a repository comes from Cursor's `commit-msg.cursor.co-author`, not from agent-memory.

Any other `core.hooksPath` outside the repository's `.git/hooks` gets the hooks as before, and install names the folder, since another tool may manage it.

The hooks run the command as it was called. For a tool install that's `~/.local/bin/agent-memory`, or uv's tool bin folder, which survives `uv tool upgrade roune-agent-memory`. Run from a clone without the tool install, the hooks run `<clone>/bin/agent-memory`, as before.

## One laptop, from a clone

```
git clone https://github.com/roune/agent-memory && cd agent-memory
./scripts/install            # uv tool install --editable .
./scripts/setup              # builds the image, starts it and Qdrant, waits for /v1/health
agent-memory install         # editor hooks, after a yes
agent-memory connect --editor claude-code   # optional: MCP in the editor
```

`scripts/setup` runs `docker compose up -d --build`, so the server runs this clone's code, tagged with the release's image name. See [server.md](server.md#try-it).

## One laptop, without a clone

You need [uv](https://docs.astral.sh/uv/) and Docker with Compose.

```
uv tool install git+https://github.com/roune/agent-memory
agent-memory setup
agent-memory install         # editor hooks, after a yes
```

`setup` does what `./scripts/setup` does in a clone, with the published image. It checks everything first and writes nothing until every check has passed:

- **Docker** is installed, `docker compose` works, and the daemon answers. Each failure prints one line that says what to install or start.
- **No newer server's files are in the folder.** If the folder's `compose.yaml` pins a newer version than the CLI, `setup` stops and says to upgrade the CLI. `--force` goes back anyway.
- **Port 8765 is free**, or held by this folder's own server. If the `agent-memory-server` compose project runs from another folder, `setup` names that folder and says to pass `--dir` with it, or to stop that project. If a clone's server answers, it says to run `docker compose down` in that clone.

Then it writes, pulls and starts:

- **The folder** is `${XDG_DATA_HOME:-~/.local/share}/agent-memory/server/`. `agent-memory setup --print-dir` prints it. `--dir <path>` picks another, and `~/.config/agent-memory/setup` remembers it, so a rerun without `--dir` finds it. `AGENT_MEMORY_DIR` moves the CLI's state, not this folder.
- **`compose.yaml`** is the copy inside the package, pinned to this CLI's image. A `compose.yaml` that `setup` didn't write is saved as `compose.yaml.<timestamp>.bak` first. Put your own settings in `.env`, next to it, such as `AGENT_MEMORY_IMAGE` or `AGENT_MEMORY_EMBEDDER`.
- **`.env`** (mode 0600) gets `AGENT_MEMORY_UID` and `AGENT_MEMORY_GID`, which make the server write `data/server` as you. Other lines stay.
- **`data/server` and `data/qdrant`** are created before Docker starts, because on Linux Docker would create them owned by root.
- **It runs** `docker compose pull` and `up -d --no-build` as the project `agent-memory-server`, so its containers stay apart from a clone's. Then it waits up to 120 seconds for `/v1/health`.
- **`~/.config/agent-memory/server`** is written with `url = http://127.0.0.1:8765` if it is missing. A file that names another server is left alone, and `setup` prints the `connect --url` command.
- **It offers the editors.** In a terminal, it asks once whether to run `agent-memory connect --editor` for the editors it found, and `--no-input` skips the question. `connect` shows its diff and asks again.

`data/server/memories.sqlite` in that folder is the store. Back it up. `setup` never runs `docker compose down -v` and never deletes `data/`. It is safe to run again, and running it again is the upgrade step.

The commands for logs and for stopping are printed at the end. They look like this, with your folder:

```
docker compose -p agent-memory-server -f <folder>/compose.yaml logs
docker compose -p agent-memory-server -f <folder>/compose.yaml down
```

### Files Docker can't mount

Docker has to bind-mount `data/` from the folder. Two setups can't do that with the default folder, and `setup` says so when `up` fails with a mount error:

- **Snap Docker** (Ubuntu's `snap install docker`) can't mount hidden folders under your home, and `~/.local/share` is one.
- **Docker Desktop** mounts only the folders it shares. A custom `XDG_DATA_HOME` outside its file-sharing list isn't one of them.

The fix is a folder Docker can see. It is remembered, so you pass it once:

```
agent-memory setup --dir ~/agent-memory-server
```

### Without `setup`

For your own folder layout, or to see each step, do what `setup` does by hand:

```
mkdir my-agent-memory && cd my-agent-memory
curl -fsSLO https://raw.githubusercontent.com/roune/agent-memory/v0.1.0/compose.yaml
mkdir -p data/server data/qdrant
printf 'AGENT_MEMORY_UID=%s\nAGENT_MEMORY_GID=%s\n' "$(id -u)" "$(id -g)" > .env
docker compose pull
docker compose up -d
curl -s http://127.0.0.1:8765/v1/health
agent-memory connect --url http://127.0.0.1:8765
agent-memory install
```

- **`compose.yaml` comes from the release tag** (here `v0.1.0`), so it pins the image of that release.
- **Pull before `up`.** `compose.yaml` keeps `build: .` for clones, and with `build:` set, a plain `up` tries to build a missing image instead of pulling it. There is nothing to build here.
- **Create `data/server` first, and write `.env`.** On Linux, Docker creates a missing bind folder owned by root, and the server runs as the ids in `.env` (1000:1000 without them), so it couldn't write `memories.sqlite`. `scripts/setup` does both in a clone.
- **`data/` next to `compose.yaml` is the store.** Back up `data/server/memories.sqlite`.
- **`connect` writes `~/.config/agent-memory/server`.** `scripts/setup` writes it in a clone.
- **Don't run it beside `setup`'s server.** Both publish port 8765. The folder is called `my-agent-memory`, not `agent-memory-server`, because compose names its project after the folder, and `agent-memory-server` is the project name `setup` uses. `setup` refuses to start when it sees that project running from another folder, and says which one. Pass `--dir` with this folder to let `setup` manage it.

To upgrade by hand, download `compose.yaml` from the new tag, then run `docker compose pull` and `docker compose up -d`. Then upgrade the CLI as the table below says.

## Upgrades

| Install | Upgrade the CLI | Then |
| --- | --- | --- |
| uv from git | `uv tool upgrade roune-agent-memory` | `agent-memory setup` |
| a clone | `git pull` | `./scripts/setup` |

The other ways to install the CLI will appear here as they exist.

- **`setup` is the upgrade for the server.** It rewrites `compose.yaml` for the new version, keeps `.env` and `data/`, pulls the new image and restarts. The server moves to the new image only then.
- **The CLI tells you when they differ.** `/v1/health` reports the server's `version`. `agent-memory setup` and `agent-memory review` compare it with the CLI's own, and print one line when they differ: `the server runs 0.2.0 and this CLI is 0.3.0: run agent-memory setup to upgrade the server`. A server whose `/v1/health` has no version is older than 0.2.0, and the line says so. `capture`, `git-hook` and `sync --quiet` never ask, so the hooks stay fast and silent. `review` asks with a short timeout and says nothing when the server doesn't answer.
- **A server you started another way** (the manual steps, a team host) is upgraded the way you started it.
- **The hooks keep working.** They run the command as it was called, which uv's tool bin link keeps across an upgrade (see [Git hooks in a folder Cursor manages](#git-hooks-in-a-folder-cursor-manages)).

### If Qdrant won't start after an upgrade

Qdrant may not read storage written more than one minor version back, which a CLI that skipped releases can cause. `memories.sqlite` is the source of truth and Qdrant is derived from it, so rebuild Qdrant's storage from the rows. In the server folder (`agent-memory setup --print-dir`):

```
docker compose -p agent-memory-server stop
mv data/qdrant data/qdrant.old
mkdir data/qdrant
docker compose -p agent-memory-server run --rm --no-build -T server agent-memory-server reindex --rebuild
docker compose -p agent-memory-server up -d --no-build
```

`--rebuild` matters. `memories.sqlite` still records every row as embedded, so a plain `reindex` would embed nothing, find an empty collection, and stop with an error. `--rebuild` drops the collection, forgets what it held, and embeds every memory again. `reindex` takes the server's lock, which is why the server is stopped first (see [server.md](server.md#embedding)). Delete `data/qdrant.old` once the server answers. In a clone, leave out `-p agent-memory-server` and `--no-build`.

### Why auth is off here, and when it must not be

`compose.yaml` sets `AGENT_MEMORY_AUTH=off`. Two things make that safe on a laptop:

- It publishes the port as `127.0.0.1:8765:8765`, so no other machine can connect.
- The server's [browser rules](server.md#browser-rules) keep web pages on this machine out. A `Host` it doesn't know gets a 421, an `Origin` gets a 403, and a POST must be JSON.

The server can't see how Docker publishes its port. If you change that mapping to `8765:8765` or `0.0.0.0`, anyone on the network can write memories as anyone. A server other machines reach is a team server, with `compose.team.yaml`.

Auth off doesn't stop a process that runs as you, such as an agent with a shell. Nothing here claims to. See the caveat in [server-plan.md](server-plan.md#agent-memory-connect).

## A team server

One host runs the server with auth on, behind a reverse proxy that adds HTTPS. Each person gets a token, and each laptop connects with it.

### On the server host

```
mkdir agent-memory-server && cd agent-memory-server
curl -fsSLO https://raw.githubusercontent.com/roune/agent-memory/v0.1.0/compose.team.yaml
mkdir -p data/server data/qdrant
cat > .env <<EOF
AGENT_MEMORY_ALLOWED_HOSTS=memory.acme.internal
AGENT_MEMORY_UID=$(id -u)
AGENT_MEMORY_GID=$(id -g)
EOF
docker compose -f compose.team.yaml pull
docker compose -f compose.team.yaml up -d
```

- **Auth is on**, because `compose.team.yaml` doesn't set `AGENT_MEMORY_AUTH`. Every route but `/v1/health` needs a bearer token.
- **`AGENT_MEMORY_ALLOWED_HOSTS`** is required. It holds the host names people use, comma-separated, as the proxy passes them on in `Host`: `memory.acme.internal`, with no port for HTTPS on 443. The file adds `127.0.0.1:8765` in front, which the image's health check sends. A `Host` that isn't listed gets a 421.
- **The port is published on `127.0.0.1:8765`**, for a proxy on the same host. To publish on another address, set `AGENT_MEMORY_BIND`. Do that only behind TLS, because every request carries a token.
- **Qdrant has no published port.** `AGENT_MEMORY_QDRANT_API_KEY`, `AGENT_MEMORY_EMBEDDER` and `AGENT_MEMORY_EMBED_MODEL` work as in [server.md](server.md#settings).

The proxy keeps the original `Host` and forwards to `127.0.0.1:8765`. With Caddy, which gets a certificate on its own:

```
memory.acme.internal {
    reverse_proxy 127.0.0.1:8765
}
```

TLS inside the server is out of scope ([server-plan.md](server-plan.md#out-of-this-plan)).

### Tokens

Mint one token per person on the host. It is shown once:

```
docker compose -f compose.team.yaml exec server agent-memory-server token create \
  --person miguel --org acme --team platform --scopes read,write --label laptop
docker compose -f compose.team.yaml exec server agent-memory-server token list
docker compose -f compose.team.yaml exec server agent-memory-server token revoke t_1a2b3c4d5e6f
```

Send the token to its person by a private channel. `migrate` needs a separate admin token; see [server.md](server.md#auth-and-tokens).

### On each laptop

```
uv tool install git+https://github.com/roune/agent-memory
agent-memory connect --url https://memory.acme.internal --token-stdin < token.txt
agent-memory install
agent-memory connect --editor claude-code,cursor,codex
```

`connect --editor` mints a read-only token for each editor, so editor files never hold the write token.

## Windows (WSL 2)

agent-memory doesn't run on native Windows. The CLI exits with a pointer to this section. Use WSL 2, where everything below works as it does on Linux.

- **Install the CLI inside WSL**, in a WSL shell: `uv tool install git+https://github.com/roune/agent-memory`. The Windows `PATH` isn't enough, because the CLI is a Linux program.
- **Docker** comes from Docker Desktop with its WSL integration turned on for your distribution, or from Docker Engine installed inside WSL. Run `docker compose` from the WSL shell too, so `data/` sits on the WSL filesystem.
- **The editors have to run in WSL too.** That means Claude Code or Codex in a WSL shell, or Cursor or VS Code attached to WSL. A hook in the Windows-side `%USERPROFILE%\.cursor` can't run a CLI that lives in WSL, so don't point Windows-side editors at it.

## Releasing

Only the repository owner does these steps. Pushing a tag is the release.

1. **Choose the version.** Set it in `pyproject.toml` (`version`), and in the `image:` line of both `compose.yaml` and `compose.team.yaml`. The copy of `compose.yaml` at `scripts/agent_memory_assets/compose.yaml`, which the wheel carries for `agent-memory setup`, must be byte-for-byte the same, so copy it after you edit the root file. Run `uv lock` and `scripts/release-check v<version>`. CI runs the same check on every PR, so a version bump that misses one of these files, or a packaged copy that differs, fails there.
2. **Merge** to `main`.
3. **Tag the merge commit and push the tag:**

   ```
   git tag v0.1.0 <merge sha>
   git push origin v0.1.0
   ```

4. **Watch the `release` workflow** (`.github/workflows/release.yml`):
   - `check` runs `scripts/release-check` on the tag.
   - `publish` builds `linux/amd64` and `linux/arm64` and pushes `ghcr.io/roune/agent-memory-server:<version>`.
   - `verify` pulls that tag on an amd64 runner and an arm64 runner, and runs `scripts/image-health`. That checks `/v1/health` with auth on, that `/v1/whoami` needs a token, the architecture, and the version label.
5. **Make the package public, once.** GHCR creates the package as private on its first push. The no-clone and team setups pull without logging in, so they fail until it is public.
   - Go to github.com/roune → Packages → `agent-memory-server` → Package settings → Danger Zone → Change visibility → Public.
   - The image's `org.opencontainers.image.source` label links the package to this repo, so the workflow's token can push later versions.
   - Later releases keep the visibility.
6. **Check it as a stranger would**, logged out of `ghcr.io`:

   ```
   docker logout ghcr.io
   docker pull ghcr.io/roune/agent-memory-server:0.1.0
   scripts/image-health ghcr.io/roune/agent-memory-server:0.1.0
   ```

7. **Optionally, write a GitHub release** for the tag, with `compose.yaml` and `compose.team.yaml` attached. The commands above download them from the tag, so the release isn't required.

A tag-only workflow can't run in a PR. So on every PR, `test.yml`'s `release-image` job runs the same build for both platforms. It pushes to a registry on the runner instead of GHCR, then pulls each platform and runs `scripts/image-health` on it. It also runs the no-clone `compose.yaml` steps and `compose.team.yaml` against that image. Last, it installs the built wheel and runs `agent-memory setup` twice against that image, with a temporary `HOME`: it checks `/v1/health`, the CLI's server file, and that a memory written before the second run is still there after it. The steps a PR can't cover are the push to GHCR, the native arm64 runner, and the package's visibility.
