"""The embedding model, behind one interface.

A deployment runs one embedder. scripts/embed.py embeds approved passages with
it, and a query path embeds the question with the same embedder, so both sides
of a search live in one vector space. Each embedder names its provider, model,
revision, dimension, and distance, which is what a collection records.

Only fastembed on the pinned bge-small checkpoint is implemented. Choose it with
AGENT_MEMORY_EMBEDDER (default fastembed) and AGENT_MEMORY_EMBED_MODEL (default
BAAI/bge-small-en-v1.5). Any other value is an error, not a fallback.
docs/embedding-model.md, "Adding a provider", says how another one plugs in.
"""

import os
from pathlib import Path
from typing import Protocol

PROVIDER_ENV = "AGENT_MEMORY_EMBEDDER"
MODEL_ENV = "AGENT_MEMORY_EMBED_MODEL"
DEFAULT_PROVIDER = "fastembed"


class EmbedderError(Exception):
    pass


class Embedder(Protocol):
    provider: str
    model: str
    revision: str
    dimension: int
    distance: str

    def load(self):
        """Get ready to embed: fetch and load local weights. A hosted model may do nothing."""

    def embed_documents(self, texts):
        """One vector per approved passage, in order."""

    def embed_query(self, text):
        """One vector for a search question."""


def _as_list(vector):
    return [float(value) for value in (vector.tolist() if hasattr(vector, "tolist") else vector)]


class FastEmbedBge:
    """BAAI/bge-small-en-v1.5 from the pinned quantized ONNX checkpoint, on CPU.

    Passages are embedded as they are. A query gets the BGE v1.5 retrieval
    instruction first, as the model card recommends for short queries.
    """

    provider = "fastembed"
    model = "BAAI/bge-small-en-v1.5"
    revision = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
    onnx_repo = "Qdrant/bge-small-en-v1.5-onnx-Q"
    checkpoint = "bge-small-en-v1.5-onnx-q"
    dimension = 384
    distance = "Cosine"
    query_prefix = "Represent this sentence for searching relevant passages: "

    def __init__(self, models_dir, backend=None):
        self.model_dir = Path(models_dir) / self.checkpoint
        self._loaded = backend

    def ensure_checkpoint(self):
        """Download the pinned revision once. A stamp file records which one is on disk."""
        stamp = self.model_dir / ".revision"
        ready = (self.model_dir / "model_optimized.onnx").is_file() and stamp.is_file()
        if ready and stamp.read_text().strip() == self.revision:
            return
        from huggingface_hub import snapshot_download

        self.model_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=self.onnx_repo,
            revision=self.revision,
            local_dir=str(self.model_dir),
        )
        stamp.write_text(self.revision + "\n")

    def load(self):
        """The FastEmbed model, loaded once per embedder."""
        if self._loaded is None:
            self.ensure_checkpoint()
            from fastembed import TextEmbedding

            self._loaded = TextEmbedding(
                model_name=self.model,
                specific_model_path=str(self.model_dir),
            )
        return self._loaded

    def _embed(self, texts):
        vectors = [_as_list(vector) for vector in self.load().embed(list(texts))]
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbedderError(
                    f"{self.model} returned a vector of length {len(vector)}, expected {self.dimension}"
                )
        return vectors

    def embed_documents(self, texts):
        return self._embed(texts)

    def embed_query(self, text):
        return self._embed([self.query_prefix + text])[0]


PROVIDERS = {
    FastEmbedBge.provider: FastEmbedBge,
}


def config_from_env(environ=None):
    environ = os.environ if environ is None else environ
    return {
        "provider": environ.get(PROVIDER_ENV, ""),
        "model": environ.get(MODEL_ENV, ""),
    }


def load_embedder(config, models_dir, backend=None):
    """The configured embedder. Raises EmbedderError for anything not implemented.

    models_dir is where a local model keeps its checkpoint. backend replaces the
    loaded model, for tests.
    """
    provider = (config.get("provider") or DEFAULT_PROVIDER).strip().lower()
    factory = PROVIDERS.get(provider)
    if factory is None:
        implemented = ", ".join(sorted(PROVIDERS))
        raise EmbedderError(
            f"embedder {provider!r} is not implemented (implemented: {implemented}). "
            'See docs/embedding-model.md, "Adding a provider".'
        )
    model = (config.get("model") or factory.model).strip()
    if model != factory.model:
        raise EmbedderError(
            f"{provider} runs only {factory.model} at revision {factory.revision}, not {model!r}"
        )
    return factory(models_dir, backend=backend)
