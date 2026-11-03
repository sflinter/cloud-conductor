# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os
import time

from conductor import shutdown
from conductor.shutdown import Interrupted
from conductor.ssh import rsync, ssh_exec

log = logging.getLogger(__name__)

# setup_command runs detached on the pod, like the job, and is polled through
# its exit file: a Ctrl-C or a budget stop never waits for a long setup, and
# a setup that outlives the orchestrator can be picked up again on resume.
SETUP_SCRIPT = ".conductor_setup.sh"
SETUP_PID_FILE = ".conductor_setup_pid"
SETUP_EXIT_FILE = ".conductor_setup_exit"
SETUP_LOG = "conductor_setup.log"
SETUP_POLL_SECONDS = 10.0
SETUP_LOG_TAIL_LINES = 30

# The conductor's own files in remote_project_dir: never shipped from the
# local tree and never deleted from the pod by the code rsync's --delete
CONDUCTOR_EXCLUDES = [".conductor_*", "conductor_job.log", SETUP_LOG]

# Only install rsync when it is missing; the subshell keeps "||" from
# swallowing the apt-get chain on images that already have it.
INSTALL_RSYNC_CMD = (
    "which rsync >/dev/null 2>&1 || "
    "(apt-get update -qq && apt-get install -y -qq rsync)"
)


def deploy_direct(
    ssh_host: str,
    ssh_port: int,
    ssh_key_path: str,
    local_dir: str = ".",
    remote_dir: str = "/workspace/project",
    setup_command: str = "",
    excludes: list[str] | None = None,
    setup_timeout: int = 1800,
    rsync_timeout: int = 600,
) -> bool:
    name = "pod"
    if not _install_rsync(name, ssh_host, ssh_port, ssh_key_path):
        return False

    src = local_dir.rstrip("/") + "/"
    dst = remote_dir.rstrip("/") + "/"

    ssh_exec(ssh_host, ssh_port, ssh_key_path, f"mkdir -p {dst}")

    log.info(f"[{name}] Syncing {src} → {dst}")
    result = rsync(src, dst, ssh_host, ssh_port, ssh_key_path,
                   excludes=excludes or [], delete=True, timeout=rsync_timeout)
    if result.returncode != 0:
        log.error(f"[{name}] rsync failed: {result.stderr}")
        return False

    if setup_command:
        log.info(f"[{name}] Running setup command")
        result = ssh_exec(
            ssh_host, ssh_port, ssh_key_path,
            f"cd {remote_dir} && {setup_command}",
            timeout=setup_timeout,
        )
        if result.returncode != 0:
            log.error(f"[{name}] Setup command failed: {result.stderr}")
            return False

    return True


# Deploy progress, recorded in PodState.deploy_steps as each step finishes
STEP_SYNCED = "synced"
STEP_UPLOADED = "uploaded"
STEP_SETUP_STARTED = "setup_started"
STEP_SETUP_DONE = "setup_done"


def deploy(config, pod_state, is_reuse: bool = False, on_step=None) -> bool:
    """Code sync → upload_paths → setup, skipping the steps already in
    pod_state.deploy_steps (a resumed deploy continues from the first
    incomplete step: setup is never run twice on the same pod). on_step()
    is called after each recorded step so the caller can persist it."""
    from conductor.config import JobConfig
    cfg: JobConfig = config

    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = cfg.ssh_key_path
    steps = pod_state.deploy_steps
    if steps:
        log.info(f"[{cfg.name}] Resuming deploy; already done: {', '.join(steps)}")

    def _mark(step: str) -> None:
        if step not in steps:
            steps.append(step)
        if on_step is not None:
            on_step()

    if cfg.deploy_method == "image":
        log.info(f"[{cfg.name}] Image deploy — skipping code rsync")
        if cfg.upload_paths and STEP_UPLOADED not in steps:
            _install_rsync(cfg.name, host, port, key)
    elif STEP_SYNCED not in steps:
        if not _install_rsync(cfg.name, host, port, key):
            return False

        src = cfg.local_project_dir.rstrip("/") + "/"
        dst = cfg.remote_project_dir.rstrip("/") + "/"

        ssh_exec(host, port, key, f"mkdir -p {dst}")

        log.info(f"[{cfg.name}] Syncing {src} → {dst}")
        excludes = [*cfg.rsync_excludes, *(e for e in CONDUCTOR_EXCLUDES if e not in cfg.rsync_excludes)]
        result = rsync(src, dst, host, port, key, excludes=excludes, delete=True,
                       timeout=cfg.deploy_timeout_seconds)
        if result.returncode != 0:
            log.error(f"[{cfg.name}] rsync failed: {result.stderr}")
            return False
        _mark(STEP_SYNCED)

    if STEP_UPLOADED not in steps:
        shutdown.check(f"[{cfg.name}] code sync")
        if not _upload_paths(cfg, host, port, key):
            return False
        _mark(STEP_UPLOADED)

    if not cfg.setup_command or is_reuse:
        return True
    if STEP_SETUP_DONE in steps:
        code = pod_state.setup_exit_code
        log.info(f"[{cfg.name}] Setup already ran on this pod (exit code {code}); skipping")
        return code == 0
    shutdown.check(f"[{cfg.name}] upload")
    code = _run_setup(cfg, host, port, key, started=STEP_SETUP_STARTED in steps,
                      on_started=lambda: _mark(STEP_SETUP_STARTED))
    if code is not None:
        pod_state.setup_exit_code = code
        _mark(STEP_SETUP_DONE)
    return code == 0


def _install_rsync(name: str, host: str, port: int, key: str) -> bool:
    log.info(f"[{name}] Installing rsync on pod")
    result = ssh_exec(host, port, key, INSTALL_RSYNC_CMD)
    if result.returncode != 0:
        log.error(f"[{name}] Failed to install rsync: {result.stderr}")
        return False
    return True


def _upload_paths(config, host: str, port: int, key: str) -> bool:
    """Push each upload_paths entry. A local source that does not exist
    never fails the deploy: a directory path (ending in '/') is created
    empty and uploaded, anything else is skipped with a warning. A file is
    uploaded as itself; a directory's contents go into the remote dir."""
    if not config.upload_paths:
        return True
    remote_base = config.remote_project_dir.rstrip("/")
    for up in config.upload_paths:
        remote = f"{remote_base}/{up.remote}"
        local = up.local
        if not os.path.exists(local):
            if local.endswith("/"):
                log.warning(f"[{config.name}] upload_paths source {local} does not exist; "
                            f"creating it empty")
                try:
                    os.makedirs(local, exist_ok=True)
                except OSError as e:
                    log.warning(f"[{config.name}] Could not create {local} ({e}); skipping it")
                    continue
            else:
                log.warning(f"[{config.name}] upload_paths source {local} does not exist; skipping it")
                continue
        if os.path.isfile(local):
            # remote ending in '/' is the directory to put the file in
            parent = remote.rstrip("/") if remote.endswith("/") else os.path.dirname(remote)
            src, dst = local, remote
        else:
            parent = remote.rstrip("/")
            src, dst = local.rstrip("/") + "/", remote.rstrip("/") + "/"
        ssh_exec(host, port, key, f"mkdir -p {parent}")
        log.info(f"[{config.name}] Uploading {src} → {dst}")
        result = rsync(src, dst, host, port, key, timeout=config.deploy_timeout_seconds)
        if result.returncode != 0:
            log.error(f"[{config.name}] Upload failed for {up.local}: {result.stderr}")
            return False
    return True


def _setup_paths(remote_dir: str) -> dict[str, str]:
    d = remote_dir.rstrip("/")
    return {"script": f"{d}/{SETUP_SCRIPT}", "pid": f"{d}/{SETUP_PID_FILE}",
            "exit": f"{d}/{SETUP_EXIT_FILE}", "log": f"{d}/{SETUP_LOG}"}


def start_setup(config, host: str, port: int, key: str) -> bool:
    """Write the setup script and start it detached (setsid nohup); its
    exit code lands in the setup exit file."""
    d = config.remote_project_dir.rstrip("/")
    p = _setup_paths(d)
    script = (
        "#!/bin/bash\n"
        f"cd {d} && ( {config.setup_command} )\n"
        f"echo $? > {p['exit']}\n"
    )
    cmd = (
        f"mkdir -p {d} && cat > {p['script']} << 'CONDUCTOR_EOF'\n{script}CONDUCTOR_EOF\n"
        f"rm -f {p['exit']} && "
        f"setsid nohup bash {p['script']} > {p['log']} 2>&1 < /dev/null & "
        f"echo $! > {p['pid']}"
    )
    result = ssh_exec(host, port, key, cmd, timeout=60)
    if result.returncode != 0:
        log.error(f"[{config.name}] Could not start setup command: {result.stderr}")
        return False
    return True


def setup_status(config, host: str, port: int, key: str) -> tuple[str, int | None]:
    """("exited", code), ("running", None), ("gone", None) when the setup
    process vanished without an exit file (or never started), or
    ("unknown", None) when SSH failed."""
    p = _setup_paths(config.remote_project_dir)
    cmd = (
        f"if [ -f {p['exit']} ]; then echo \"exited $(cat {p['exit']})\"; "
        f"elif [ -f {p['pid']} ] && kill -0 \"$(cat {p['pid']})\" 2>/dev/null; then echo running; "
        f"else echo gone; fi"
    )
    result = ssh_exec(host, port, key, cmd, timeout=30)
    if result.returncode != 0:
        return "unknown", None
    parts = result.stdout.split()
    if not parts:
        return "unknown", None
    if parts[0] == "exited":
        try:
            return "exited", int(parts[1])
        except (IndexError, ValueError):
            return "exited", 1
    if parts[0] in ("running", "gone"):
        return parts[0], None
    return "unknown", None


def wait_setup(config, host: str, port: int, key: str) -> int | None:
    """Poll the detached setup until it exits (returns its exit code) or
    setup_timeout_seconds pass (kills it, returns None). Raises Interrupted
    between polls when shutdown is requested; the setup keeps running on
    the pod."""
    deadline = time.time() + config.setup_timeout_seconds
    while True:
        state, code = setup_status(config, host, port, key)
        if state == "exited":
            return code
        if state == "gone":
            log.error(f"[{config.name}] Setup process is gone without an exit code")
            return None
        if time.time() >= deadline:
            log.error(f"[{config.name}] Setup command timed out after {config.setup_timeout_seconds}s; "
                      f"killing it")
            p = _setup_paths(config.remote_project_dir)
            ssh_exec(host, port, key, f"kill -- -\"$(cat {p['pid']})\" 2>/dev/null; "
                                      f"kill \"$(cat {p['pid']})\" 2>/dev/null; true", timeout=30)
            return None
        if shutdown.wait(SETUP_POLL_SECONDS):
            raise Interrupted(f"shutdown requested while setup is running on the pod")


def setup_log_tail(config, host: str, port: int, key: str, lines: int = SETUP_LOG_TAIL_LINES) -> str:
    p = _setup_paths(config.remote_project_dir)
    result = ssh_exec(host, port, key, f"tail -n {lines} {p['log']} 2>/dev/null", timeout=30)
    return result.stdout.rstrip() if result.returncode == 0 else ""


def _run_setup(config, host: str, port: int, key: str, started: bool = False,
               on_started=None) -> int | None:
    """Run setup_command detached and wait for it. started: a previous
    orchestrator already started it on this pod; wait for that run (it may
    still be going, or have finished) instead of starting it again, unless
    it is gone without an exit code. Returns the exit code, or None when it
    timed out, vanished or could not be started."""
    log_path = f"{config.remote_project_dir.rstrip('/')}/{SETUP_LOG}"
    if started:
        state, code = setup_status(config, host, port, key)
        if state == "gone":
            log.warning(f"[{config.name}] Setup started by a previous run is gone without an exit "
                        f"code (pod restarted?); starting it again")
            started = False
        else:
            log.info(f"[{config.name}] Setup was started by a previous run; waiting for it "
                     f"(log {log_path})")
    if not started:
        log.info(f"[{config.name}] Running setup command detached "
                 f"(timeout {config.setup_timeout_seconds}s, log {log_path})")
        if not start_setup(config, host, port, key):
            return None
        if on_started is not None:
            on_started()
    code = wait_setup(config, host, port, key)
    if code != 0:
        what = "timed out or vanished" if code is None else f"exited with code {code}"
        tail = setup_log_tail(config, host, port, key)
        log.error(f"[{config.name}] Setup command {what}" + (f":\n{tail}" if tail else ""))
        return code
    log.info(f"[{config.name}] Setup command finished")
    return 0
