from unittest.mock import patch, call
import subprocess

from conductor.config import JobConfig
from conductor.deployer import deploy
from conductor.state import PodState


def _make_config(**overrides):
    defaults = dict(
        name="test", run_command="echo hi", ssh_key_path="/key",
        local_project_dir="/src", remote_project_dir="/workspace/proj",
        rsync_excludes=[".git/"], setup_command="pip install .",
    )
    defaults.update(overrides)
    return JobConfig(**defaults)


def _make_pod():
    return PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)


def _fake_ssh(setup_states=("exited 0",), calls=None):
    """ssh_exec stand-in: setup status polls answer from setup_states in
    turn (the last one repeats), everything else succeeds."""
    states = list(setup_states)

    def _ssh(host, port, key, cmd, timeout=300, retries=3):
        if calls is not None:
            calls.append(cmd)
        if "echo running" in cmd:
            out = states.pop(0) if len(states) > 1 else states[0]
            return subprocess.CompletedProcess([], 0, stdout=out + "\n", stderr="")
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")
    return _ssh


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_full_sequence(mock_ssh, mock_rsync):
    mock_ssh.side_effect = _fake_ssh()
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    config = _make_config()
    pod = _make_pod()

    assert deploy(config, pod) is True
    # install rsync, mkdir, start detached setup, poll its exit file
    assert mock_ssh.call_count == 4
    # rsync called once
    assert mock_rsync.call_count == 1
    rsync_call = mock_rsync.call_args
    assert rsync_call[1]["delete"] is True


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_image_mode_skips_rsync(mock_ssh, mock_rsync):
    config = _make_config(deploy_method="image", setup_command="")
    pod = _make_pod()
    assert deploy(config, pod) is True
    mock_ssh.assert_not_called()
    mock_rsync.assert_not_called()


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_image_mode_runs_setup(mock_ssh, mock_rsync):
    mock_ssh.side_effect = _fake_ssh()
    config = _make_config(deploy_method="image")
    pod = _make_pod()
    assert deploy(config, pod) is True
    setup_calls = [c for c in mock_ssh.call_args_list if "pip install" in str(c)]
    assert len(setup_calls) == 1


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_image_mode_upload_paths(mock_ssh, mock_rsync):
    from conductor.config import SyncPath
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    config = _make_config(
        deploy_method="image", setup_command="",
        upload_paths=[SyncPath(local="/local/seed/", remote="models/seed/")],
    )
    pod = _make_pod()
    assert deploy(config, pod) is True
    # rsync called once for the upload path (+ install rsync ssh call)
    assert mock_rsync.call_count == 1


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_reuse_skips_setup(mock_ssh, mock_rsync):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    config = _make_config()
    pod = _make_pod()

    assert deploy(config, pod, is_reuse=True) is True
    # Should call: install rsync, mkdir (2 ssh calls) — no setup command
    assert mock_ssh.call_count == 2
    # Setup command should NOT have been called
    setup_calls = [c for c in mock_ssh.call_args_list if "pip install" in str(c)]
    assert len(setup_calls) == 0


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_rsync_failure(mock_ssh, mock_rsync):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    mock_rsync.return_value = subprocess.CompletedProcess([], 1, stderr="rsync error")
    config = _make_config()
    pod = _make_pod()
    assert deploy(config, pod) is False


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_setup_timeout_is_configurable(mock_ssh, mock_rsync):
    calls = []
    mock_ssh.side_effect = _fake_ssh(setup_states=("running",), calls=calls)
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    now = {"t": 1000.0}

    def fake_time():
        now["t"] += 1000.0
        return now["t"]
    config = _make_config(setup_timeout_seconds=4321, deploy_timeout_seconds=99)
    with patch("conductor.deployer.time.time", side_effect=fake_time):
        assert deploy(config, _make_pod()) is False
    polls = [c for c in calls if "echo running" in c]
    assert len(polls) == 5                      # 4321 s at 1000 s per poll
    assert any(c.startswith("kill -- -") for c in calls)   # the timed-out setup is killed
    assert mock_rsync.call_args[1]["timeout"] == 99


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_setup_failure_fails_deploy_with_log_tail(mock_ssh, mock_rsync, caplog):
    calls = []
    base = _fake_ssh(setup_states=("running", "exited 2"), calls=calls)

    def _ssh(host, port, key, cmd, timeout=300, retries=3):
        if cmd.startswith("tail -n"):
            return subprocess.CompletedProcess([], 0, stdout="error: venv exists\n", stderr="")
        return base(host, port, key, cmd, timeout, retries)
    mock_ssh.side_effect = _ssh
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    assert deploy(_make_config(), _make_pod()) is False
    assert "exited with code 2" in caplog.text and "venv exists" in caplog.text


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_setup_runs_detached_and_is_polled(mock_ssh, mock_rsync):
    calls = []
    mock_ssh.side_effect = _fake_ssh(setup_states=("running", "running", "exited 0"), calls=calls)
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    assert deploy(_make_config(), _make_pod()) is True
    start = [c for c in calls if "pip install ." in c][0]
    assert "setsid nohup bash /workspace/proj/.conductor_setup.sh" in start
    assert "> /workspace/proj/conductor_setup.log 2>&1 < /dev/null &" in start
    assert "echo $? > /workspace/proj/.conductor_setup_exit" in start
    assert len([c for c in calls if "echo running" in c]) == 3


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_setup_wait_is_interrupted_by_shutdown(mock_ssh, mock_rsync):
    """Ctrl-C or a budget stop must not wait for a 40-minute setup: the poll
    raises Interrupted and the setup keeps running on the pod."""
    import pytest
    from conductor import shutdown
    from conductor.shutdown import Interrupted
    calls = []
    base = _fake_ssh(setup_states=("running",), calls=calls)

    def _ssh(host, port, key, cmd, timeout=300, retries=3):
        if "echo running" in cmd:
            shutdown.request()
        return base(host, port, key, cmd, timeout, retries)
    mock_ssh.side_effect = _ssh
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    with pytest.raises(Interrupted):
        deploy(_make_config(setup_timeout_seconds=100000), _make_pod())
    assert not any(c.startswith("kill") for c in calls)


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_rsync_protects_conductor_files(mock_ssh, mock_rsync):
    mock_ssh.side_effect = _fake_ssh()
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    deploy(_make_config(setup_command=""), _make_pod())
    excludes = mock_rsync.call_args[1]["excludes"]
    assert ".git/" in excludes and ".conductor_*" in excludes and "conductor_setup.log" in excludes


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_install_rsync_command_is_parenthesised(mock_ssh, mock_rsync):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    deploy(_make_config(setup_command=""), _make_pod())
    install_cmd = mock_ssh.call_args_list[0][0][3]
    assert "which rsync" in install_cmd
    assert "|| (apt-get update" in install_cmd
    assert install_cmd.rstrip().endswith("rsync)")
