"""Where the laptop's own state lives, and the one-time move there. Standard library only.

The state is `inbox/`, `outbox/`, `review.json` and `capture.log`. It lives in
`${XDG_DATA_HOME:-~/.local/share}/agent-memory/`, or in AGENT_MEMORY_DIR when
that is set. A tool install (`uv tool install`) has no clone to keep it in.

Before that, it lived in the clone, or in AGENT_MEMORY_DIR, in this layout:

    inbox/  outbox/  data/review.json  data/capture.log

`move(old, new)` moves that layout into the new one. It never overwrites: a
directory that exists in both places is merged entry by entry, and a file whose
name is taken stays where it was and is reported. Two exceptions, both safe:
an old `capture.log` is appended to the new one, and an old `review.json` (the
pid and port of a review page) is dropped when a new one exists. An old file
that is empty or the same as the new one is dropped too, such as `outbox/.lock`.

Once the old paths are gone, a second run finds nothing and does nothing.
"""

import errno
import os
import shutil
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no flock; the move then runs unlocked.
    fcntl = None

# (path in the old layout, path in the new one, what to do when both exist)
LAYOUT = (
    ("inbox", "inbox", "merge"),
    ("outbox", "outbox", "merge"),
    ("data/review.json", "review.json", "drop-old"),
    ("data/capture.log", "capture.log", "append"),
)


def default_root():
    """${XDG_DATA_HOME:-~/.local/share}/agent-memory. A relative XDG_DATA_HOME is ignored, as the spec says."""
    xdg = os.environ.get("XDG_DATA_HOME", "")
    base = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".local" / "share"
    return base / "agent-memory"


def root():
    override = os.environ.get("AGENT_MEMORY_DIR")
    return Path(override) if override else default_root()


def ensure_root():
    """Create the default root, mode 0700: the inbox holds transcript copies. AGENT_MEMORY_DIR is the caller's own."""
    if os.environ.get("AGENT_MEMORY_DIR"):
        return
    path = default_root()
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)


def is_clone(path):
    """A checkout of this repo: the place the state lived before the move."""
    path = Path(path)
    return (path / "bin" / "agent-memory").is_file() and (path / "scripts" / "agent_memory.py").is_file()


def pending(old_root, new_root):
    """The old paths that a move would still handle."""
    old_root, new_root = Path(old_root), Path(new_root)
    found = []
    for old, new, _ in LAYOUT:
        source = old_root / old
        if _exists(source) and _absolute(source) != _absolute(new_root / new):
            found.append(source)
    return found


def move(old_root, new_root):
    """Move the old layout under old_root into new_root. Returns (moved, left).

    moved: (old path, new path) pairs. left: (old path, reason) pairs, for what
    stayed because its name was taken. Raises OSError if the file system does.
    """
    old_root, new_root = Path(old_root), Path(new_root)
    if not pending(old_root, new_root):
        return [], []
    if not new_root.exists():
        new_root.mkdir(parents=True, mode=0o700)
    moved, left = [], []
    with _lock(new_root):
        for old, new, when_both in LAYOUT:
            source, target = old_root / old, new_root / new
            if not _exists(source) or _absolute(source) == _absolute(target):
                continue
            if not _exists(target):
                if _rename(source, target):
                    moved.append((source, target))
            elif when_both == "merge" and _is_dir(source) and _is_dir(target):
                before = len(left)
                _merge(source, target, left)
                if len(left) == before or not _exists(source):
                    moved.append((source, target))
            elif when_both == "append" and _is_file(source) and _is_file(target):
                with source.open("rb") as handle, target.open("ab") as out:
                    shutil.copyfileobj(handle, out)
                source.unlink()
                moved.append((source, target))
            elif when_both == "drop-old" and _is_file(source):
                source.unlink()
            elif _same_or_empty(source, target):
                source.unlink()
            else:
                left.append((source, f"{target} already exists"))
        _remove_if_empty(old_root / "data")
    return moved, left


def _merge(source, target, left):
    """Move each entry of directory source into directory target, never overwriting."""
    for child in sorted(source.iterdir()):
        into = target / child.name
        if not _exists(into):
            _rename(child, into)
        elif _is_dir(child) and _is_dir(into):
            _merge(child, into, left)
        elif _same_or_empty(child, into):
            child.unlink()
        else:
            left.append((child, f"{into} already exists"))
    _remove_if_empty(source)


def _rename(source, target):
    """Move one path. Across file systems a directory is moved entry by entry,
    so a file written into it meanwhile stays behind instead of being lost.
    Returns False when another process moved it first."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, target)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    if _is_dir(source):
        target.mkdir(mode=source.stat().st_mode & 0o777)
        _merge(source, target, [])
    else:
        shutil.move(str(source), str(target))
    return True


def _same_or_empty(source, target):
    if not (_is_file(source) and _is_file(target)):
        return False
    size = source.stat().st_size
    if size == 0:
        return True
    if size != target.stat().st_size:
        return False
    return source.read_bytes() == target.read_bytes()


def _remove_if_empty(path):
    try:
        path.rmdir()
    except OSError:
        pass


def _exists(path):
    return path.exists() or path.is_symlink()


def _is_dir(path):
    return path.is_dir() and not path.is_symlink()


def _is_file(path):
    return path.is_file() and not path.is_symlink()


def _absolute(path):
    return Path(os.path.abspath(path))


class _lock:
    """flock on <new root>/.move.lock, so two hooks starting at once move once."""

    def __init__(self, folder):
        self.path = folder / ".move.lock"
        self.handle = None

    def __enter__(self):
        if fcntl is not None:
            self.handle = open(self.path, "a+")
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
        return False
