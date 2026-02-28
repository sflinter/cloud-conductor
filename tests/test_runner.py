from unittest.mock import patch
import subprocess

from conductor.config import JobConfig
from conductor.runner import launch, is_alive
from conductor.state import PodState


def _make_config(**overrides):
    defaults = dict(name="test", run_command="python train.py", ssh_key_path="/key",
                    remote_project_dir="/workspace/proj")
    defaults.update(overrides)
    return JobConfig(**defaults)


@patch("conductor.runner.ssh_exec")
def test_launch_success(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="12345\n", stderr="")
    config = _make_config()
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)
    pid = launch(config, pod)
    assert pid == 12345
    # Verify nohup command format
    cmd = mock_ssh.call_args[0][3]
    assert "nohup" in cmd
    assert "bash -c" in cmd
    assert "</dev/null &" in cmd
    assert "echo $!" in cmd


@patch("conductor.runner.ssh_exec")
def test_launch_failure(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="error")
    config = _make_config()
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)
    assert launch(config, pod) is None


@patch("conductor.runner.ssh_exec")
def test_is_alive_true(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="alive\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345)
    assert is_alive(pod, "/key") is True


@patch("conductor.runner.ssh_exec")
def test_is_alive_false(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="dead\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345)
    assert is_alive(pod, "/key") is False


@patch("conductor.runner.ssh_exec")
def test_is_alive_unreachable(mock_ssh):
    mock_ssh.side_effect = Exception("connection refused")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345)
    assert is_alive(pod, "/key") is None


def test_is_alive_no_pid():
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=None)
    assert is_alive(pod, "/key") is False
