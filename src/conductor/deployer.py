from __future__ import annotations

import logging

from conductor.config import JobConfig
from conductor.ssh import rsync, ssh_exec
from conductor.state import PodState

log = logging.getLogger(__name__)


def deploy(config: JobConfig, pod_state: PodState, is_reuse: bool = False) -> bool:
    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = config.ssh_key_path

    if config.deploy_method == "image":
        log.info(f"[{config.name}] Image deploy — skipping code rsync")
        if config.upload_paths or config.setup_command:
            # Still need rsync for upload_paths
            if config.upload_paths:
                _install_rsync(config.name, host, port, key)
        if not _upload_paths(config, host, port, key):
            return False
        if config.setup_command and not is_reuse:
            return _run_setup(config, host, port, key)
        return True

    # rsync deploy
    if not _install_rsync(config.name, host, port, key):
        return False

    src = config.local_project_dir.rstrip("/") + "/"
    dst = config.remote_project_dir.rstrip("/") + "/"

    ssh_exec(host, port, key, f"mkdir -p {dst}")

    log.info(f"[{config.name}] Syncing {src} → {dst}")
    result = rsync(src, dst, host, port, key, excludes=config.rsync_excludes, delete=True)
    if result.returncode != 0:
        log.error(f"[{config.name}] rsync failed: {result.stderr}")
        return False

    if not _upload_paths(config, host, port, key):
        return False

    if config.setup_command and not is_reuse:
        return _run_setup(config, host, port, key)

    return True


def _install_rsync(name: str, host: str, port: int, key: str) -> bool:
    log.info(f"[{name}] Installing rsync on pod")
    result = ssh_exec(host, port, key, "which rsync || apt-get update -qq && apt-get install -y -qq rsync")
    if result.returncode != 0:
        log.error(f"[{name}] Failed to install rsync: {result.stderr}")
        return False
    return True


def _upload_paths(config: JobConfig, host: str, port: int, key: str) -> bool:
    if not config.upload_paths:
        return True
    remote_base = config.remote_project_dir.rstrip("/")
    for up in config.upload_paths:
        remote = f"{remote_base}/{up.remote}"
        src = up.local.rstrip("/") + "/"
        ssh_exec(host, port, key, f"mkdir -p {remote}")
        log.info(f"[{config.name}] Uploading {src} → {remote}")
        result = rsync(src, remote.rstrip("/") + "/", host, port, key)
        if result.returncode != 0:
            log.error(f"[{config.name}] Upload failed for {up.local}: {result.stderr}")
            return False
    return True


def _run_setup(config: JobConfig, host: str, port: int, key: str) -> bool:
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
