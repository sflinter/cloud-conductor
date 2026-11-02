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
def test_deploy_image_mode_skips_rsync(mock_ssh, mock_rsync):
    config = _make_config(deploy_method="image", setup_command="")
    pod = _make_pod()
    assert deploy(config, pod) is True
    mock_ssh.assert_not_called()
    mock_rsync.assert_not_called()


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_deploy_image_mode_runs_setup(mock_ssh, mock_rsync):
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
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
    mock_ssh.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    config = _make_config(setup_timeout_seconds=4321, deploy_timeout_seconds=99)
    assert deploy(config, _make_pod()) is True
    setup_call = [c for c in mock_ssh.call_args_list if "pip install" in str(c)][0]
    assert setup_call[1]["timeout"] == 4321
    assert mock_rsync.call_args[1]["timeout"] == 99


@patch("conductor.deployer.rsync")
@patch("conductor.deployer.ssh_exec")
def test_setup_timeout_fails_job_not_process(mock_ssh, mock_rsync):
    # ssh_exec reports a timeout as a failed result; deploy must return False
    mock_ssh.side_effect = [
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),   # install rsync
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),   # mkdir
        subprocess.CompletedProcess([], 124, stdout="", stderr="timed out after 1800s"),
    ]
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    assert deploy(_make_config(), _make_pod()) is False


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
