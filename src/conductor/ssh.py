# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os
import random
import subprocess
import time

log = logging.getLogger(__name__)

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    # Fail fast on a dead host instead of hanging until the Python timeout
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    # Never block on a password/passphrase prompt
    "-o", "BatchMode=yes",
]

# Keep partially transferred files and give up on a stalled connection
RSYNC_OPTS = ["--partial", "--timeout=120"]

# Return code used when the local subprocess could not run or timed out.
RC_TIMEOUT = 124
RC_LOCAL_ERROR = 255

# ssh exits 255 on a connection failure; rsync codes for socket/protocol/timeouts
SSH_TRANSIENT_RC = {255}
RSYNC_TRANSIENT_RC = {10, 12, 30, 35, 255, RC_TIMEOUT}

DEFAULT_RETRIES = 3
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 15.0


def _ssh_base(host: str, port: int, key_path: str) -> list[str]:
    return ["ssh", *SSH_OPTS, "-i", key_path, "-p", str(port), f"root@{host}"]


def _run_once(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
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


def _run(cmd: list[str], timeout: int, retries: int, transient: set[int]) -> subprocess.CompletedProcess:
    """_run_once with exponential backoff and jitter on transient return codes."""
    attempts = max(1, retries)
    delay = RETRY_BASE_SECONDS
    result = None
    for attempt in range(1, attempts + 1):
        result = _run_once(cmd, timeout)
        if result.returncode == 0 or result.returncode not in transient or attempt == attempts:
            return result
        wait = min(delay, RETRY_MAX_SECONDS) * (0.5 + random.random())
        log.warning(f"{cmd[0]} failed with rc {result.returncode} (attempt {attempt}/{attempts}): "
                    f"{(result.stderr or '').strip()[:200]}; retrying in {wait:.1f}s")
        time.sleep(wait)
        delay *= 2
    return result


def _text(data) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else str(data)


def ssh_exec(
    host: str,
    port: int,
    key_path: str,
    command: str,
    timeout: int = 300,
    retries: int = DEFAULT_RETRIES,
) -> subprocess.CompletedProcess:
    """Run a command over SSH. Only a connection failure (rc 255) is retried;
    a command timeout or non-zero exit is returned as is."""
    cmd = [*_ssh_base(host, port, key_path), command]
    return _run(cmd, timeout, retries, SSH_TRANSIENT_RC)


def _rsync_ssh(key_path: str, port: int) -> str:
    return f"ssh {' '.join(SSH_OPTS)} -i {key_path} -p {port}"


def rsync(
    src: str,
    dst: str,
    host: str,
    port: int,
    key_path: str,
    excludes: list[str] | None = None,
    delete: bool = False,
    timeout: int = 600,
    retries: int = DEFAULT_RETRIES,
) -> subprocess.CompletedProcess:
    cmd = ["rsync", "-az", *RSYNC_OPTS, "-e", _rsync_ssh(key_path, port)]
    if delete:
        cmd.append("--delete")
    for exc in (excludes or []):
        cmd.extend(["--exclude", exc])
    cmd.extend([src, f"root@{host}:{dst}"])
    return _run(cmd, timeout, retries, RSYNC_TRANSIENT_RC)


def rsync_pull(
    remote_path: str,
    local_path: str,
    host: str,
    port: int,
    key_path: str,
    timeout: int = 600,
    retries: int = DEFAULT_RETRIES,
) -> subprocess.CompletedProcess:
    cmd = ["rsync", "-az", *RSYNC_OPTS, "-e", _rsync_ssh(key_path, port),
           f"root@{host}:{remote_path}", local_path]
    return _run(cmd, timeout, retries, RSYNC_TRANSIENT_RC)


def wait_ssh(host: str, port: int, key_path: str, timeout: int = 900) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = ssh_exec(host, port, key_path, "echo ok", timeout=10, retries=1)
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
