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


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_full_sequence(mock_ssh, mock_rsync):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    config = _make_config()
    pod = _make_pod()

    assert deploy(config, pod) is True
    # Should call: install rsync, mkdir, setup command (3 ssh calls)
    assert mock_ssh.call_count == 3
    # rsync called once
    assert mock_rsync.call_count == 1
    rsync_call = mock_rsync.call_args
    assert rsync_call[1]["delete"] is True


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_image_mode_skips(mock_ssh, mock_rsync):
    config = _make_config(deploy_method="image")
    pod = _make_pod()
    assert deploy(config, pod) is True
    mock_ssh.assert_not_called()
    mock_rsync.assert_not_called()


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
