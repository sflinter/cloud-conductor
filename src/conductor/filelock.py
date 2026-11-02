# Copyright (c) 2026 Steve Flinter. MIT License.
"""Cross-process lock for the state file and the pod registry.

Both files are read-modify-written by the orchestrator (including its pool
threads) and by concurrent CLI commands. flock() on a hidden `.lock`
sibling serialises them; each open() gets its own file description, so
threads in one process exclude each other too. The lock is not reentrant:
do not call a locking function from inside a locked region."""
from __future__ import annotations

import contextlib
import os

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


def lock_path(path: str) -> str:
    d, name = os.path.split(str(path))
    return os.path.join(d, f".{name}.lock")


@contextlib.contextmanager
def locked(path: str):
    """Hold an exclusive lock associated with `path` (not on the file itself,
    so atomic os.replace() of the file keeps working)."""
    lp = lock_path(path)
    os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
