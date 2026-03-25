# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os

from conductor.ssh import ssh_exec, _ssh_base
from conductor.state import PodState

log = logging.getLogger(__name__)


def launch_direct(
    ssh_host: str,
    ssh_port: int,
    ssh_key_path: str,
    command: str,
    remote_dir: str = "/workspace/project",
    name: str = "pod",
) -> int | None:
    log_file = f"{remote_dir}/conductor_job.log"
    pid_file = f"{remote_dir}/.conductor_pid"
    script_file = f"{remote_dir}/.conductor_launch.sh"

    script = (
        f"#!/bin/bash\n"
        f"cd {remote_dir}\n"
        f"( {command} ) > {log_file} 2>&1 < /dev/null &\n"
        f"echo $! > {pid_file}\n"
    )
    write_result = ssh_exec(ssh_host, ssh_port, ssh_key_path,
                            f"cat > {script_file} << 'CONDUCTOR_EOF'\n{script}CONDUCTOR_EOF")
    if write_result.returncode != 0:
        log.error(f"[{name}] Failed to write launch script: {write_result.stderr}")
        return None

    log.info(f"[{name}] Launching job")
    result = ssh_exec(ssh_host, ssh_port, ssh_key_path,
                      f"bash {script_file} && cat {pid_file}", timeout=30)
    if result.returncode != 0:
        log.error(f"[{name}] Launch failed: {result.stderr}")
        return None

    pid_str = result.stdout.strip()
    try:
        pid = int(pid_str)
        log.info(f"[{name}] Job launched with PID {pid}")
        return pid
    except ValueError:
        log.error(f"[{name}] Could not parse PID from: {pid_str!r}")
        return None


def exec_foreground(
    ssh_host: str,
    ssh_port: int,
    ssh_key_path: str,
    command: str,
    remote_dir: str = "/workspace/project",
) -> None:
    cmd = [*_ssh_base(ssh_host, ssh_port, ssh_key_path),
           f"cd {remote_dir} && {command}"]
    os.execvp("ssh", cmd)


def launch(config, pod_state: PodState) -> int | None:
    from conductor.config import JobConfig
    cfg: JobConfig = config
    return launch_direct(
        ssh_host=pod_state.ssh_host,
        ssh_port=pod_state.ssh_port,
        ssh_key_path=cfg.ssh_key_path,
        command=cfg.run_command,
        remote_dir=cfg.remote_project_dir,
        name=cfg.name,
    )


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


def get_log_path(config) -> str:
    return f"{config.remote_project_dir}/conductor_job.log"
