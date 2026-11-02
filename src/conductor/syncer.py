# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os
import re
import subprocess

from conductor.config import JobConfig
from conductor.ssh import rsync_pull, rsync
from conductor.state import PodState

log = logging.getLogger(__name__)

# rsync: "some files/attrs were not transferred"; with one of these messages
# naming the source itself it means the source does not exist on the pod
RSYNC_PARTIAL_RC = 23
_MISSING_SOURCE_RE = re.compile(
    r'(?:change_dir|link_stat) "(?P<path>[^"]*)" failed: No such file or directory')


def remote_source_missing(result: subprocess.CompletedProcess, remote: str) -> bool:
    """True if rsync failed only because `remote` (or a directory above it)
    does not exist on the pod: a job that never created its output directory,
    not a transfer error."""
    if result.returncode != RSYNC_PARTIAL_RC:
        return False
    target = remote.rstrip("/")
    for m in _MISSING_SOURCE_RE.finditer(result.stderr or ""):
        path = m.group("path")
        if path.endswith("/."):
            path = path[:-2]
        path = path.rstrip("/")
        if target == path or target.startswith(path + "/"):
            return True
    return False


def sync_pull(config: JobConfig, pod_state: PodState) -> bool:
    """Pull every sync_paths entry. A source that does not exist on the pod
    is nothing to sync; a transfer failure of an existing source fails the
    sync unless the entry is marked optional."""
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
        result = rsync_pull(remote, local, host, port, key, timeout=config.sync_timeout_seconds)
        if result.returncode == 0:
            continue
        if remote_source_missing(result, remote):
            log.info(f"[{config.name}] Nothing to sync for {sp.remote}: {remote} does not exist on the pod")
            continue
        detail = (result.stderr or "").strip()
        if sp.optional:
            log.warning(f"[{config.name}] Optional sync of {sp.remote} failed, ignoring: {detail}")
            continue
        log.warning(f"[{config.name}] Sync failed for {sp.remote}: {detail}")
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
        result = rsync(src, remote, host, port, key, timeout=config.sync_timeout_seconds)
        if result.returncode != 0:
            log.warning(f"[{config.name}] Push failed for {sp.remote}: {result.stderr}")
            all_ok = False

    return all_ok
