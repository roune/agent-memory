"""agent-memory-server serve | reindex.

  serve    take the server lock, check the embedder, and answer on 0.0.0.0:8765
  reindex  with the server stopped: build the configured embedder's collection
           and make it the active one (see reindex.py)

Settings come from environment variables (see settings.py).
"""

import argparse
import logging
import sys

import embedder as embedders

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
    if not settings.auth_off:
        print(
            "agent-memory-server: auth isn't built yet (docs/server-plan.md, PR 2c). Set "
            "AGENT_MEMORY_AUTH=off, and publish the port on 127.0.0.1 only.",
            file=err,
        )
        return 2
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
        log.warning("auth is off: every caller can write. Publish the port on 127.0.0.1 only.")
        start(build(settings, store, embedding), host, port)
        return 0
    finally:
        lock.release()


def run_reindex(settings, store, model, qdrant, args, out=print, err=sys.stderr):
    if args.yes and not args.prune:
        print("agent-memory-server: --yes goes with --prune", file=err)
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
        return reindex(store, model, qdrant, rebuild=args.rebuild, prune=args.prune, yes=args.yes, out=out)
    except (EmbeddingMismatch, ReindexError, QdrantError, StoreError, embedders.EmbedderError) as error:
        print(f"agent-memory-server: {error}", file=err)
        return 1
    finally:
        lock.release()


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
    try:
        model = _embedder(settings, environ, model)
    except embedders.EmbedderError as error:
        print(f"agent-memory-server: {error}", file=err)
        return 1
    store = ServerStore(settings.db_path)
    qdrant = Qdrant(settings.qdrant_url, settings.qdrant_api_key, transport=transport)
    if args.command == "serve":
        return serve(settings, store, model, qdrant, args.host, args.port, start=start, build=build, err=err)
    return run_reindex(settings, store, model, qdrant, args, out=out, err=err)
