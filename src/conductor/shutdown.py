# Copyright (c) 2026 Steve Flinter. MIT License.
"""Process-wide shutdown flag.

The first Ctrl-C sets it. Every long wait in the provisioning pipeline
(provider SSH-info polling, the SSH readiness probe, retry backoffs) checks
it between polls so a single Ctrl-C detaches within one poll interval
instead of after a provision timeout."""
from __future__ import annotations

import threading


class Interrupted(Exception):
    """A wait was abandoned because shutdown was requested. The pipeline
    treats it as "stop here, keep everything as it is", never as a failure."""


_event = threading.Event()


def request() -> None:
    _event.set()


def clear() -> None:
    _event.clear()


def requested() -> bool:
    return _event.is_set()


def wait(seconds: float) -> bool:
    """Sleep for up to `seconds`. Returns True (early) if shutdown is requested."""
    return _event.wait(max(0.0, seconds))


def check(what: str = "operation") -> None:
    """Raise Interrupted if shutdown has been requested."""
    if _event.is_set():
        raise Interrupted(f"shutdown requested during {what}")
