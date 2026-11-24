#!/usr/bin/env python3
"""The server stops on SIGTERM within Docker's stop timeout, even with a request held open.

Needs the server extra: scripts/test-server runs it. Under the plain python3 it is skipped.
"""

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
HAVE_SERVER = all(importlib.util.find_spec(name) for name in ("starlette", "uvicorn"))
DOCKER_STOP_TIMEOUT = 10

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


@unittest.skipUnless(HAVE_SERVER, "needs the server extra (scripts/test-server)")
class GracefulShutdownTest(unittest.TestCase):
    def test_sigterm_stops_the_server_with_a_request_still_open(self):
        from agent_memory_server.cli import GRACEFUL_SHUTDOWN_SECONDS

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen(
            [sys.executable, "-c", HOLDING_SERVER, str(SCRIPTS), str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.addCleanup(lambda: server.poll() is None and server.kill())
        self.addCleanup(server.stderr.close)

        deadline = time.monotonic() + 15
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", port), timeout=1)
                break
            except OSError:
                self.assertIsNone(server.poll(), "the server exited before it listened")
                self.assertLess(time.monotonic(), deadline, "the server did not listen")
                time.sleep(0.05)
        self.addCleanup(client.close)
        client.sendall(f"GET /hold HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        client.settimeout(5)
        received = b""
        while b"open" not in received:
            chunk = client.recv(4096)
            self.assertTrue(chunk, "the server closed the held request before SIGTERM")
            received += chunk

        started = time.monotonic()
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=DOCKER_STOP_TIMEOUT + 5)
        except subprocess.TimeoutExpired:
            self.fail("the server was still running after SIGTERM, as it would be when Docker kills it")
        took = time.monotonic() - started
        self.assertLess(took, DOCKER_STOP_TIMEOUT - 2, f"the server took {took:.1f}s to stop")
        self.assertGreaterEqual(took, GRACEFUL_SHUTDOWN_SECONDS - 1, "the open request was not given its grace period")


if __name__ == "__main__":
    unittest.main()
