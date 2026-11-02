# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import random
from typing import Callable, TypeVar

import httpx

from conductor import shutdown
from conductor.errors import ConductorError, TRANSIENT_ERRORS, describe

log = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _sleep(seconds: float) -> None:
    # Returns early when shutdown is requested: the next attempt runs at once
    shutdown.wait(seconds)


def is_retryable(exc: BaseException) -> bool:
    """Transport errors, timeouts, 429 and 5xx are worth retrying; a 4xx
    other than 429 or an authentication failure is not."""
    status = _http_status(exc)
    if status is not None:
        return status in RETRYABLE_HTTP_STATUS
    try:
        from runpod import error as runpod_error
        if isinstance(exc, runpod_error.AuthenticationError):
            return False
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, ConductorError) and exc.__cause__ is not None:
        return is_retryable(exc.__cause__)
    return isinstance(exc, (ConductorError, *TRANSIENT_ERRORS))


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def retry(
    fn: Callable[..., T],
    *args,
    attempts: int = 4,
    base: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retry_on: tuple[type[BaseException], ...] = (ConductorError, *TRANSIENT_ERRORS),
    should_retry: Callable[[BaseException], bool] = is_retryable,
    what: str = "",
    **kwargs,
) -> T:
    """Call fn(*args, **kwargs) with exponential backoff and jitter.

    An exception is retried only if it is an instance of retry_on and
    should_retry(exc) is true; the last one is re-raised."""
    what = what or getattr(fn, "__name__", "call")
    delay = base
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retry_on as e:
            if attempt >= attempts or not should_retry(e):
                raise
            wait = min(delay, max_delay)
            if jitter:
                wait = wait * (0.5 + random.random())
            log.warning(f"{what} failed (attempt {attempt}/{attempts}): {describe(e)}; "
                        f"retrying in {wait:.1f}s")
            _sleep(wait)
            delay = min(delay * 2, max_delay)
    raise AssertionError("unreachable")  # pragma: no cover
