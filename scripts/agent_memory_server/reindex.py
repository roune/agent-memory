"""Build the configured embedder's collection, then make it the active one. Standard library only.

Run it while the server is stopped (cli.py takes the server lock first):

  reindex            embed every row the collection lacks, check the point
                     count, and switch embedding_state to this embedder
  reindex --rebuild  drop that collection first and embed everything again
  --prune            list the other collections whose name starts with
                     "memories"; with --yes, delete them. The legacy
                     `memories` collection, which the laptop's scripts/embed
                     still writes until the cutover, is kept unless
                     --include-legacy is passed too.
"""

from agent_memory_server.embedding import Embedding, ensure_collection

LEGACY_COLLECTION = "memories"


class ReindexError(Exception):
    pass


def reindex(
    store, embedder, qdrant, rebuild=False, prune=False, yes=False, include_legacy=False, out=print, batch_size=64
):
    embedding = Embedding(store, embedder, qdrant, batch_size=batch_size)
    name = embedding.collection
    if rebuild:
        qdrant.delete_collection(name)
        store.forget_collection(name)
        out(f"dropped {name}")
    ensure_collection(qdrant, embedding.description)
    done = embedding.embed_all_pending()
    points = qdrant.count(name)
    rows = store.count_memories()
    if points != rows:
        raise ReindexError(
            f"{name} holds {points} points but memories.sqlite has {rows} memories, so the active "
            "embedder was not switched. Run `agent-memory-server reindex --rebuild`."
        )
    previous = store.embedding_state()
    store.set_embedding_state(embedding.description)
    out(f"embedded {done} into {name}; it holds all {rows} memories")
    if previous is None or previous["collection"] != name:
        was = "none" if previous is None else previous["collection"]
        out(f"active collection: {name} (was {was})")
    if prune:
        others = [other for other in qdrant.collection_names() if other.startswith("memories") and other != name]
        if LEGACY_COLLECTION in others and not include_legacy:
            others.remove(LEGACY_COLLECTION)
            out(
                f"keeping {LEGACY_COLLECTION}: the laptop's scripts/embed still writes it until the cutover "
                "(pass --include-legacy to delete it too)"
            )
        if not others:
            out("nothing to prune")
        elif not yes:
            out("would delete: " + ", ".join(others))
            out("run again with --prune --yes to delete them")
        else:
            for other in others:
                qdrant.delete_collection(other)
                store.forget_collection(other)
                out(f"deleted {other}")
    return 0
