from __future__ import annotations

import logging

from conductor.config import JobConfig
from conductor.ssh import ssh_exec
from conductor.state import PodState

log = logging.getLogger(__name__)


def launch(config: JobConfig, pod_state: PodState) -> int | None:
    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = config.ssh_key_path
    remote_dir = config.remote_project_dir
    log_file = f"{remote_dir}/conductor_job.log"

    cmd = (
        f"cd {remote_dir} && "
        f"nohup bash -c '{config.run_command}' > {log_file} 2>&1 </dev/null & echo $!"
    )

    log.info(f"[{config.name}] Launching job")
    result = ssh_exec(host, port, key, cmd)
    if result.returncode != 0:
        log.error(f"[{config.name}] Launch failed: {result.stderr}")
        return None

    pid_str = result.stdout.strip()
    try:
        pid = int(pid_str)
        log.info(f"[{config.name}] Job launched with PID {pid}")
        return pid
    except ValueError:
        log.error(f"[{config.name}] Could not parse PID from: {pid_str!r}")
        return None


def is_alive(pod_state: PodState, key_path: str) -> bool | None:
    """Check if job process is still running. Returns True/False or None if unreachable."""
    if not pod_state.pid or not pod_state.ssh_host:
        return False

    try:
        result = ssh_exec(
            pod_state.ssh_host, pod_state.ssh_port, key_path,
            f"kill -0 {pod_state.pid} 2>/dev/null && echo alive || echo dead",
            timeout=15,
        )
    except Exception:
        return None  # SSH unreachable

    if result.returncode != 0:
        return None

    return "alive" in result.stdout


def get_log_path(config: JobConfig) -> str:
    return f"{config.remote_project_dir}/conductor_job.log"
