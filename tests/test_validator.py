import os
import stat
import tempfile

import pytest

from conductor.config import JobConfig
from conductor.validator import validate


def _make_config(**overrides):
    defaults = dict(name="test", run_command="echo hi", ssh_key_path="/nonexistent/key",
                    local_project_dir=".", budget_usd=40.0)
    defaults.update(overrides)
    return JobConfig(**defaults)


def test_valid_config():
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o600)
        cfg = _make_config(ssh_key_path=f.name)
        result = validate([cfg], check_gpu=False)
        assert result.ok


def test_missing_ssh_key():
    cfg = _make_config(ssh_key_path="/no/such/key")
    result = validate([cfg], check_gpu=False)
    assert not result.ok
    assert any("SSH key not found" in e for e in result.errors)


def test_loose_ssh_permissions():
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o644)
        cfg = _make_config(ssh_key_path=f.name)
        result = validate([cfg], check_gpu=False)
        assert result.ok  # warnings, not errors
        assert any("loose permissions" in w for w in result.warnings)


def test_missing_local_dir():
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o600)
        cfg = _make_config(ssh_key_path=f.name, local_project_dir="/no/such/dir")
        result = validate([cfg], check_gpu=False)
        assert not result.ok
        assert any("local_project_dir" in e for e in result.errors)


def test_unknown_dependency():
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o600)
        cfg = _make_config(ssh_key_path=f.name, name="a", depends_on=["nonexistent"])
        result = validate([cfg], check_gpu=False)
        assert not result.ok
        assert any("unknown job" in e for e in result.errors)


def test_dependency_cycle():
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o600)
        a = _make_config(ssh_key_path=f.name, name="a", depends_on=["b"])
        b = _make_config(ssh_key_path=f.name, name="b", depends_on=["a"])
        result = validate([a, b], check_gpu=False)
        assert not result.ok
        assert any("cycle" in e.lower() for e in result.errors)


def test_duplicate_name():
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o600)
        a = _make_config(ssh_key_path=f.name, name="a")
        b = _make_config(ssh_key_path=f.name, name="a", run_command="echo b")
        result = validate([a, b], check_gpu=False)
        assert not result.ok
        assert any("Duplicate" in e for e in result.errors)


def test_negative_budget():
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o600)
        cfg = _make_config(ssh_key_path=f.name, budget_usd=-10)
        result = validate([cfg], check_gpu=False)
        assert not result.ok


def test_no_configs():
    result = validate([], check_gpu=False)
    assert not result.ok
