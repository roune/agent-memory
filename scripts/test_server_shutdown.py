#!/usr/bin/env python3
"""The server stops on SIGTERM within Docker's stop timeout, even with a request held open.

Needs the server extra: scripts/test-server runs it. Under the plain python3 it is skipped.
"""

import importlib.util
import json
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
HAVE_SERVER = all(importlib.util.find_spec(name) for name in ("starlette", "uvicorn", "mcp"))
DOCKER_STOP_TIMEOUT = 10

# A toy app with a request that never ends: only the grace timeout can stop it.
HOLDING_SERVER = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route
from agent_memory_server.cli import _uvicorn

async def forever():
    yield b"open\\n"
    while True:
        await asyncio.sleep(1)
        yield b"."

async def hold(request):
    return StreamingResponse(forever(), media_type="text/plain")

_uvicorn(Starlette(routes=[Route("/hold", hold)]), "127.0.0.1", int(sys.argv[2]))
"""

# The real app, auth off, with test doubles for the model and Qdrant.
REAL_SERVER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from agent_memory_server.app import create_app
from agent_memory_server.cli import _uvicorn
from agent_memory_server.embedding import Embedding
from agent_memory_server.qdrant import Qdrant
from agent_memory_server.settings import Settings
from agent_memory_server.store import ServerStore
from server_doubles import FakeQdrant, HashEmbedder

port, data = int(sys.argv[2]), Path(sys.argv[3])
settings = Settings(data_dir=data, models_dir=data / "models", auth="off", allowed_hosts=(f"127.0.0.1:{port}",))
store = ServerStore(settings.db_path)
embedding = Embedding(store, HashEmbedder(), Qdrant("http://qdrant:6333", transport=FakeQdrant()))
_uvicorn(create_app(settings, store, embedding), "127.0.0.1", port)
"""

LISTEN_VERSION = "2026-07-28"


@unittest.skipUnless(HAVE_SERVER, "needs the server extra (scripts/test-server)")
class GracefulShutdownTest(unittest.TestCase):
    def start(self, script, *args):
        """Run script in a subprocess on a free port. Returns (process, a connected socket)."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        log = tempfile.TemporaryFile()
        self.addCleanup(log.close)
        server = subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(SCRIPTS), str(port), *args],
            stdout=log,
            stderr=subprocess.STDOUT,
        )

        def end():
            if server.poll() is None:
                server.kill()
                server.wait()

        self.addCleanup(end)
        self.log = log
        deadline = time.monotonic() + 15
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                if server.poll() is not None:
                    self.fail(f"the server exited before it listened:\n{self.output()}")
                if time.monotonic() > deadline:
                    self.fail(f"the server did not listen:\n{self.output()}")
                time.sleep(0.05)
        self.addCleanup(client.close)
        client.settimeout(5)
        return server, client, port

    def output(self):
        self.log.seek(0)
        return self.log.read().decode(errors="replace")[-2000:]

    def read_until(self, client, marker):
        received = b""
        while marker not in received:
            chunk = client.recv(4096)
            self.assertTrue(chunk, f"the server closed the request before SIGTERM:\n{received!r}")
            received += chunk
        return received

    def stop(self, server):
        """SIGTERM, then the seconds the process took to exit."""
        started = time.monotonic()
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=DOCKER_STOP_TIMEOUT + 5)
        except subprocess.TimeoutExpired:
            self.fail(f"the server was still running after SIGTERM, as it would be when Docker kills it:\n{self.output()}")
        return time.monotonic() - started

    def test_sigterm_stops_the_server_with_a_request_still_open(self):
        from agent_memory_server.cli import GRACEFUL_SHUTDOWN_SECONDS

        server, client, port = self.start(HOLDING_SERVER)
        client.sendall(f"GET /hold HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        self.read_until(client, b"open")
        took = self.stop(server)
        self.assertLess(took, DOCKER_STOP_TIMEOUT - 2, f"the server took {took:.1f}s to stop")
        self.assertGreaterEqual(took, GRACEFUL_SHUTDOWN_SECONDS - 1, "the open request was not given its grace period")

    def test_a_subscriptions_listen_request_does_not_hold_shutdown(self):
        from agent_memory_server.cli import GRACEFUL_SHUTDOWN_SECONDS

        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        server, client, port = self.start(REAL_SERVER, data.name)
        meta = {
            "io.modelcontextprotocol/protocolVersion": LISTEN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
        }
        listen = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "subscriptions/listen",
                "params": {"notifications": {"toolsListChanged": True}, "_meta": meta},
            }
        ).encode()
        client.sendall(
            (
                f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                f"Accept: application/json, text/event-stream\r\nMCP-Protocol-Version: {LISTEN_VERSION}\r\n"
                f"Mcp-Method: subscriptions/listen\r\nContent-Length: {len(listen)}\r\n\r\n"
            ).encode()
            + listen
        )
        self.read_until(client, b"HTTP/1.1")
        took = self.stop(server)
        self.assertLess(
            took, GRACEFUL_SHUTDOWN_SECONDS - 1, f"shutdown waited {took:.1f}s on the listen request:\n{self.output()}"
        )


if __name__ == "__main__":
    unittest.main()
