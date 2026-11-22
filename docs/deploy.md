# Deploying agent-memory

There are three ways to run it. Each one has two parts: the `agent-memory` CLI on each laptop, and one server in Docker, which is `agent-memory-server` next to Qdrant.

| Setup | CLI | Server | Auth |
| --- | --- | --- | --- |
| [One laptop, from a clone](#one-laptop-from-a-clone) | `./scripts/install` (editable) | `./scripts/setup`, built from the clone | off, loopback only |
| [One laptop, without a clone](#one-laptop-without-a-clone) | `uv tool install git+https://github.com/roune/agent-memory` | `compose.yaml`, the published image | off, loopback only |
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

The rest of the clone's `data/` stays where it is: the server's `data/server`, Qdrant's `data/qdrant`, and the old laptop store `data/memories.sqlite`, which `agent-memory migrate` reads.

## Upgrading the CLI to the tool install

1. Install the CLI:
   - From a clone, run `git pull`, then `./scripts/install`. It removes the old `~/.local/bin/agent-memory` link to the clone, then runs `uv tool install --editable .`.
   - Without a clone, run `uv tool install git+https://github.com/roune/agent-memory`. If `~/.local/bin/agent-memory` is the old link into a clone, remove it first. uv won't replace a file it didn't write.
2. Run `agent-memory install`. It updates the hooks it wrote, where they are, to run the installed command, and moves the old clone's state.
3. In each repository with git hooks, run `agent-memory config <repo> --git-hooks true`, so those hooks run the installed command too. Until then they run `<clone>/bin/agent-memory`, which works as long as the clone is there.

`agent-memory install` updates only the entries it wrote:

- In `~/.claude/settings.json` and `~/.codex/hooks.json`, those are the entries whose command runs `agent-memory capture`.
- For Cursor and Pi, those are the files that carry the `agent-memory-hook` line: `~/.cursor/hooks/agent-memory.sh` and `~/.pi/agent/extensions/agent-memory/index.ts`.

Each entry keeps its place and its other keys, such as a `timeout`. Every other hook stays as it is. If a file at one of those paths has no marker, install stops before it writes anything and says which file it is.

The hooks run the command as it was called. For a tool install that's `~/.local/bin/agent-memory`, or uv's tool bin folder, which survives `uv tool upgrade agent-memory`. Run from a clone without the tool install, the hooks run `<clone>/bin/agent-memory`, as before.

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
mkdir agent-memory-server && cd agent-memory-server
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

To upgrade, download `compose.yaml` from the new tag, then run `docker compose pull` and `docker compose up -d`. Then run `uv tool upgrade agent-memory`.

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

## Releasing

Only the repository owner does these steps. Pushing a tag is the release.

1. **Choose the version.** Set it in `pyproject.toml` (`version`), and in the `image:` line of both `compose.yaml` and `compose.team.yaml`. Run `uv lock` and `scripts/release-check v<version>`. CI runs the same check on every PR, so a version bump that misses one of the three files fails there.
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

A tag-only workflow can't run in a PR. So on every PR, `test.yml`'s `release-image` job runs the same build for both platforms. It pushes to a registry on the runner instead of GHCR, then pulls each platform and runs `scripts/image-health` on it. It also runs the no-clone `compose.yaml` steps and `compose.team.yaml` against that image. The steps a PR can't cover are the push to GHCR, the native arm64 runner, and the package's visibility.
