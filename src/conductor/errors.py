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
