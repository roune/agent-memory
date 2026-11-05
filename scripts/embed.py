"""Embed approved memories into the local Qdrant server.

Reads memories/*.json. Talks to the server over HTTP. Does not open data/qdrant.
"""

import json
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
MEMORIES = ROOT / "memories"
MODEL_DIR = ROOT / "data" / "models" / "bge-small-en-v1.5-onnx-q"
REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
ONNX_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSION = 384
QDRANT = "http://127.0.0.1:6333"
COLLECTION = "memories"

REQUIRED = ("id", "kind", "repo", "tool", "session_id", "text")


def api(method, path, body=None, missing_ok=False):
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        QDRANT + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request) as response:
            raw = response.read()
    except HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        detail = error.read().decode()
        raise SystemExit(f"{method} {path} failed: {error.code} {detail}") from error
    return json.loads(raw) if raw else {}


def ensure_model():
    stamp = MODEL_DIR / ".revision"
    ready = (MODEL_DIR / "model_optimized.onnx").is_file() and stamp.is_file()
    if ready and stamp.read_text().strip() == REVISION:
        return
    from huggingface_hub import snapshot_download

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=ONNX_REPO,
        revision=REVISION,
        local_dir=str(MODEL_DIR),
    )
    stamp.write_text(REVISION + "\n")


def load_memories():
    documents = []
    for path in sorted(MEMORIES.glob("*.json")):
        document = json.loads(path.read_text())
        missing = [field for field in REQUIRED if not document.get(field)]
        if missing:
            raise SystemExit(f"{path.name} is missing {', '.join(missing)}")
        if document["kind"] not in ("change", "learning", "decision"):
            raise SystemExit(f"{path.name} has kind {document['kind']!r}")
        document["_file"] = path.name
        documents.append(document)
    if not documents:
        raise SystemExit(f"no memories in {MEMORIES}")
    return documents


def ensure_collection():
    info = api("GET", f"/collections/{COLLECTION}", missing_ok=True)
    if info is None:
        api(
            "PUT",
            f"/collections/{COLLECTION}",
            {"vectors": {"size": DIMENSION, "distance": "Cosine"}},
        )
        for field in ("repo", "tool", "session_id", "kind"):
            api(
                "PUT",
                f"/collections/{COLLECTION}/index",
                {"field_name": field, "field_schema": "keyword"},
            )
        return
    size = info["result"]["config"]["params"]["vectors"]["size"]
    if size != DIMENSION:
        raise SystemExit(
            f"collection {COLLECTION} has dimension {size}, model has {DIMENSION}"
        )


def point_id(document_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-memory:{document_id}"))


def main():
    documents = load_memories()
    ensure_model()
    from fastembed import TextEmbedding

    started = time.perf_counter()
    model = TextEmbedding(
        model_name=MODEL_NAME,
        specific_model_path=str(MODEL_DIR),
    )
    loaded = time.perf_counter()
    texts = [document["text"] for document in documents]
    vectors = [vector.tolist() for vector in model.embed(texts)]
    embedded = time.perf_counter()
    ensure_collection()
    points = []
    for document, vector in zip(documents, vectors):
        if len(vector) != DIMENSION:
            raise SystemExit(f"{document['id']} vector has length {len(vector)}")
        points.append(
            {
                "id": point_id(document["id"]),
                "vector": vector,
                "payload": {
                    "document_id": document["id"],
                    "kind": document["kind"],
                    "repo": document["repo"],
                    "tool": document["tool"],
                    "session_id": document["session_id"],
                    "text": document["text"],
                    "embedding_model": MODEL_NAME,
                    "embedding_revision": REVISION,
                },
            }
        )
    api("PUT", f"/collections/{COLLECTION}/points?wait=true", {"points": points})
    print(
        f"embedded {len(points)} into {COLLECTION} "
        f"(load {loaded - started:.2f}s, embed {embedded - loaded:.2f}s)"
    )
    for point in points:
        print(f"{point['id']}  {point['payload']['document_id']}")


if __name__ == "__main__":
    main()
