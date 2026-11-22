"""The server lock: /data/server.lock, held by `serve` and taken by `reindex`.

An exclusive flock, so reindex can't run while the server is up and race its
retry loop. The kernel drops the lock when the process exits, even on a crash.
"""

import fcntl
import os
from pathlib import Path


class ServerLock:
    def __init__(self, path):
        self.path = Path(path)
        self._file = None

    def acquire(self):
        """True when this process now holds the lock, False when another one does."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._file = handle
        return True

    def release(self):
        if self._file is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()
            self._file = None
