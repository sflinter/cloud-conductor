# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from conductor.ssh import ssh_exec, _ssh_base
from conductor.state import PodState

log = logging.getLogger(__name__)

LOG_FILE = "conductor_job.log"
PID_FILE = ".conductor_pid"
START_FILE = ".conductor_pid_start"
EXIT_FILE = ".conductor_exit"
RUN_SCRIPT = ".conductor_run.sh"
LAUNCH_SCRIPT = ".conductor_launch.sh"
LOG_TAIL_LINES = 50


@dataclass
class LaunchResult:
    pid: int
    pid_start: str | None = None  # process start time, to detect pid reuse


@dataclass
class ProcessStatus:
    """Tri-state result of a remote process check.

    state is "running", "exited" (exit_code is None when the process is gone
    but left no exit file) or "unknown" (SSH failed; nothing can be concluded)."""
    state: str
    exit_code: int | None = None
    detail: str = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def exited(self) -> bool:
        return self.state == "exited"

    @property
    def unknown(self) -> bool:
        return self.state == "unknown"


def _start_time_cmd(pid: str) -> str:
    # Field 22 of /proc/<pid>/stat is the start time in clock ticks since boot.
    # Strip everything up to the closing paren of the comm field first, so a
    # command name containing spaces cannot shift the fields. Fall back to
    # ps lstart (spaces replaced) on systems without /proc.
    return (
        f"s=$(sed 's/.*) //' /proc/{pid}/stat 2>/dev/null | awk '{{print $20}}'); "
        f"[ -z \"$s\" ] && s=$(ps -o lstart= -p {pid} 2>/dev/null | tr -s ' ' | tr ' ' '_'); "
        f"echo $s"
    )


def _paths(remote_dir: str) -> dict[str, str]:
    d = remote_dir.rstrip("/")
    return {
        "log": f"{d}/{LOG_FILE}", "pid": f"{d}/{PID_FILE}", "start": f"{d}/{START_FILE}",
        "exit": f"{d}/{EXIT_FILE}", "run": f"{d}/{RUN_SCRIPT}", "launch": f"{d}/{LAUNCH_SCRIPT}",
    }


def launch_direct(
    ssh_host: str,
    ssh_port: int,
    ssh_key_path: str,
    command: str,
    remote_dir: str = "/workspace/project",
    name: str = "pod",
) -> LaunchResult | None:
    p = _paths(remote_dir)

    # The user's command lives in its own script so arbitrary quoting is safe.
    # The exit code is written to the exit file the moment the command ends.
    run_script = (
        "#!/bin/bash\n"
        f"cd {remote_dir} && ( {command} )\n"
        f"echo $? > {p['exit']}\n"
    )
    # setsid + nohup detach the job from the SSH session; the pid and its
    # start time are recorded so a later check can tell a reused pid apart.
    # If a previous launch is still running, report it instead of starting twice.
    launch_script = (
        "#!/bin/bash\n"
        f"cd {remote_dir} || exit 1\n"
        f"if [ -f {p['pid']} ] && [ ! -f {p['exit']} ] && kill -0 \"$(cat {p['pid']})\" 2>/dev/null; then\n"
        f"  echo \"$(cat {p['pid']}) $(cat {p['start']} 2>/dev/null)\"\n"
        "  exit 0\n"
        "fi\n"
        f"rm -f {p['exit']}\n"
        f"setsid nohup bash {p['run']} > {p['log']} 2>&1 < /dev/null &\n"
        "pid=$!\n"
        f"echo $pid > {p['pid']}\n"
        f"start=$({_start_time_cmd('$pid')})\n"
        f"echo \"$start\" > {p['start']}\n"
        "echo \"$pid $start\"\n"
    )
    write_cmd = (
        f"mkdir -p {remote_dir} && "
        f"cat > {p['run']} << 'CONDUCTOR_EOF'\n{run_script}CONDUCTOR_EOF\n"
        f"cat > {p['launch']} << 'CONDUCTOR_EOF'\n{launch_script}CONDUCTOR_EOF"
    )
    write_result = ssh_exec(ssh_host, ssh_port, ssh_key_path, write_cmd)
    if write_result.returncode != 0:
        log.error(f"[{name}] Failed to write launch script: {write_result.stderr}")
        return None

    log.info(f"[{name}] Launching job")
    result = ssh_exec(ssh_host, ssh_port, ssh_key_path, f"bash {p['launch']}", timeout=30)
    if result.returncode != 0:
        log.error(f"[{name}] Launch failed: {result.stderr}")
        return None

    parts = result.stdout.split()
    try:
        pid = int(parts[0])
    except (IndexError, ValueError):
        log.error(f"[{name}] Could not parse PID from: {result.stdout!r}")
        return None
    pid_start = parts[1] if len(parts) > 1 else None
    log.info(f"[{name}] Job launched with PID {pid}" + (f" (start {pid_start})" if pid_start else ""))
    return LaunchResult(pid=pid, pid_start=pid_start)


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


def launch(config, pod_state: PodState) -> LaunchResult | None:
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


def is_alive(pod_state: PodState, key_path: str, remote_dir: str | None = None) -> ProcessStatus:
    """Check the job process. Reads the exit file first (immediate, with a
    real exit code), then falls back to the pid. SSH failure is "unknown"."""
    if not pod_state.ssh_host:
        return ProcessStatus("unknown", detail="no ssh host")
    if not pod_state.pid and not remote_dir:
        return ProcessStatus("unknown", detail="no pid recorded")

    exit_check = ""
    if remote_dir:
        exit_file = _paths(remote_dir)["exit"]
        exit_check = f"if [ -f {exit_file} ]; then echo \"exited $(cat {exit_file})\"; el"

    if pod_state.pid:
        pid = pod_state.pid
        pid_check = (
            f"if kill -0 {pid} 2>/dev/null; then echo \"running $({_start_time_cmd(str(pid))})\"; "
            f"else echo gone; fi"
        )
    else:
        pid_check = "if true; then echo nopid; fi"

    cmd = exit_check + pid_check

    result = ssh_exec(pod_state.ssh_host, pod_state.ssh_port, key_path, cmd, timeout=15)
    if result.returncode != 0:
        return ProcessStatus("unknown", detail=f"ssh failed: {result.stderr.strip() or result.returncode}")

    return _parse_status(result.stdout, pod_state)


def _parse_status(stdout: str, pod_state: PodState) -> ProcessStatus:
    parts = stdout.split()
    if not parts:
        return ProcessStatus("unknown", detail="empty response")
    word = parts[0]
    if word == "exited":
        try:
            return ProcessStatus("exited", exit_code=int(parts[1]))
        except (IndexError, ValueError):
            return ProcessStatus("exited", detail="exit file unreadable")
    if word == "running":
        start = parts[1] if len(parts) > 1 else None
        if start and pod_state.pid_start and start != pod_state.pid_start:
            return ProcessStatus("exited", detail=f"pid {pod_state.pid} reused by another process")
        return ProcessStatus("running")
    if word == "gone":
        return ProcessStatus("exited", detail="process gone, no exit file")
    if word == "nopid":
        return ProcessStatus("unknown", detail="no pid recorded")
    return ProcessStatus("unknown", detail=f"unexpected response: {stdout.strip()!r}")


def fetch_log_tail(pod_state: PodState, key_path: str, remote_dir: str, lines: int = LOG_TAIL_LINES) -> str:
    if not pod_state.ssh_host:
        return ""
    log_file = _paths(remote_dir)["log"]
    result = ssh_exec(pod_state.ssh_host, pod_state.ssh_port, key_path,
                      f"tail -n {lines} {log_file} 2>/dev/null", timeout=30)
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip()


def get_utilization(pod_state: PodState, key_path: str) -> dict | None:
    if not pod_state.ssh_host or not pod_state.pid:
        return None

    cmd = (
        "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total"
        " --format=csv,noheader,nounits 2>/dev/null;"
        f" echo ---; ps -p {pod_state.pid} -o %cpu= 2>/dev/null"
    )
    result = ssh_exec(pod_state.ssh_host, pod_state.ssh_port, key_path, cmd, timeout=10)
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
    return f"{config.remote_project_dir}/{LOG_FILE}"
