from __future__ import annotations

import logging

from conductor.config import JobConfig
from conductor.ssh import rsync, ssh_exec
from conductor.state import PodState

log = logging.getLogger(__name__)


def deploy(config: JobConfig, pod_state: PodState, is_reuse: bool = False) -> bool:
    if config.deploy_method == "image":
        log.info(f"[{config.name}] Image deploy — skipping rsync/setup")
        return True

    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = config.ssh_key_path

    # Install rsync on pod (most RunPod images lack it)
    log.info(f"[{config.name}] Installing rsync on pod")
    result = ssh_exec(host, port, key, "which rsync || apt-get update -qq && apt-get install -y -qq rsync")
    if result.returncode != 0:
        log.error(f"[{config.name}] Failed to install rsync: {result.stderr}")
        return False

    # Rsync code to pod
    src = config.local_project_dir.rstrip("/") + "/"
    dst = config.remote_project_dir.rstrip("/") + "/"

    # Ensure remote dir exists
    ssh_exec(host, port, key, f"mkdir -p {dst}")

    log.info(f"[{config.name}] Syncing {src} → {dst}")
    result = rsync(
        src, dst, host, port, key,
        excludes=config.rsync_excludes,
        delete=True,
    )
    if result.returncode != 0:
        log.error(f"[{config.name}] rsync failed: {result.stderr}")
        return False

    # Run setup command (skip for pod reuse — deps already installed)
    if config.setup_command and not is_reuse:
        log.info(f"[{config.name}] Running setup command")
        result = ssh_exec(
            host, port, key,
            f"cd {config.remote_project_dir} && {config.setup_command}",
            timeout=600,
        )
        if result.returncode != 0:
            log.error(f"[{config.name}] Setup command failed: {result.stderr}")
            return False

    return True
