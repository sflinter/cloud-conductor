from unittest.mock import patch
import subprocess

from conductor.config import JobConfig
from conductor.runner import launch, is_alive, get_utilization
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
    # Two calls: write script + execute it
    assert mock_ssh.call_count == 2
    run_cmd = mock_ssh.call_args_list[1][0][3]
    assert ".conductor_launch.sh" in run_cmd


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


@patch("conductor.runner.ssh_exec")
def test_get_utilization_success(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess(
        [], 0, stdout="45, 12000, 24576\n---\n 23.5\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=999)
    result = get_utilization(pod, "/key")
    assert result == {"gpu_util": 45, "gpu_mem_used": 12000, "gpu_mem_total": 24576, "cpu_util": 23.5}
    cmd = mock_ssh.call_args[0][3]
    assert "nvidia-smi" in cmd
    assert "ps -p 999" in cmd


@patch("conductor.runner.ssh_exec")
def test_get_utilization_no_gpu(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess(
        [], 0, stdout="\n---\n 99.2\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=999)
    result = get_utilization(pod, "/key")
    assert result == {"cpu_util": 99.2}


@patch("conductor.runner.ssh_exec")
def test_get_utilization_ssh_failure(mock_ssh):
    mock_ssh.side_effect = Exception("timeout")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=999)
    assert get_utilization(pod, "/key") is None


def test_get_utilization_no_ssh_info():
    pod = PodState(name="test", pid=999)
    assert get_utilization(pod, "/key") is None


def test_get_utilization_no_pid():
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)
    assert get_utilization(pod, "/key") is None
