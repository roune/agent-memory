"""Test doubles for the server: a deterministic embedder and an in-memory Qdrant.

Not shipped in the package. FakeQdrant is a transport for
agent_memory_server.qdrant.Qdrant, so the client's own requests are what the
tests check, and no test needs Docker or a model.
"""

import hashlib
import json
from urllib.parse import urlsplit

from agent_memory_server.qdrant import QdrantUnavailable


class HashEmbedder:
    """Same text, same vector. dimension and model can be changed to test a switch."""

    provider = "hash"
    distance = "Cosine"

    def __init__(self, dimension=8, model="test/hash", revision="1"):
        self.dimension = dimension
        self.model = model
        self.revision = revision
        self.loads = 0
        self.embedded = []

    def load(self):
        self.loads += 1

    def _vector(self, text):
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(self.dimension)]

    def embed_documents(self, texts):
        texts = list(texts)
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector("query: " + text)


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm = (sum(a * a for a in left) * sum(b * b for b in right)) ** 0.5
    return dot / norm if norm else 0.0


def _matches(payload, query_filter):
    """Qdrant's `must` of keyword matches: a list payload matches when it holds the value."""
    for condition in (query_filter or {}).get("must", []):
        value = payload.get(condition["key"])
        wanted = condition["match"]["value"]
        if value != wanted and not (isinstance(value, list) and wanted in value):
            return False
    return True


def _pick(payload, query):
    chosen = query.get("with_payload", False)
    if chosen is True:
        return payload
    if isinstance(chosen, list):
        return {key: payload[key] for key in chosen if key in payload}
    return {}


class FakeQdrant:
    """The Qdrant endpoints the server calls, in memory. Set down=True to make it unreachable."""

    def __init__(self):
        self.collections = {}
        self.down = False
        self.requests = []

    def __call__(self, method, url, body, headers, timeout):
        if self.down:
            raise QdrantUnavailable("Qdrant is not reachable: connection refused")
        parts = urlsplit(url)
        path = parts.path
        data = json.loads(body) if body else None
        self.requests.append((method, path, data))
        segments = [segment for segment in path.split("/") if segment]
        if path == "/readyz":
            return 200, b"all shards are ready"
        if segments == ["collections"] and method == "GET":
            names = [{"name": name} for name in sorted(self.collections)]
            return self._ok({"collections": names})
        if len(segments) >= 2 and segments[0] == "collections":
            name = segments[1]
            rest = segments[2:]
            found = self.collections.get(name)
            if not rest:
                if method == "GET":
                    if found is None:
                        return 404, b'{"status":{"error":"Not found"}}'
                    return self._ok(
                        {
                            "config": {"params": {"vectors": found["vectors"]}, "metadata": found["metadata"]},
                            "payload_schema": {field: {"data_type": kind} for field, kind in found["indexes"].items()},
                            "points_count": len(found["points"]),
                        }
                    )
                if method == "PUT":
                    if found is not None:
                        return 409, b'{"status":{"error":"already exists"}}'
                    self.collections[name] = {
                        "vectors": data["vectors"],
                        "metadata": data.get("metadata") or {},
                        "indexes": {},
                        "points": {},
                    }
                    return self._ok(True)
                if method == "DELETE":
                    if found is None:
                        return 404, b'{"status":{"error":"Not found"}}'
                    del self.collections[name]
                    return self._ok(True)
            if found is None:
                return 404, b'{"status":{"error":"Not found"}}'
            if rest == ["index"] and method == "PUT":
                found["indexes"][data["field_name"]] = data["field_schema"]
                return self._ok({"status": "completed"})
            if rest == ["points"] and method == "PUT":
                for point in data["points"]:
                    if len(point["vector"]) != found["vectors"]["size"]:
                        return 400, b'{"status":{"error":"Wrong input: Vector dimension error"}}'
                    found["points"][point["id"]] = point
                return self._ok({"status": "completed"})
            if rest == ["points", "count"] and method == "POST":
                return self._ok({"count": len(found["points"])})
            if rest == ["points", "query"] and method == "POST":
                if len(data["query"]) != found["vectors"]["size"]:
                    return 400, b'{"status":{"error":"Wrong input: Vector dimension error"}}'
                hits = [
                    {"id": point["id"], "score": _cosine(data["query"], point["vector"]), "payload": _pick(point["payload"], data)}
                    for point in found["points"].values()
                    if _matches(point["payload"], data.get("filter"))
                ]
                hits.sort(key=lambda hit: hit["score"], reverse=True)
                return self._ok({"points": hits[: data.get("limit", 10)]})
        return 404, b'{"status":{"error":"no such endpoint in FakeQdrant"}}'

    @staticmethod
    def _ok(result):
        return 200, json.dumps({"result": result, "status": "ok"}).encode()

    def payloads(self, name):
        return {point["payload"]["document_id"]: point["payload"] for point in self.collections[name]["points"].values()}
