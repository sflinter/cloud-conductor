from unittest.mock import patch
import subprocess

from conductor.config import JobConfig
from conductor.runner import launch, is_alive, get_utilization, fetch_log_tail
from conductor.state import PodState


def _make_config(**overrides):
    defaults = dict(name="test", run_command="python train.py", ssh_key_path="/key",
                    remote_project_dir="/workspace/proj")
    defaults.update(overrides)
    return JobConfig(**defaults)


@patch("conductor.runner.ssh_exec")
def test_launch_success(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="12345 987654\n", stderr="")
    config = _make_config()
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)
    result = launch(config, pod)
    assert result.pid == 12345
    assert result.pid_start == "987654"
    # Two calls: write scripts + execute the launcher
    assert mock_ssh.call_count == 2
    write_cmd = mock_ssh.call_args_list[0][0][3]
    run_cmd = mock_ssh.call_args_list[1][0][3]
    assert ".conductor_launch.sh" in run_cmd
    assert "setsid nohup bash /workspace/proj/.conductor_run.sh" in write_cmd
    assert "> /workspace/proj/conductor_job.log 2>&1 < /dev/null &" in write_cmd
    assert "echo $? > /workspace/proj/.conductor_exit" in write_cmd
    assert "rm -f /workspace/proj/.conductor_exit" in write_cmd
    assert "/proc/$pid/stat" in write_cmd
    assert "( python train.py )" in write_cmd


@patch("conductor.runner.ssh_exec")
def test_launch_without_start_time(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="12345\n", stderr="")
    result = launch(_make_config(), PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222))
    assert result.pid == 12345
    assert result.pid_start is None


@patch("conductor.runner.ssh_exec")
def test_launch_script_is_idempotent(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="1 2\n", stderr="")
    launch(_make_config(), PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222))
    write_cmd = mock_ssh.call_args_list[0][0][3]
    # A still-running previous launch is reported, not started twice
    assert 'kill -0 "$(cat /workspace/proj/.conductor_pid)"' in write_cmd


@patch("conductor.runner.ssh_exec")
def test_launch_command_with_quotes_is_written_verbatim(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="1\n", stderr="")
    cmd = """python train.py --name 'it\'s' --cfg "players=3" """
    launch(_make_config(run_command=cmd), PodState(name="test", ssh_host="h", ssh_port=1))
    write_cmd = mock_ssh.call_args_list[0][0][3]
    assert cmd in write_cmd
    assert "<< 'CONDUCTOR_EOF'" in write_cmd


@patch("conductor.runner.ssh_exec")
def test_launch_failure(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="error")
    config = _make_config()
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)
    assert launch(config, pod) is None


@patch("conductor.runner.ssh_exec")
def test_launch_bad_pid(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="garbage\n", stderr="")
    assert launch(_make_config(), PodState(name="test", ssh_host="h", ssh_port=1)) is None


@patch("conductor.runner.ssh_exec")
def test_is_alive_running(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="running 4242\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345, pid_start="4242")
    status = is_alive(pod, "/key", "/workspace/proj")
    assert status.running
    assert status.exit_code is None
    cmd = mock_ssh.call_args[0][3]
    assert "/workspace/proj/.conductor_exit" in cmd
    assert "kill -0 12345" in cmd


@patch("conductor.runner.ssh_exec")
def test_is_alive_exited_zero(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="exited 0\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345)
    status = is_alive(pod, "/key", "/workspace/proj")
    assert status.exited
    assert status.exit_code == 0


@patch("conductor.runner.ssh_exec")
def test_is_alive_exited_nonzero(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="exited 137\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345)
    status = is_alive(pod, "/key", "/workspace/proj")
    assert status.exited
    assert status.exit_code == 137


@patch("conductor.runner.ssh_exec")
def test_is_alive_gone_without_exit_file(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="gone\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345)
    status = is_alive(pod, "/key", "/workspace/proj")
    assert status.exited
    assert status.exit_code is None


@patch("conductor.runner.ssh_exec")
def test_is_alive_detects_pid_reuse(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="running 9999\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345, pid_start="4242")
    status = is_alive(pod, "/key", "/workspace/proj")
    assert status.exited
    assert "reused" in status.detail


@patch("conductor.runner.ssh_exec")
def test_is_alive_unreachable(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 255, stdout="", stderr="connection refused")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=12345)
    status = is_alive(pod, "/key", "/workspace/proj")
    assert status.unknown
    assert "refused" in status.detail


@patch("conductor.runner.ssh_exec")
def test_is_alive_no_pid_checks_exit_file(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="nopid\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=None)
    status = is_alive(pod, "/key", "/workspace/proj")
    assert status.unknown  # not "exited": a pid-less job must not be declared done
    cmd = mock_ssh.call_args[0][3]
    assert "kill -0" not in cmd
    assert ".conductor_exit" in cmd


@patch("conductor.runner.ssh_exec")
def test_is_alive_no_pid_no_dir_does_not_ssh(mock_ssh):
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=None)
    assert is_alive(pod, "/key").unknown
    mock_ssh.assert_not_called()


def test_is_alive_no_ssh_host():
    assert is_alive(PodState(name="test", pid=5), "/key").unknown


@patch("conductor.runner.ssh_exec")
def test_fetch_log_tail(mock_ssh):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="line1\nline2\n", stderr="")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)
    assert fetch_log_tail(pod, "/key", "/workspace/proj", lines=2) == "line1\nline2"
    assert "tail -n 2 /workspace/proj/conductor_job.log" in mock_ssh.call_args[0][3]
    mock_ssh.return_value = subprocess.CompletedProcess([], 255, stdout="", stderr="x")
    assert fetch_log_tail(pod, "/key", "/workspace/proj") == ""


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
    mock_ssh.return_value = subprocess.CompletedProcess([], 124, stdout="", stderr="timed out")
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222, pid=999)
    assert get_utilization(pod, "/key") is None


def test_get_utilization_no_ssh_info():
    pod = PodState(name="test", pid=999)
    assert get_utilization(pod, "/key") is None


def test_get_utilization_no_pid():
    pod = PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)
    assert get_utilization(pod, "/key") is None
