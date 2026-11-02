# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import json
import subprocess
from typing import Callable, TypeVar

import httpx

try:
    import requests
    _REQUESTS_ERRORS: tuple[type[BaseException], ...] = (requests.RequestException,)
except ImportError:  # pragma: no cover
    _REQUESTS_ERRORS = ()

try:
    from runpod import error as _runpod_error
    _RUNPOD_ERRORS: tuple[type[BaseException], ...] = (_runpod_error.RunPodError,)
except ImportError:  # pragma: no cover
    _RUNPOD_ERRORS = ()


class ConductorError(Exception):
    """A well-defined failure of an SSH, rsync or provider call.

    Raised in place of the raw transport/SDK exceptions so callers can mark a
    job failed instead of letting the orchestrator die."""


class SshAuthError(ConductorError):
    """sshd answered but rejected our key. Waiting longer or trying another
    GPU with the same key cannot help, so the job fails immediately."""


class NoCapacityError(ConductorError):
    """The provider had no instance of the requested GPU type right now.
    Worth retrying the whole candidate list after a backoff."""


# Provider messages that mean "nothing available right now", not "bad request"
CAPACITY_PATTERNS = (
    "no longer any instances available",
    "no instances available",
    "not available",
    "no available",
    "out of capacity",
    "insufficient capacity",
    "no capacity",
    "out of stock",
)


def is_capacity_message(text: str) -> bool:
    lowered = (text or "").lower()
    return any(p in lowered for p in CAPACITY_PATTERNS)


# Provider answers to a terminate/delete of an instance that is already gone
NOT_FOUND_PATTERNS = (
    "not found",
    "no such instance",
    "does not exist",
    "no longer exists",
)


def is_not_found_message(text: str) -> bool:
    lowered = (text or "").lower()
    return any(p in lowered for p in NOT_FOUND_PATTERNS)


# Exceptions that mean "the outside world misbehaved", not "the code is wrong".
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    subprocess.TimeoutExpired,
    OSError,
    json.JSONDecodeError,
    KeyError,
    httpx.HTTPError,
    *_REQUESTS_ERRORS,
    *_RUNPOD_ERRORS,
)

T = TypeVar("T")


def guarded(what: str, fn: Callable[..., T], *args, **kwargs) -> T:
    """Call fn, converting transient transport/SDK errors into ConductorError."""
    try:
        return fn(*args, **kwargs)
    except ConductorError:
        raise
    except TRANSIENT_ERRORS as e:
        raise ConductorError(f"{what}: {type(e).__name__}: {e}") from e


def describe(e: BaseException) -> str:
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__
