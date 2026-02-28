from __future__ import annotations

import os
import subprocess
import time

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]


def _ssh_base(host: str, port: int, key_path: str) -> list[str]:
    return ["ssh", *SSH_OPTS, "-i", key_path, "-p", str(port), f"root@{host}"]


def ssh_exec(host: str, port: int, key_path: str, command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [*_ssh_base(host, port, key_path), command]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def rsync(
    src: str,
    dst: str,
    host: str,
    port: int,
    key_path: str,
    excludes: list[str] | None = None,
    delete: bool = False,
) -> subprocess.CompletedProcess:
    ssh_cmd = f"ssh {' '.join(SSH_OPTS)} -i {key_path} -p {port}"
    cmd = ["rsync", "-az", "-e", ssh_cmd]
    if delete:
        cmd.append("--delete")
    for exc in (excludes or []):
        cmd.extend(["--exclude", exc])
    cmd.extend([src, f"root@{host}:{dst}"])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def rsync_pull(
    remote_path: str,
    local_path: str,
    host: str,
    port: int,
    key_path: str,
) -> subprocess.CompletedProcess:
    ssh_cmd = f"ssh {' '.join(SSH_OPTS)} -i {key_path} -p {port}"
    cmd = ["rsync", "-az", "-e", ssh_cmd, f"root@{host}:{remote_path}", local_path]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def wait_ssh(host: str, port: int, key_path: str, timeout: int = 300) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = ssh_exec(host, port, key_path, "echo ok", timeout=10)
            if result.returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(5)
    return False


def ssh_interactive(host: str, port: int, key_path: str) -> None:
    cmd = [*_ssh_base(host, port, key_path)]
    os.execvp("ssh", cmd)


def tail_remote_log(host: str, port: int, key_path: str, remote_log: str) -> None:
    cmd = [*_ssh_base(host, port, key_path), f"tail -f {remote_log}"]
    os.execvp("ssh", cmd)
