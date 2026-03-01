# Copyright (c) 2026 Steve Flinter. MIT License.
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
    pid_file = f"{remote_dir}/.conductor_pid"
    script_file = f"{remote_dir}/.conductor_launch.sh"

    # Write a launch script that fully detaches the job from the SSH session.
    # The subshell + FD redirection ensures no inherited SSH channel FDs.
    script = (
        f"#!/bin/bash\n"
        f"cd {remote_dir}\n"
        f"( {config.run_command} ) > {log_file} 2>&1 < /dev/null &\n"
        f"echo $! > {pid_file}\n"
    )
    write_result = ssh_exec(host, port, key,
                            f"cat > {script_file} << 'CONDUCTOR_EOF'\n{script}CONDUCTOR_EOF")
    if write_result.returncode != 0:
        log.error(f"[{config.name}] Failed to write launch script: {write_result.stderr}")
        return None

    log.info(f"[{config.name}] Launching job")
    result = ssh_exec(host, port, key,
                      f"bash {script_file} && cat {pid_file}", timeout=30)
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


def get_utilization(pod_state: PodState, key_path: str) -> dict | None:
    if not pod_state.ssh_host or not pod_state.pid:
        return None

    cmd = (
        "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total"
        " --format=csv,noheader,nounits 2>/dev/null;"
        f" echo ---; ps -p {pod_state.pid} -o %cpu= 2>/dev/null"
    )
    try:
        result = ssh_exec(pod_state.ssh_host, pod_state.ssh_port, key_path, cmd, timeout=10)
    except Exception:
        return None

    if result.returncode != 0:
        return None

    try:
        parts = result.stdout.split("---")
        gpu_line = parts[0].strip()
        cpu_line = parts[1].strip() if len(parts) > 1 else ""

        metrics = {}
        if gpu_line:
            vals = [v.strip() for v in gpu_line.split(",")]
            if len(vals) >= 3:
                metrics["gpu_util"] = int(vals[0])
                metrics["gpu_mem_used"] = int(vals[1])
                metrics["gpu_mem_total"] = int(vals[2])

        if cpu_line:
            metrics["cpu_util"] = float(cpu_line)

        return metrics if metrics else None
    except (ValueError, IndexError):
        return None


def get_log_path(config: JobConfig) -> str:
    return f"{config.remote_project_dir}/conductor_job.log"
