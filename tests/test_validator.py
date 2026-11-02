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


def _fake_ssh_tools(keygen_rc=0, keygen_err="", agent_rc=0, agent_out="", list_rc=0,
                    list_out="3072 SHA256:abcdef comment (RSA)"):
    import subprocess

    def run(cmd, **kwargs):
        if cmd[0] == "ssh-keygen" and "-y" in cmd:
            assert cmd[cmd.index("-P") + 1] == ""   # must never prompt
            return subprocess.CompletedProcess(cmd, keygen_rc, stdout="ssh-rsa AAAA\n", stderr=keygen_err)
        if cmd[0] == "ssh-keygen" and "-l" in cmd:
            return subprocess.CompletedProcess(cmd, list_rc, stdout=list_out, stderr="")
        if cmd[0] == "ssh-add":
            return subprocess.CompletedProcess(cmd, agent_rc, stdout=agent_out, stderr="")
        raise AssertionError(f"unexpected command {cmd}")
    return run


def _validate_key(monkeypatch, run):
    monkeypatch.setattr("conductor.validator.subprocess.run", run)
    with tempfile.NamedTemporaryFile(suffix="_key") as f:
        os.chmod(f.name, 0o600)
        return validate([_make_config(ssh_key_path=f.name)], check_gpu=False), f.name


def test_unencrypted_key_is_usable(monkeypatch):
    result, _ = _validate_key(monkeypatch, _fake_ssh_tools(keygen_rc=0))
    assert result.ok


def test_passphrase_key_not_in_agent_is_an_error(monkeypatch):
    run = _fake_ssh_tools(keygen_rc=255,
                          keygen_err='Load key "/k": incorrect passphrase supplied to decrypt private key',
                          agent_rc=1, agent_out="The agent has no identities.")
    result, key = _validate_key(monkeypatch, run)
    assert not result.ok
    assert result.errors == [
        "SSH key is passphrase-protected and not loaded in ssh-agent: "
        f"run `ssh-add --apple-use-keychain {key}` or use an unencrypted key"]


def test_passphrase_key_loaded_in_agent_is_usable(monkeypatch):
    run = _fake_ssh_tools(keygen_rc=255, keygen_err="incorrect passphrase",
                          agent_rc=0, agent_out="3072 SHA256:abcdef comment (RSA)\n")
    result, _ = _validate_key(monkeypatch, run)
    assert result.ok


def test_agent_holding_a_different_key_does_not_count(monkeypatch):
    run = _fake_ssh_tools(keygen_rc=255, keygen_err="incorrect passphrase",
                          agent_rc=0, agent_out="256 SHA256:other x (ED25519)\n")
    result, _ = _validate_key(monkeypatch, run)
    assert not result.ok and "passphrase-protected" in result.errors[0]


def test_corrupt_key_is_reported_as_unusable(monkeypatch):
    run = _fake_ssh_tools(keygen_rc=255, keygen_err='Load key "/k": invalid format',
                          list_rc=255, list_out="", agent_rc=1)
    result, _ = _validate_key(monkeypatch, run)
    assert not result.ok and "not a usable private key" in result.errors[0]


def test_missing_ssh_keygen_skips_the_check(monkeypatch):
    def run(cmd, **kwargs):
        raise FileNotFoundError("ssh-keygen")
    result, _ = _validate_key(monkeypatch, run)
    assert result.ok
