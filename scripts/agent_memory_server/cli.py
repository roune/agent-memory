"""agent-memory-server serve | reindex | token.

  serve    take the server lock, check the embedder, and answer on 0.0.0.0:8765
  reindex  with the server stopped: build the configured embedder's collection
           and make it the active one (see reindex.py)
  token    create, list and revoke bearer tokens (see auth.py). Run it next to
           the server: docker compose exec server agent-memory-server token …

Settings come from environment variables (see settings.py).
"""

import argparse
import json
import logging
import sys

import embedder as embedders
import memory_rules

from agent_memory_server import auth
from agent_memory_server.embedding import Embedding, EmbeddingMismatch
from agent_memory_server.lock import ServerLock
from agent_memory_server.qdrant import Qdrant, QdrantError
from agent_memory_server.reindex import ReindexError, reindex
from agent_memory_server.settings import Settings
from agent_memory_server.store import ServerStore
from store_core import StoreError

log = logging.getLogger("agent_memory_server")


def _parser():
    parser = argparse.ArgumentParser(prog="agent-memory-server")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="answer HTTP on --host:--port")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8765)
    again = commands.add_parser("reindex", help="build the configured embedder's collection (server stopped)")
    again.add_argument("--rebuild", action="store_true", help="drop that collection first and embed everything")
    again.add_argument("--prune", action="store_true", help="list the other memories* collections")
    again.add_argument("--yes", action="store_true", help="with --prune, delete them")
    again.add_argument(
        "--include-legacy",
        action="store_true",
        help="with --prune, also the legacy `memories` collection (only after the cutover)",
    )
    token = commands.add_parser("token", help="create, list and revoke bearer tokens")
    actions = token.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="mint a token; the secret is printed once")
    create.add_argument("--person", required=True)
    create.add_argument("--org", required=True)
    create.add_argument("--team", required=True)
    create.add_argument("--scopes", default="read", help="comma-separated: read, write, admin (default read)")
    create.add_argument("--allow-org", action="append", default=[], help="another org this token may write as")
    create.add_argument("--allow-team", action="append", default=[], help="another team this token may write as")
    create.add_argument("--label", default="", help="what the token is for, e.g. laptop (at most 64 characters)")
    create.add_argument("--same-person", action="store_true", help="a second token for a person who has one")
    actions.add_parser("list", help="every token, without secrets")
    revoke = actions.add_parser("revoke", help="revoke a token and the tokens minted from it")
    revoke.add_argument("token_id")
    return parser


def _uvicorn(app, host, port):
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info", server_header=False, proxy_headers=False)


def _embedder(settings, environ, given):
    if given is not None:
        return given
    return embedders.load_embedder(embedders.config_from_env(environ), settings.models_dir)


def _create_app(settings, store, embedding):
    from agent_memory_server.app import create_app

    return create_app(settings, store, embedding)


def serve(settings, store, model, qdrant, host, port, start=_uvicorn, build=_create_app, err=sys.stderr):
    lock = ServerLock(settings.lock_path)
    if not lock.acquire():
        print(f"agent-memory-server: another server or a reindex holds {settings.lock_path}", file=err)
        return 1
    try:
        embedding = Embedding(store, model, qdrant)
        try:
            model.load()
            embedding.prepare()
            embedding.state = "ready"
        except EmbeddingMismatch as error:
            print(f"agent-memory-server: {error}", file=err)
            return 1
        except embedders.EmbedderError as error:
            print(f"agent-memory-server: {error}", file=err)
            return 1
        except QdrantError as error:
            log.warning("%s; memories are stored and embedded once it answers", error)
        if settings.auth_off:
            log.warning("auth is off: every caller can write. Publish the port on 127.0.0.1 only.")
        elif store.count_active_tokens() == 0:
            log.warning(
                "auth is on and there are no tokens yet. Create one with: docker compose exec server "
                "agent-memory-server token create --person <you> --org <org> --team <team> --scopes read,write"
            )
        start(build(settings, store, embedding), host, port)
        return 0
    finally:
        lock.release()


def run_reindex(settings, store, model, qdrant, args, out=print, err=sys.stderr):
    if (args.yes or args.include_legacy) and not args.prune:
        print("agent-memory-server: --yes and --include-legacy go with --prune", file=err)
        return 2
    lock = ServerLock(settings.lock_path)
    if not lock.acquire():
        print(
            f"agent-memory-server: the server is running (it holds {settings.lock_path}). "
            "Stop it first: docker compose stop server",
            file=err,
        )
        return 1
    try:
        model.load()
        return reindex(
            store,
            model,
            qdrant,
            rebuild=args.rebuild,
            prune=args.prune,
            yes=args.yes,
            include_legacy=args.include_legacy,
            out=out,
        )
    except (EmbeddingMismatch, ReindexError, QdrantError, StoreError, embedders.EmbedderError) as error:
        print(f"agent-memory-server: {error}", file=err)
        return 1
    finally:
        lock.release()


def _clean(values, name):
    found = []
    for value in values:
        value = value.strip()
        if not value or "," in value:
            raise ValueError(f"{name} needs one non-empty value each time")
        if value not in found:
            found.append(value)
    return tuple(found)


def run_token(store, args, out=print, err=sys.stderr):
    try:
        if args.action == "create":
            person, org, team = args.person.strip(), args.org.strip(), args.team.strip()
            memory_rules.check_identity(org, team, person)
            scopes = auth.parse_scopes(args.scopes)
            allowed_orgs = tuple(value for value in _clean(args.allow_org, "--allow-org") if value != org)
            allowed_teams = tuple(value for value in _clean(args.allow_team, "--allow-team") if value != team)
            token_id, secret = store.create_token(
                person,
                org,
                team,
                scopes,
                allowed_orgs=allowed_orgs,
                allowed_teams=allowed_teams,
                label=auth.clean_label(args.label),
                same_person=args.same_person,
            )
            out(f"token {token_id} for {person} ({org}/{team}), scopes {','.join(scopes)}")
            out(secret)
            out("This is the only time the secret is shown. The server keeps only its hash.")
            return 0
        if args.action == "list":
            rows = store.list_tokens()
            if not rows:
                out("no tokens")
            for row in rows:
                state = f"revoked {row['revoked_at']}" if row["revoked_at"] else "active"
                parent = f" child of {row['parent_id']}" if row["parent_id"] else ""
                allowed = json.loads(row["allowed_orgs"]) + json.loads(row["allowed_teams"])
                extra = f" also {','.join(allowed)}" if allowed else ""
                label = f" [{row['label']}]" if row["label"] else ""
                out(
                    f"{row['id']}  {row['person']}  {row['org']}/{row['team']}{extra}  "
                    f"{','.join(json.loads(row['scopes']))}{label}{parent}  {state}"
                )
            return 0
        count = store.revoke_token(args.token_id.strip())
        if not count:
            print(f"agent-memory-server: no active token {args.token_id}", file=err)
            return 1
        out(f"revoked {count} token{'s' if count != 1 else ''}")
        return 0
    except (ValueError, auth.AuthError, StoreError) as error:
        print(f"agent-memory-server: {error}", file=err)
        return 1


def run(
    argv=None,
    environ=None,
    model=None,
    transport=None,
    start=_uvicorn,
    build=_create_app,
    out=print,
    err=sys.stderr,
    setup_logging=False,
):
    """The command line. model, transport, start and build replace the real ones in tests."""
    args = _parser().parse_args(argv)
    if setup_logging:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env(environ)
    store = ServerStore(settings.db_path)
    if args.command == "token":
        return run_token(store, args, out=out, err=err)
    try:
        model = _embedder(settings, environ, model)
    except embedders.EmbedderError as error:
        print(f"agent-memory-server: {error}", file=err)
        return 1
    qdrant = Qdrant(settings.qdrant_url, settings.qdrant_api_key, transport=transport)
    if args.command == "serve":
        return serve(settings, store, model, qdrant, args.host, args.port, start=start, build=build, err=err)
    return run_reindex(settings, store, model, qdrant, args, out=out, err=err)
