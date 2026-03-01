# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os

from conductor.config import JobConfig
from conductor.ssh import rsync_pull, rsync
from conductor.state import PodState

log = logging.getLogger(__name__)


def sync_pull(config: JobConfig, pod_state: PodState) -> bool:
    if not config.sync_paths:
        return True

    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = config.ssh_key_path
    remote_base = config.remote_project_dir.rstrip("/")
    all_ok = True

    for sp in config.sync_paths:
        remote = f"{remote_base}/{sp.remote}"
        local = sp.local
        os.makedirs(local, exist_ok=True)
        log.info(f"[{config.name}] Syncing {remote} → {local}")
        result = rsync_pull(remote, local, host, port, key)
        if result.returncode != 0:
            log.warning(f"[{config.name}] Sync failed for {sp.remote}: {result.stderr}")
            all_ok = False

    return all_ok


def sync_push(config: JobConfig, pod_state: PodState) -> bool:
    """Push local synced results back to pod (for spot recovery)."""
    if not config.sync_paths:
        return True

    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = config.ssh_key_path
    remote_base = config.remote_project_dir.rstrip("/")
    all_ok = True

    for sp in config.sync_paths:
        local = sp.local
        if not os.path.exists(local):
            continue
        remote = f"{remote_base}/{sp.remote}"
        src = local.rstrip("/") + "/"
        log.info(f"[{config.name}] Pushing {src} → {remote}")
        result = rsync(src, remote, host, port, key)
        if result.returncode != 0:
            log.warning(f"[{config.name}] Push failed for {sp.remote}: {result.stderr}")
            all_ok = False

    return all_ok
