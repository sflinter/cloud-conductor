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


@patch("conductor.syncer.ssh_exec")
@patch("conductor.syncer.rsync")
@patch("conductor.syncer.os.path.exists", return_value=True)
def test_sync_push(mock_exists, mock_rsync, mock_ssh):
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


_CHANGE_DIR = ('rsync: [sender] change_dir "/workspace/proj/output" failed: No such file or directory (2)\n'
               'rsync(40813): warning: receiver has empty file list: exiting\n')
_LINK_STAT = ('rsync: [sender] link_stat "/workspace/proj/train.log" failed: No such file or directory (2)\n'
              'rsync error: some files/attrs were not transferred (see previous errors) (code 23)\n')


def test_remote_source_missing_classification():
    from conductor.syncer import remote_source_missing
    missing_dir = subprocess.CompletedProcess([], 23, stderr=_CHANGE_DIR)
    assert remote_source_missing(missing_dir, "/workspace/proj/output/") is True
    missing_file = subprocess.CompletedProcess([], 23, stderr=_LINK_STAT)
    assert remote_source_missing(missing_file, "/workspace/proj/train.log") is True
    # A missing parent directory means the source is missing too
    parent = subprocess.CompletedProcess(
        [], 23, stderr='rsync: change_dir "/workspace/proj" failed: No such file or directory (2)\n')
    assert remote_source_missing(parent, "/workspace/proj/output/") is True
    # A nested file that vanished is a partial transfer, not a missing source
    nested = subprocess.CompletedProcess(
        [], 23, stderr='rsync: link_stat "/workspace/proj/output/ckpt.pt" failed: No such file or directory (2)\n')
    assert remote_source_missing(nested, "/workspace/proj/output/") is False
    # Same message with another exit code, or another message with code 23, is a failure
    assert remote_source_missing(subprocess.CompletedProcess([], 12, stderr=_CHANGE_DIR), "/workspace/proj/output/") is False
    assert remote_source_missing(subprocess.CompletedProcess([], 23, stderr="rsync: write failed"), "/workspace/proj/output/") is False


@patch("conductor.syncer.rsync_pull")
@patch("conductor.syncer.os.makedirs")
def test_sync_pull_missing_remote_is_nothing_to_sync(mock_mkdir, mock_rsync, caplog):
    import logging
    mock_rsync.return_value = subprocess.CompletedProcess([], 23, stderr=_CHANGE_DIR)
    config = _make_config(sync_paths=[SyncPath(remote="output/", local="./results/")])
    with caplog.at_level(logging.INFO, logger="conductor.syncer"):
        assert sync_pull(config, _make_pod()) is True
    assert any(r.levelno == logging.INFO and "Nothing to sync" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


@patch("conductor.syncer.rsync_pull")
@patch("conductor.syncer.os.makedirs")
def test_sync_pull_transfer_failure_is_fatal_unless_optional(mock_mkdir, mock_rsync):
    mock_rsync.return_value = subprocess.CompletedProcess([], 12, stderr="rsync: connection unexpectedly closed")
    required = _make_config(sync_paths=[SyncPath(remote="output/", local="./results/")])
    assert sync_pull(required, _make_pod()) is False
    optional = _make_config(sync_paths=[SyncPath(remote="output/", local="./results/", optional=True)])
    assert sync_pull(optional, _make_pod()) is True


@patch("conductor.syncer.rsync_pull")
@patch("conductor.syncer.os.makedirs")
def test_sync_pull_one_failure_fails_the_sync(mock_mkdir, mock_rsync):
    mock_rsync.side_effect = [
        subprocess.CompletedProcess([], 23, stderr=_CHANGE_DIR),     # missing: fine
        subprocess.CompletedProcess([], 30, stderr="timeout"),       # real failure
        subprocess.CompletedProcess([], 0),
    ]
    config = _make_config(sync_paths=[
        SyncPath(remote="output/", local="./results/"),
        SyncPath(remote="ckpt/", local="./ckpt/"),
        SyncPath(remote="train.log", local="./logs/"),
    ])
    assert sync_pull(config, _make_pod()) is False
    assert mock_rsync.call_count == 3


@patch("conductor.syncer.ssh_exec")
@patch("conductor.syncer.rsync")
def test_sync_push_file_and_directory_entries(mock_rsync, mock_ssh, tmp_dir):
    """A file entry was pulled into the local directory under its own name:
    push that file to the remote file path, not the whole local directory
    ("destination must be a directory when copying more than 1 file")."""
    import os
    mock_rsync.return_value = subprocess.CompletedProcess([], 0)
    mock_ssh.return_value = subprocess.CompletedProcess([], 0)
    logs = os.path.join(tmp_dir, "logs") + "/"
    results = os.path.join(tmp_dir, "results") + "/"
    os.makedirs(logs)
    os.makedirs(results)
    for f in ("train.log", "other.txt"):
        open(os.path.join(logs, f), "w").close()
    open(os.path.join(results, "ckpt.pt"), "w").close()
    config = _make_config(sync_paths=[
        SyncPath(remote="out/train.log", local=logs),
        SyncPath(remote="output/", local=results),
    ])
    assert sync_push(config, _make_pod()) is True

    (f_src, f_dst), (d_src, d_dst) = [c[0][:2] for c in mock_rsync.call_args_list]
    assert f_src == os.path.join(logs, "train.log")
    assert f_dst == "/workspace/proj/out/"          # lands as /workspace/proj/out/train.log
    assert d_src == results and d_dst == "/workspace/proj/output/"
    mkdirs = [c[0][3] for c in mock_ssh.call_args_list]
    assert mkdirs == ["mkdir -p /workspace/proj/out", "mkdir -p /workspace/proj/output"]


@patch("conductor.syncer.ssh_exec")
@patch("conductor.syncer.rsync")
def test_sync_push_skips_entries_never_synced(mock_rsync, mock_ssh, tmp_dir):
    import os
    config = _make_config(sync_paths=[
        SyncPath(remote="out/train.log", local=os.path.join(tmp_dir, "logs") + "/"),
        SyncPath(remote="output/", local=os.path.join(tmp_dir, "missing") + "/"),
    ])
    assert sync_push(config, _make_pod()) is True
    mock_rsync.assert_not_called()
    mock_ssh.assert_not_called()


def test_push_spec_is_inverse_of_pull():
    from conductor.syncer import push_spec
    assert push_spec("/w", SyncPath(remote="a/b/", local="./x/")) == ("./x/", "/w/a/b/", "/w/a/b")
    assert push_spec("/w", SyncPath(remote="a/f.log", local="./x/")) == ("./x/f.log", "/w/a/", "/w/a")
    assert push_spec("/w", SyncPath(remote="models", local="./x")) == ("./x/models", "/w/", "/w")
