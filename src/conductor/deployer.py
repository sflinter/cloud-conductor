# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging

from conductor.ssh import rsync, ssh_exec

log = logging.getLogger(__name__)


def deploy_direct(
    ssh_host: str,
    ssh_port: int,
    ssh_key_path: str,
    local_dir: str = ".",
    remote_dir: str = "/workspace/project",
    setup_command: str = "",
    excludes: list[str] | None = None,
) -> bool:
    name = "pod"
    if not _install_rsync(name, ssh_host, ssh_port, ssh_key_path):
        return False

    src = local_dir.rstrip("/") + "/"
    dst = remote_dir.rstrip("/") + "/"

    ssh_exec(ssh_host, ssh_port, ssh_key_path, f"mkdir -p {dst}")

    log.info(f"[{name}] Syncing {src} → {dst}")
    result = rsync(src, dst, ssh_host, ssh_port, ssh_key_path,
                   excludes=excludes or [], delete=True)
    if result.returncode != 0:
        log.error(f"[{name}] rsync failed: {result.stderr}")
        return False

    if setup_command:
        log.info(f"[{name}] Running setup command")
        result = ssh_exec(
            ssh_host, ssh_port, ssh_key_path,
            f"cd {remote_dir} && {setup_command}",
            timeout=600,
        )
        if result.returncode != 0:
            log.error(f"[{name}] Setup command failed: {result.stderr}")
            return False

    return True


def deploy(config, pod_state, is_reuse: bool = False) -> bool:
    from conductor.config import JobConfig
    cfg: JobConfig = config

    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = cfg.ssh_key_path

    if cfg.deploy_method == "image":
        log.info(f"[{cfg.name}] Image deploy — skipping code rsync")
        if cfg.upload_paths or cfg.setup_command:
            if cfg.upload_paths:
                _install_rsync(cfg.name, host, port, key)
        if not _upload_paths(cfg, host, port, key):
            return False
        if cfg.setup_command and not is_reuse:
            return _run_setup(cfg, host, port, key)
        return True

    # rsync deploy
    if not _install_rsync(cfg.name, host, port, key):
        return False

    src = cfg.local_project_dir.rstrip("/") + "/"
    dst = cfg.remote_project_dir.rstrip("/") + "/"

    ssh_exec(host, port, key, f"mkdir -p {dst}")

    log.info(f"[{cfg.name}] Syncing {src} → {dst}")
    result = rsync(src, dst, host, port, key, excludes=cfg.rsync_excludes, delete=True)
    if result.returncode != 0:
        log.error(f"[{cfg.name}] rsync failed: {result.stderr}")
        return False

    if not _upload_paths(cfg, host, port, key):
        return False

    if cfg.setup_command and not is_reuse:
        return _run_setup(cfg, host, port, key)

    return True


def _install_rsync(name: str, host: str, port: int, key: str) -> bool:
    log.info(f"[{name}] Installing rsync on pod")
    result = ssh_exec(host, port, key, "which rsync || apt-get update -qq && apt-get install -y -qq rsync")
    if result.returncode != 0:
        log.error(f"[{name}] Failed to install rsync: {result.stderr}")
        return False
    return True


def _upload_paths(config, host: str, port: int, key: str) -> bool:
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


def _run_setup(config, host: str, port: int, key: str) -> bool:
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
