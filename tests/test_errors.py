import json
import subprocess

import httpx
import pytest

from conductor.errors import ConductorError, guarded, describe


def test_guarded_passes_through_result():
    assert guarded("x", lambda a, b=1: a + b, 2, b=3) == 5


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="ssh", timeout=1),
    OSError("boom"),
    json.JSONDecodeError("bad", "{", 0),
    httpx.ConnectError("refused"),
    KeyError("data"),
])
def test_guarded_converts_transient_errors(exc):
    def fn():
        raise exc
    with pytest.raises(ConductorError) as info:
        guarded("call", fn)
    assert "call:" in str(info.value)
    assert info.value.__cause__ is exc


def test_guarded_converts_runpod_errors():
    from runpod import error as runpod_error

    def fn():
        raise runpod_error.QueryError("rate limited")
    with pytest.raises(ConductorError, match="rate limited"):
        guarded("runpod.get_pod", fn)


def test_guarded_leaves_programming_errors_alone():
    def fn():
        raise TypeError("bug")
    with pytest.raises(TypeError):
        guarded("call", fn)


def test_describe():
    assert describe(ValueError("x")) == "ValueError: x"
    assert describe(ValueError()) == "ValueError"
