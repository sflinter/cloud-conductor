import os
import tempfile

import pytest


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture(autouse=True)
def no_retry_sleeps(monkeypatch):
    """Retries and terminate verification must never make a test wait."""
    monkeypatch.setattr("conductor.retry._sleep", lambda s: None)
    monkeypatch.setattr("conductor.ssh.RETRY_BASE_SECONDS", 0.0)
    monkeypatch.setattr("conductor.ssh.RETRY_MAX_SECONDS", 0.0)
    monkeypatch.setattr("conductor.provisioner.TERMINATE_POLL_SECONDS", 0.0)
    monkeypatch.setattr("conductor.provisioner.TERMINATE_VERIFY_SECONDS", 0.0)
    monkeypatch.setattr("conductor.ssh.WAIT_SSH_POLL_SECONDS", 0.0)
    monkeypatch.setattr("conductor.providers.runpod.WAIT_POLL_SECONDS", 0.0)
    monkeypatch.setattr("conductor.providers.vastai.WAIT_POLL_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def ssh_key_probe_succeeds(monkeypatch):
    """The validator shells out to ssh-keygen/ssh-add; by default pretend
    every key is unencrypted so tests with placeholder key files pass."""
    import subprocess

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr("conductor.validator.subprocess.run", fake_run)


@pytest.fixture(autouse=True)
def clear_shutdown_flag():
    """The process-wide Ctrl-C flag must never leak between tests."""
    from conductor import shutdown
    shutdown.clear()
    yield
    shutdown.clear()


@pytest.fixture
def sample_toml(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('''
[global]
gpu_type_id = "NVIDIA RTX A2000"
image_name = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
ssh_key_path = "~/.ssh/id_rsa"
remote_project_dir = "/workspace/project"
budget_usd = 40.00

[[jobs]]
name = "train"
run_command = "python train.py"

[[jobs]]
name = "eval"
depends_on = ["train"]
run_command = "python eval.py"
''')
    return path
