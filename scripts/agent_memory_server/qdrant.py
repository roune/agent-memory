"""A small Qdrant HTTP client. Standard library only.

The transport is injectable: transport(method, url, body, headers, timeout)
returns (status, body bytes), or raises QdrantUnavailable when Qdrant can't be
reached. Tests pass an in-memory Qdrant, so none of them needs Docker.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class QdrantError(Exception):
    """Qdrant answered with an error."""


class QdrantUnavailable(QdrantError):
    """Qdrant could not be reached."""


def urllib_transport(method, url, body, headers, timeout):
    request = Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except (URLError, OSError) as error:
        raise QdrantUnavailable(f"Qdrant is not reachable: {getattr(error, 'reason', error)}") from error


class Qdrant:
    def __init__(self, url, api_key="", transport=None, timeout=10.0):
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.transport = transport or urllib_transport
        self.timeout = timeout

    def _call(self, method, path, body=None, missing_ok=False):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        data = None if body is None else json.dumps(body).encode()
        status, raw = self.transport(method, self.url + path, data, headers, self.timeout)
        if missing_ok and status == 404:
            return None
        if status >= 400:
            detail = raw.decode(errors="replace")[:500] if raw else ""
            raise QdrantError(f"Qdrant {method} {path} failed: {status} {detail}".strip())
        return json.loads(raw) if raw else {}

    def ready(self):
        try:
            status, _ = self.transport("GET", self.url + "/readyz", None, {}, min(self.timeout, 2.0))
        except QdrantUnavailable:
            return False
        return status == 200

    def collection(self, name):
        """The collection's info (Qdrant's `result`), or None when it doesn't exist."""
        found = self._call("GET", f"/collections/{name}", missing_ok=True)
        return None if found is None else found.get("result") or {}

    def collection_names(self):
        found = self._call("GET", "/collections")
        return sorted(item["name"] for item in (found.get("result") or {}).get("collections") or [])

    def create_collection(self, name, size, distance, metadata=None):
        body = {"vectors": {"size": size, "distance": distance}}
        if metadata:
            body["metadata"] = metadata
        self._call("PUT", f"/collections/{name}", body)

    def create_index(self, name, field, schema):
        self._call("PUT", f"/collections/{name}/index?wait=true", {"field_name": field, "field_schema": schema})

    def delete_collection(self, name):
        self._call("DELETE", f"/collections/{name}", missing_ok=True)

    def upsert(self, name, points):
        self._call("PUT", f"/collections/{name}/points?wait=true", {"points": points})

    def query(self, name, body):
        """The universal query API. Returns Qdrant's points, or None when the collection is missing."""
        found = self._call("POST", f"/collections/{name}/points/query", body, missing_ok=True)
        if found is None:
            return None
        return (found.get("result") or {}).get("points") or []

    def count(self, name):
        found = self._call("POST", f"/collections/{name}/points/count", {"exact": True})
        return int((found.get("result") or {}).get("count", 0))


def vector_size(info):
    """The vector size a collection was created with, from Qdrant's collection info."""
    vectors = ((info.get("config") or {}).get("params") or {}).get("vectors") or {}
    return vectors.get("size")
