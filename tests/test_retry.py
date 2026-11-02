from unittest.mock import MagicMock, patch
import subprocess

import httpx
import pytest

from conductor.errors import ConductorError
from conductor.retry import retry, is_retryable


def _status_error(code):
    resp = MagicMock()
    resp.status_code = code
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


def test_retry_returns_first_success():
    fn = MagicMock(side_effect=[OSError("a"), ConductorError("b"), "ok"])
    with patch("conductor.retry._sleep") as sleep:
        assert retry(fn, attempts=4, base=1.0, jitter=False) == "ok"
    assert fn.call_count == 3
    assert [c[0][0] for c in sleep.call_args_list] == [1.0, 2.0]


def test_retry_backoff_is_capped_and_jittered():
    fn = MagicMock(side_effect=[OSError("x")] * 5 + ["ok"])
    with patch("conductor.retry._sleep") as sleep, patch("conductor.retry.random.random", return_value=1.0):
        assert retry(fn, attempts=6, base=4.0, max_delay=10.0, jitter=True) == "ok"
    delays = [c[0][0] for c in sleep.call_args_list]
    # base 4 → 8 → capped 10, each multiplied by (0.5 + 1.0)
    assert delays == [6.0, 12.0, 15.0, 15.0, 15.0]


def test_retry_gives_up_after_attempts():
    fn = MagicMock(side_effect=subprocess.TimeoutExpired("x", 1))
    with pytest.raises(subprocess.TimeoutExpired):
        retry(fn, attempts=3, base=0)
    assert fn.call_count == 3


def test_retry_does_not_retry_programming_errors():
    fn = MagicMock(side_effect=TypeError("bug"))
    with pytest.raises(TypeError):
        retry(fn, attempts=3)
    assert fn.call_count == 1


@pytest.mark.parametrize("code,expected", [(429, True), (500, True), (502, True), (503, True),
                                           (404, False), (400, False), (401, False), (403, False)])
def test_is_retryable_http_status(code, expected):
    assert is_retryable(_status_error(code)) is expected
    wrapped = ConductorError("vast.ai: ...")
    wrapped.__cause__ = _status_error(code)
    assert is_retryable(wrapped) is expected


def test_retry_respects_http_status():
    fn = MagicMock(side_effect=[_status_error(404)])
    with pytest.raises(httpx.HTTPStatusError):
        retry(fn, attempts=4)
    assert fn.call_count == 1

    fn = MagicMock(side_effect=[_status_error(429), _status_error(503), "ok"])
    assert retry(fn, attempts=4, base=0) == "ok"


def test_is_retryable_runpod_errors():
    from runpod import error as runpod_error
    assert is_retryable(runpod_error.QueryError("Something went wrong")) is True
    assert is_retryable(runpod_error.AuthenticationError("bad key")) is False
    assert is_retryable(httpx.ConnectError("refused")) is True
    assert is_retryable(ValueError("x")) is False
