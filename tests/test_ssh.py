from unittest.mock import patch, MagicMock
import subprocess

from conductor.ssh import ssh_exec, rsync, rsync_pull, wait_ssh, SSH_OPTS


def test_ssh_exec_command():
    with patch("conductor.ssh.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
        result = ssh_exec("1.2.3.4", 22222, "/key", "echo ok")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "-i" in cmd
        assert "/key" in cmd
        assert "-p" in cmd
        assert "22222" in cmd
        assert "root@1.2.3.4" in cmd
        assert cmd[-1] == "echo ok"
        assert "-o" in cmd
        assert "StrictHostKeyChecking=no" in cmd


def test_rsync_command():
    with patch("conductor.ssh.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        rsync("/src/", "/dst/", "1.2.3.4", 22222, "/key",
              excludes=[".git/", "__pycache__/"], delete=True)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rsync"
        assert "-az" in cmd
        assert "--delete" in cmd
        assert "--exclude" in cmd
        git_idx = cmd.index("--exclude")
        assert cmd[git_idx + 1] == ".git/"
        assert "root@1.2.3.4:/dst/" in cmd


def test_rsync_pull_command():
    with patch("conductor.ssh.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        rsync_pull("/remote/path", "/local/path", "1.2.3.4", 22222, "/key")
        cmd = mock_run.call_args[0][0]
        assert "root@1.2.3.4:/remote/path" in cmd
        assert "/local/path" in cmd


def test_wait_ssh_success():
    with patch("conductor.ssh.ssh_exec") as mock_exec, \
         patch("conductor.ssh.time") as mock_time:
        mock_time.time.side_effect = [0, 1]  # start, check
        mock_time.sleep = MagicMock()
        mock_exec.return_value = subprocess.CompletedProcess([], 0)
        assert wait_ssh("1.2.3.4", 22222, "/key", timeout=10) is True


def test_wait_ssh_timeout():
    with patch("conductor.ssh.ssh_exec") as mock_exec, \
         patch("conductor.ssh.time") as mock_time:
        mock_time.time.side_effect = [0, 5, 100]
        mock_time.sleep = MagicMock()
        mock_exec.return_value = subprocess.CompletedProcess([], 255)
        assert wait_ssh("1.2.3.4", 22222, "/key", timeout=10) is False


def test_ssh_exec_timeout_returns_failed_result():
    with patch("conductor.ssh.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=5)
        result = ssh_exec("1.2.3.4", 22222, "/key", "sleep 100", timeout=5)
        assert result.returncode != 0
        assert "timed out" in result.stderr


def test_ssh_exec_oserror_returns_failed_result():
    with patch("conductor.ssh.subprocess.run") as mock_run:
        mock_run.side_effect = OSError("ssh: command not found")
        result = ssh_exec("1.2.3.4", 22222, "/key", "echo ok")
        assert result.returncode != 0
        assert "not found" in result.stderr


def test_rsync_timeout_is_passed_through():
    with patch("conductor.ssh.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        rsync("/src/", "/dst/", "1.2.3.4", 22222, "/key", timeout=42)
        assert mock_run.call_args[1]["timeout"] == 42
        rsync_pull("/r", "/l", "1.2.3.4", 22222, "/key", timeout=43)
        assert mock_run.call_args[1]["timeout"] == 43


def test_rsync_pull_timeout_returns_failed_result():
    with patch("conductor.ssh.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["rsync"], timeout=600)
        result = rsync_pull("/r", "/l", "1.2.3.4", 22222, "/key")
        assert result.returncode != 0


def test_wait_ssh_survives_timeout_results():
    with patch("conductor.ssh.ssh_exec") as mock_exec, \
         patch("conductor.ssh.time") as mock_time:
        mock_time.time.side_effect = [0, 1, 2, 100]
        mock_time.sleep = MagicMock()
        mock_exec.side_effect = [
            subprocess.CompletedProcess([], 124, stderr="timed out"),
            subprocess.CompletedProcess([], 0),
        ]
        assert wait_ssh("1.2.3.4", 22222, "/key", timeout=10) is True
