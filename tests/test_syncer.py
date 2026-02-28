from unittest.mock import patch
import subprocess

from conductor.config import JobConfig, SyncPath
from conductor.syncer import sync_pull, sync_push
from conductor.state import PodState


def _make_config(**overrides):
    defaults = dict(name="test", run_command="echo hi", ssh_key_path="/key",
                    remote_project_dir="/workspace/proj")
    defaults.update(overrides)
    return JobConfig(**defaults)


def _make_pod():
    return PodState(name="test", ssh_host="1.2.3.4", ssh_port=22222)


@patch("conductor.syncer.rsync_pull")
@patch("conductor.syncer.os.makedirs")
def test_sync_pull_paths(mock_mkdir, mock_rsync):
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    config = _make_config(sync_paths=[
        SyncPath(remote="output/", local="./results/"),
        SyncPath(remote="train.log", local="./logs/"),
    ])
    pod = _make_pod()
    assert sync_pull(config, pod) is True
    assert mock_rsync.call_count == 2
    # Check remote paths are constructed correctly
    call1 = mock_rsync.call_args_list[0]
    assert call1[0][0] == "/workspace/proj/output/"
    assert call1[0][1] == "./results/"


def test_sync_pull_empty():
    config = _make_config(sync_paths=[])
    pod = _make_pod()
    assert sync_pull(config, pod) is True


@patch("conductor.syncer.rsync")
@patch("conductor.syncer.os.path.exists", return_value=True)
def test_sync_push(mock_exists, mock_rsync):
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    config = _make_config(sync_paths=[
        SyncPath(remote="output/", local="./results/"),
    ])
    pod = _make_pod()
    assert sync_push(config, pod) is True
    call1 = mock_rsync.call_args
    assert call1[0][0] == "./results/"  # local src
    assert call1[0][1] == "/workspace/proj/output/"  # remote dst


def test_sync_push_empty():
    config = _make_config(sync_paths=[])
    pod = _make_pod()
    assert sync_push(config, pod) is True
