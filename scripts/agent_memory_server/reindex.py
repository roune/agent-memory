"""Build the configured embedder's collection, then make it the active one. Standard library only.

Run it while the server is stopped (cli.py takes the server lock first):

  reindex            embed every row the collection lacks, check the point
                     count, and switch embedding_state to this embedder
  reindex --rebuild  drop that collection first and embed everything again
  --prune            list the other collections whose name starts with
                     "memories"; with --yes, delete them. The legacy
                     `memories` collection, from before the server, is kept
                     unless --include-legacy is passed too.
"""

from agent_memory_server.embedding import Embedding, ensure_collection

LEGACY_COLLECTION = "memories"


class ReindexError(Exception):
    pass


def _count(count, one, many):
    return f"{count} {one if count == 1 else many}"


def reindex(
    store, embedder, qdrant, rebuild=False, prune=False, yes=False, include_legacy=False, out=print, batch_size=64
):
    """Print one line for each step: what was dropped or created, how many memories were embedded,
    the final point count, the active collection, and what --prune deleted or would delete."""
    embedding = Embedding(store, embedder, qdrant, batch_size=batch_size)
    description = embedding.description
    name = embedding.collection
    if rebuild:
        existed = qdrant.collection(name) is not None
        qdrant.delete_collection(name)
        out(f"dropped {name}" if existed else f"nothing to drop: {name} did not exist")
        store.forget_collection(name)
    if ensure_collection(qdrant, description):
        out(
            f"created {name} ({description['provider']} {description['model']}, "
            f"{description['dimension']} dimensions, {description['distance']})"
        )
    else:
        out(f"using {name}, which already existed")
    done = embedding.embed_all_pending()
    out(f"embedded {_count(done, 'memory', 'memories')} into {name}")
    points = qdrant.count(name)
    rows = store.count_memories()
    if points != rows:
        raise ReindexError(
            f"{name} holds {points} points but memories.sqlite has {rows} memories, so the active "
            "embedder was not switched. Run `agent-memory-server reindex --rebuild`."
        )
    out(
        f"{name} holds {_count(points, 'point', 'points')}, one for each of the "
        f"{_count(rows, 'memory', 'memories')} in memories.sqlite"
    )
    previous = store.embedding_state()
    store.set_embedding_state(description)
    if previous is None or previous["collection"] != name:
        was = "none" if previous is None else previous["collection"]
        out(f"active collection: {name} (was {was})")
    else:
        out(f"active collection: {name} (unchanged)")
    if prune:
        others = [other for other in qdrant.collection_names() if other.startswith("memories") and other != name]
        kept = LEGACY_COLLECTION in others and not include_legacy
        if kept:
            others.remove(LEGACY_COLLECTION)
            out(
                f"keeping {LEGACY_COLLECTION}, the collection from before the server "
                "(pass --include-legacy to delete it too)"
            )
        if not others:
            out("nothing else to prune" if kept else "nothing to prune: no other memories* collection")
        elif not yes:
            out("would delete: " + ", ".join(others))
            out("run again with --prune --yes to delete them")
        else:
            for other in others:
                qdrant.delete_collection(other)
                store.forget_collection(other)
                out(f"deleted {other}")
    return 0
