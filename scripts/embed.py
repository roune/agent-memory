"""Embed approved memories from the local SQLite store into Qdrant.

Reads the memory table. Talks to the configured Qdrant URL over HTTP.
Does not open data/qdrant and does not read memories/*.json.
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import memory_store

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "data" / "models" / "bge-small-en-v1.5-onnx-q"
REVISION = "aa8f8b060edb00e03bfdd08813a2949946c8ba55"
ONNX_REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSION = 384
COLLECTION = "memories"


def api(base, method, path, body=None, missing_ok=False):
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        base.rstrip("/") + path,
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
    except URLError as error:
        raise SystemExit(f"{method} {path} failed: {error.reason}") from error
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


def load_memories(remote_id=None, repo_filter=None, rebuild=False):
    return memory_store.pending_documents(
        remote_id=remote_id,
        repo_filter=repo_filter,
        rebuild=rebuild,
    )


def ensure_collection(base):
    info = api(base, "GET", f"/collections/{COLLECTION}", missing_ok=True)
    if info is None:
        api(
            base,
            "PUT",
            f"/collections/{COLLECTION}",
            {"vectors": {"size": DIMENSION, "distance": "Cosine"}},
        )
        info = api(base, "GET", f"/collections/{COLLECTION}")
    else:
        size = info["result"]["config"]["params"]["vectors"]["size"]
        if size != DIMENSION:
            raise SystemExit(
                f"collection {COLLECTION} has dimension {size}, model has {DIMENSION}"
            )
    existing = (info.get("result") or {}).get("payload_schema") or {}
    for field in (
        "repo",
        "tool",
        "session_id",
        "kind",
        "domain",
        "commit",
        "commits",
        "org",
        "team",
        "person",
        "record",
        "subject",
        "alarm",
        "look_at",
        "supersedes",
        "superseded_by",
        "updates",
    ):
        if field in existing:
            continue
        api(
            base,
            "PUT",
            f"/collections/{COLLECTION}/index",
            {"field_name": field, "field_schema": "keyword"},
        )
    if "pull_request" not in existing:
        api(
            base,
            "PUT",
            f"/collections/{COLLECTION}/index",
            {"field_name": "pull_request", "field_schema": "integer"},
        )
    if "occurred_at" not in existing:
        api(
            base,
            "PUT",
            f"/collections/{COLLECTION}/index",
            {"field_name": "occurred_at", "field_schema": "datetime"},
        )


def point_id(document_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-memory:{document_id}"))


def payload_for(document):
    payload = {
        "document_id": document["id"],
        "kind": document["kind"],
        "domain": document["domain"],
        "repo": document["repo"],
        "tool": document["tool"],
        "session_id": document["session_id"],
        "commit": document.get("commit") or "",
        "commits": list(document.get("commits") or []),
        "text": document["text"],
        "org": document.get("org") or "",
        "team": document.get("team") or "",
        "person": document.get("person") or "",
        "embedding_model": MODEL_NAME,
        "embedding_revision": REVISION,
    }
    pull_request = document.get("pull_request")
    if isinstance(pull_request, int):
        payload["pull_request"] = pull_request
    occurred = document.get("occurred_at")
    if isinstance(occurred, str) and occurred.strip():
        payload["occurred_at"] = occurred.strip()
    for optional in (
        "record",
        "subject",
        "alarm",
        "look_at",
        "supersedes",
        "superseded_by",
        "updates",
    ):
        val = document.get(optional)
        if val:
            payload[optional] = val
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(prog="embed")
    parser.add_argument("--repo", default="", help="remote id; required in per-repo mode")
    parser.add_argument("--rebuild", action="store_true", help="embed every row, not only pending ones")
    args = parser.parse_args(argv)
    mode = memory_store.database_config()["mode"]
    remote = args.repo.strip()
    if mode == "per-repo" and not remote:
        raise SystemExit("per-repo mode needs --repo <remote id>")
    selected = remote if mode == "per-repo" else None
    try:
        base = memory_store.qdrant_url(selected)
    except memory_store.StoreError as error:
        raise SystemExit(str(error)) from error
    if not memory_store.container_ready(base):
        raise SystemExit(f"Qdrant is not ready at {base}. Run scripts/setup first.")
    documents = load_memories(
        remote_id=selected,
        repo_filter=remote if mode == "shared" and remote else None,
        rebuild=args.rebuild,
    )
    if not documents:
        print("no memories to embed")
        return 0
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
    if args.rebuild:
        api(base, "DELETE", f"/collections/{COLLECTION}", missing_ok=True)
    ensure_collection(base)
    points = []
    for document, vector in zip(documents, vectors):
        if len(vector) != DIMENSION:
            raise SystemExit(f"{document['id']} vector has length {len(vector)}")
        points.append(
            {
                "id": point_id(document["id"]),
                "vector": vector,
                "payload": payload_for(document),
            }
        )
    api(base, "PUT", f"/collections/{COLLECTION}/points?wait=true", {"points": points})
    memory_store.mark_embedded(
        [(document["id"], document["updated_at"]) for document in documents],
        remote_id=selected,
    )
    print(
        f"embedded {len(points)} into {COLLECTION} at {base} "
        f"(load {loaded - started:.2f}s, embed {embedded - loaded:.2f}s)"
    )
    for point in points:
        print(f"{point['id']}  {point['payload']['document_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
