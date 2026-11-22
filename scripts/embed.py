"""Embed approved memories from the local SQLite store into Qdrant.

Reads the memory table. Talks to the configured Qdrant URL over HTTP.
Does not open data/qdrant. The model comes from scripts/embedder.py.
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
import embedder
import memory_store

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


def models_dir():
    """Where a local model keeps its checkpoint: data/models under the store root.

    The store root is AGENT_MEMORY_DIR when it is set, otherwise the repo, so the
    bge-small checkpoint stays at data/models/bge-small-en-v1.5-onnx-q.
    """
    return memory_store.store_root() / "data" / "models"


def load_model():
    """The configured embedder (fastembed bge-small by default). Weights load on first use."""
    try:
        return embedder.load_embedder(embedder.config_from_env(), models_dir())
    except embedder.EmbedderError as error:
        raise SystemExit(str(error)) from error


def load_memories(remote_id=None, repo_filter=None, rebuild=False):
    return memory_store.pending_documents(
        remote_id=remote_id,
        repo_filter=repo_filter,
        rebuild=rebuild,
    )


def ensure_collection(base, model):
    info = api(base, "GET", f"/collections/{COLLECTION}", missing_ok=True)
    if info is None:
        api(
            base,
            "PUT",
            f"/collections/{COLLECTION}",
            {"vectors": {"size": model.dimension, "distance": model.distance}},
        )
        info = api(base, "GET", f"/collections/{COLLECTION}")
    else:
        size = info["result"]["config"]["params"]["vectors"]["size"]
        if size != model.dimension:
            raise SystemExit(
                f"collection {COLLECTION} has dimension {size}, "
                f"{model.model} has {model.dimension}. Run scripts/embed --rebuild"
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


def payload_for(
    document,
    model_name=embedder.FastEmbedBge.model,
    revision=embedder.FastEmbedBge.revision,
):
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
        "embedding_model": model_name,
        "embedding_revision": revision,
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


def points_for(documents, vectors, model):
    """Qdrant points for these documents. Stops on a vector of the wrong length."""
    if len(vectors) != len(documents):
        raise SystemExit(f"{len(documents)} documents but {len(vectors)} vectors")
    points = []
    for document, vector in zip(documents, vectors):
        if len(vector) != model.dimension:
            raise SystemExit(
                f"{document['id']} vector has length {len(vector)}, "
                f"{model.model} has {model.dimension}"
            )
        points.append(
            {
                "id": point_id(document["id"]),
                "vector": vector,
                "payload": payload_for(document, model.model, model.revision),
            }
        )
    return points


def check_rebuild(mode, remote):
    """Stop a --rebuild that would delete points it does not embed again.

    --rebuild deletes the whole collection at that Qdrant URL. In shared mode
    --repo embeds only one repo's rows, and in per-repo mode another remote can
    point at the same URL. Either way the other points would vanish while their
    rows stay marked embedded.
    """
    if mode == "shared" and remote:
        raise SystemExit(
            "--rebuild rebuilds every repo in shared mode; run it without --repo"
        )
    if mode == "per-repo":
        url = memory_store.qdrant_url(remote)
        others = sorted(
            other
            for other, other_url in memory_store.remote_urls().items()
            if other != remote and other_url.strip().rstrip("/") == url
        )
        if others:
            raise SystemExit(
                f"--rebuild would delete the points of {', '.join(others)}, "
                f"which share {url} with {remote}"
            )


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
    model = load_model()
    try:
        base = memory_store.qdrant_url(selected)
        if args.rebuild:
            check_rebuild(mode, remote)
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
    started = time.perf_counter()
    try:
        model.load()
        loaded = time.perf_counter()
        vectors = model.embed_documents([document["text"] for document in documents])
    except embedder.EmbedderError as error:
        raise SystemExit(str(error)) from error
    embedded = time.perf_counter()
    points = points_for(documents, vectors, model)
    if args.rebuild:
        api(base, "DELETE", f"/collections/{COLLECTION}", missing_ok=True)
    ensure_collection(base, model)
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
