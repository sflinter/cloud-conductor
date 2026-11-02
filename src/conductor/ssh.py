# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger(__name__)

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]

# Return code used when the local subprocess could not run or timed out.
RC_TIMEOUT = 124
RC_LOCAL_ERROR = 255


def _ssh_base(host: str, port: int, key_path: str) -> list[str]:
    return ["ssh", *SSH_OPTS, "-i", key_path, "-p", str(port), f"root@{host}"]


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run a local ssh/rsync command. Never raises: a timeout or OS error is
    returned as a failed CompletedProcess so callers can treat it like any
    other non-zero exit."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        log.warning(f"{cmd[0]} timed out after {timeout}s")
        return subprocess.CompletedProcess(
            cmd, RC_TIMEOUT, stdout=_text(e.stdout), stderr=f"timed out after {timeout}s",
        )
    except OSError as e:
        log.warning(f"{cmd[0]} could not run: {e}")
        return subprocess.CompletedProcess(cmd, RC_LOCAL_ERROR, stdout="", stderr=str(e))


def _text(data) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else str(data)


def ssh_exec(host: str, port: int, key_path: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [*_ssh_base(host, port, key_path), command]
    return _run(cmd, timeout)


def rsync(
    src: str,
    dst: str,
    host: str,
    port: int,
    key_path: str,
    excludes: list[str] | None = None,
    delete: bool = False,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    ssh_cmd = f"ssh {' '.join(SSH_OPTS)} -i {key_path} -p {port}"
    cmd = ["rsync", "-az", "-e", ssh_cmd]
    if delete:
        cmd.append("--delete")
    for exc in (excludes or []):
        cmd.extend(["--exclude", exc])
    cmd.extend([src, f"root@{host}:{dst}"])
    return _run(cmd, timeout)


def rsync_pull(
    remote_path: str,
    local_path: str,
    host: str,
    port: int,
    key_path: str,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    ssh_cmd = f"ssh {' '.join(SSH_OPTS)} -i {key_path} -p {port}"
    cmd = ["rsync", "-az", "-e", ssh_cmd, f"root@{host}:{remote_path}", local_path]
    return _run(cmd, timeout)


def wait_ssh(host: str, port: int, key_path: str, timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = ssh_exec(host, port, key_path, "echo ok", timeout=10)
        if result.returncode == 0:
            return True
        time.sleep(5)
    return False


def ssh_interactive(host: str, port: int, key_path: str) -> None:
    cmd = [*_ssh_base(host, port, key_path)]
    os.execvp("ssh", cmd)


def tail_remote_log(host: str, port: int, key_path: str, remote_log: str) -> None:
    cmd = [*_ssh_base(host, port, key_path), f"tail -f {remote_log}"]
    os.execvp("ssh", cmd)


def tail_remote_log_subprocess(host: str, port: int, key_path: str, remote_log: str) -> int:
    """Like tail_remote_log but returns on disconnect instead of replacing the process."""
    cmd = [*_ssh_base(host, port, key_path), f"tail -f {remote_log}"]
    result = subprocess.run(cmd)
    return result.returncode
