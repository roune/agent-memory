"""Collections, the startup checks, and embed-on-write. Standard library only.

Each embedder gets its own collection, memories-<provider>-<dimension>-<hash8>.
memories.sqlite's embedding_state row names the active one. Embedding.prepare()
refuses an embedder that doesn't match that row or the live collection, and
names the command that fixes it.
"""

import hashlib
import logging
import threading

import memory_points
from embedder import EmbedderError
from store_core import StoreError

from agent_memory_server.qdrant import QdrantError, vector_size

log = logging.getLogger("agent_memory_server")

FIX_REINDEX = "Stop the server and run `agent-memory-server reindex`"
FIX_REBUILD = "Stop the server and run `agent-memory-server reindex --rebuild`"


class EmbeddingMismatch(Exception):
    """The configured embedder doesn't match memories.sqlite or Qdrant."""


def collection_name(provider, model, revision, dimension):
    digest = hashlib.sha256(f"{provider}|{model}|{revision}|{dimension}".encode()).hexdigest()[:8]
    return f"memories-{provider}-{dimension}-{digest}"


def describe(embedder):
    """What a collection and embedding_state record about an embedder."""
    return {
        "provider": embedder.provider,
        "model": embedder.model,
        "revision": embedder.revision,
        "dimension": embedder.dimension,
        "distance": embedder.distance,
        "collection": collection_name(embedder.provider, embedder.model, embedder.revision, embedder.dimension),
    }


def _label(description):
    return (
        f"{description['provider']} {description['model']} at {description['revision']} "
        f"({description['dimension']} dimensions, collection {description['collection']})"
    )


def ensure_collection(qdrant, description):
    """Create the collection with its metadata if it is missing, and add any missing payload index.

    Returns True when it created the collection. Raises EmbeddingMismatch when it exists with
    another vector size.
    """
    name = description["collection"]
    info = qdrant.collection(name)
    created = info is None
    if created:
        metadata = {key: description[key] for key in ("provider", "model", "revision", "dimension", "distance")}
        qdrant.create_collection(name, description["dimension"], description["distance"], metadata)
        info = qdrant.collection(name) or {}
    else:
        size = vector_size(info)
        if size != description["dimension"]:
            raise EmbeddingMismatch(
                f"collection {name} holds vectors of size {size}, but the configured embedder makes "
                f"{description['dimension']}. {FIX_REBUILD}."
            )
    existing = info.get("payload_schema") or {}
    for field, schema in memory_points.PAYLOAD_INDEXES.items():
        if field not in existing:
            qdrant.create_index(name, field, schema)
    return created


def points_for(documents, vectors, description):
    if len(vectors) != len(documents):
        raise EmbedderError(f"{len(documents)} documents but {len(vectors)} vectors")
    points = []
    for document, vector in zip(documents, vectors):
        if len(vector) != description["dimension"]:
            raise EmbedderError(
                f"{document['id']} got a vector of length {len(vector)}, expected {description['dimension']}"
            )
        points.append(
            {
                "id": memory_points.point_id(document["id"]),
                "vector": list(vector),
                "payload": memory_points.payload_for(
                    document, description["model"], description["revision"], description["provider"]
                ),
            }
        )
    return points


class Embedding:
    """The configured embedder, its collection, and the rows it still lacks.

    state is "pending" until prepare() succeeds, "ready" after, and "mismatch"
    when prepare() found a mismatch. Qdrant being down leaves it "pending", and
    the next embed_pending() tries again.
    """

    def __init__(self, store, embedder, qdrant, batch_size=64):
        self.store = store
        self.embedder = embedder
        self.qdrant = qdrant
        self.batch_size = batch_size
        self.description = describe(embedder)
        self.collection = self.description["collection"]
        self.state = "pending"
        self.error = ""
        self._lock = threading.Lock()
        # One caller at a time uses the model: embed-on-write and search share it.
        self._model_lock = threading.Lock()

    def check_state(self):
        """Raise EmbeddingMismatch when memories.sqlite's active embedder is another one."""
        state = self.store.embedding_state()
        if state is None:
            return None
        if any(state[key] != self.description[key] for key in ("provider", "model", "revision", "dimension", "collection")):
            raise EmbeddingMismatch(
                f"memories.sqlite's active embedder is {_label(state)}, but the configured one is "
                f"{_label(self.description)}. {FIX_REINDEX} to switch to it, or set "
                "AGENT_MEMORY_EMBEDDER and AGENT_MEMORY_EMBED_MODEL back."
            )
        return state

    def prepare(self):
        """Check the embedder against memories.sqlite and Qdrant, and make its collection ready.

        Raises EmbeddingMismatch, or QdrantError when Qdrant can't be reached.
        """
        state = self.check_state()
        if state is not None and self.qdrant.collection(self.collection) is None:
            raise EmbeddingMismatch(
                f"the active collection {self.collection} is missing from Qdrant. {FIX_REBUILD}."
            )
        ensure_collection(self.qdrant, self.description)
        if state is None:
            self.store.set_embedding_state(self.description)

    def embed_all_pending(self):
        """Embed every row the collection lacks. Returns how many were embedded. Raises on failure."""
        done = 0
        seen = set()
        while True:
            documents = self.store.pending_for(self.collection, limit=self.batch_size)
            pairs = [(document["id"], document["updated_at"]) for document in documents]
            if not documents or seen.issuperset(pairs):
                return done
            seen.update(pairs)
            with self._model_lock:
                vectors = self.embedder.embed_documents([document["text"] for document in documents])
            self.qdrant.upsert(self.collection, points_for(documents, vectors, self.description))
            self.store.mark_embedded_in(self.collection, pairs)
            done += len(documents)

    def embed_query(self, text):
        """One query vector from the configured embedder, checked against its dimension."""
        with self._model_lock:
            vector = list(self.embedder.embed_query(text))
        if len(vector) != self.description["dimension"]:
            raise EmbedderError(f"the query vector has length {len(vector)}, expected {self.description['dimension']}")
        return vector

    def embed_pending(self):
        """Prepare if needed, then embed what is pending. True when nothing is left. Never raises.

        Called after each write and by the retry loop.
        """
        with self._lock:
            try:
                if self.state != "ready":
                    self.prepare()
                    self.state = "ready"
                self.embed_all_pending()
            except EmbeddingMismatch as error:
                self.state, self.error = "mismatch", str(error)
                log.error("%s", error)
                return False
            except (QdrantError, EmbedderError, StoreError, OSError) as error:
                if self.state == "ready":
                    self.state = "pending"
                self.error = str(error)
                log.warning("embedding is waiting: %s", error)
                return False
            except Exception as error:  # a bug in a model or a transport must not kill the server
                if self.state == "ready":
                    self.state = "pending"
                self.error = f"{type(error).__name__}: {error}"
                log.exception("embedding failed")
                return False
            self.error = ""
            return True

    def status(self):
        return {**self.description, "state": self.state, "error": self.error}
