#!/usr/bin/env python3
"""Embedder interface and how embed.py uses it. No model weights, no Qdrant."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import embedder


def load_embed():
    spec = importlib.util.spec_from_file_location("embed_interface_under_test", SCRIPTS / "embed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Vector:
    """Stands in for the numpy array FastEmbed yields."""

    def __init__(self, values):
        self.values = values

    def tolist(self):
        return list(self.values)


class RecordingBackend:
    def __init__(self, dimension=384):
        self.dimension = dimension
        self.seen = []

    def embed(self, texts):
        self.seen.extend(texts)
        for index, _ in enumerate(texts):
            yield Vector([float(index)] * self.dimension)


class SmallEmbedder:
    provider = "test"
    model = "test/small"
    revision = "r1"
    dimension = 8
    distance = "Cosine"

    def load(self):
        return self

    def embed_documents(self, texts):
        return [[0.5] * self.dimension for _ in texts]

    def embed_query(self, text):
        return [0.5] * self.dimension


class LoadEmbedderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.models = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_pinned_fastembed_bge_small(self):
        model = embedder.load_embedder({}, self.models)
        self.assertIsInstance(model, embedder.FastEmbedBge)
        self.assertEqual(
            (model.provider, model.model, model.revision, model.dimension, model.distance),
            (
                "fastembed",
                "BAAI/bge-small-en-v1.5",
                "aa8f8b060edb00e03bfdd08813a2949946c8ba55",
                384,
                "Cosine",
            ),
        )
        self.assertEqual(model.model_dir, self.models / "bge-small-en-v1.5-onnx-q")

    def test_blank_env_means_the_default(self):
        config = embedder.config_from_env({})
        self.assertEqual(config, {"provider": "", "model": ""})
        self.assertIsInstance(embedder.load_embedder(config, self.models), embedder.FastEmbedBge)

    def test_unimplemented_provider_is_a_clear_error(self):
        for name in ("openai", "Bedrock", " other "):
            config = embedder.config_from_env({embedder.PROVIDER_ENV: name})
            with self.assertRaises(embedder.EmbedderError) as raised:
                embedder.load_embedder(config, self.models)
            message = str(raised.exception)
            self.assertIn(repr(name.strip().lower()), message)
            self.assertIn("not implemented", message)
            self.assertIn("implemented: fastembed", message)
            self.assertIn("Adding a provider", message)

    def test_fastembed_refuses_another_model(self):
        config = embedder.config_from_env(
            {embedder.PROVIDER_ENV: "fastembed", embedder.MODEL_ENV: "BAAI/bge-base-en-v1.5"}
        )
        with self.assertRaises(embedder.EmbedderError) as raised:
            embedder.load_embedder(config, self.models)
        self.assertIn("BAAI/bge-base-en-v1.5", str(raised.exception))
        self.assertIn("aa8f8b060edb00e03bfdd08813a2949946c8ba55", str(raised.exception))

    def test_loading_the_config_does_not_load_weights(self):
        model = embedder.load_embedder({}, self.models)
        self.assertFalse(model.model_dir.exists())


class FastEmbedBgeTest(unittest.TestCase):
    def setUp(self):
        self.backend = RecordingBackend()
        self.model = embedder.load_embedder({}, "/unused", backend=self.backend)

    def test_documents_are_embedded_as_they_are(self):
        vectors = self.model.embed_documents(["first note", "second note"])
        self.assertEqual(self.backend.seen, ["first note", "second note"])
        self.assertEqual(len(vectors), 2)
        self.assertTrue(all(len(vector) == 384 for vector in vectors))
        self.assertIsInstance(vectors[1][0], float)

    def test_query_gets_the_bge_retrieval_prefix(self):
        vector = self.model.embed_query("why did we drop the JSON importer")
        self.assertEqual(
            self.backend.seen,
            ["Represent this sentence for searching relevant passages: why did we drop the JSON importer"],
        )
        self.assertEqual(len(vector), 384)

    def test_wrong_length_from_the_model_is_an_error(self):
        model = embedder.load_embedder({}, "/unused", backend=RecordingBackend(dimension=383))
        with self.assertRaises(embedder.EmbedderError) as raised:
            model.embed_documents(["note"])
        self.assertIn("383", str(raised.exception))


class EmbedScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {name: os.environ.get(name) for name in ("HOME", "AGENT_MEMORY_DIR", embedder.PROVIDER_ENV)}
        os.environ["HOME"] = str(Path(self.tmp.name) / "home")
        os.environ["AGENT_MEMORY_DIR"] = str(Path(self.tmp.name) / "store")
        os.environ.pop(embedder.PROVIDER_ENV, None)
        self.embed = load_embed()

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def document(self, document_id="person-learning-1"):
        return {
            "id": document_id,
            "kind": "learning",
            "domain": "code",
            "repo": "github.com/example/proj",
            "tool": "git",
            "session_id": "1",
            "text": "a note",
        }

    def test_checkpoint_lives_under_the_store(self):
        model = self.embed.load_model()
        self.assertEqual(
            model.model_dir,
            Path(self.tmp.name) / "store" / "data" / "models" / "bge-small-en-v1.5-onnx-q",
        )

    def test_points_carry_the_embedder_model_and_revision(self):
        model = SmallEmbedder()
        points = self.embed.points_for([self.document()], model.embed_documents(["a note"]), model)
        payload = points[0]["payload"]
        self.assertEqual((payload["embedding_model"], payload["embedding_revision"]), ("test/small", "r1"))
        self.assertEqual(len(points[0]["vector"]), 8)

    def test_points_stop_on_a_vector_of_the_wrong_length(self):
        with self.assertRaises(SystemExit) as raised:
            self.embed.points_for([self.document()], [[0.1] * 7], SmallEmbedder())
        self.assertIn("length 7", str(raised.exception.code))

    def test_existing_collection_of_another_dimension_is_refused(self):
        calls = []

        def api(base, method, path, body=None, missing_ok=False):
            calls.append((method, path))
            return {"result": {"config": {"params": {"vectors": {"size": 384}}}}}

        self.embed.api = api
        with self.assertRaises(SystemExit) as raised:
            self.embed.ensure_collection("http://qdrant", SmallEmbedder())
        self.assertIn("dimension 384", str(raised.exception.code))
        self.assertIn("test/small has 8", str(raised.exception.code))
        self.assertEqual(calls, [("GET", "/collections/memories")])

    def test_new_collection_uses_the_embedder_size_and_distance(self):
        calls = []

        def api(base, method, path, body=None, missing_ok=False):
            calls.append((method, path, body))
            if method == "GET" and missing_ok:
                return None
            return {"result": {"payload_schema": {}}}

        self.embed.api = api
        self.embed.ensure_collection("http://qdrant", SmallEmbedder())
        self.assertEqual(calls[1], ("PUT", "/collections/memories", {"vectors": {"size": 8, "distance": "Cosine"}}))

    def test_unimplemented_provider_stops_before_qdrant(self):
        os.environ[embedder.PROVIDER_ENV] = "openai"

        def ready(url):
            raise AssertionError("embed asked Qdrant before checking the embedder")

        with mock.patch.object(self.embed.memory_store, "container_ready", ready):
            with self.assertRaises(SystemExit) as raised:
                self.embed.main([])
        self.assertIn("'openai' is not implemented", str(raised.exception.code))


if __name__ == "__main__":
    unittest.main()
