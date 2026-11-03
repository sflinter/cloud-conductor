# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import os
import re
import subprocess

from conductor.config import JobConfig
from conductor.ssh import rsync, rsync_pull, ssh_exec
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


def push_spec(remote_base: str, sp) -> tuple[str, str, str]:
    """(local source, remote destination, remote dir to create) for pushing
    a sync_paths entry back to the pod: the inverse of sync_pull, which
    always pulls into the local directory. A remote ending in '/' is a
    directory whose contents were pulled into `local`; anything else (a
    file, or a directory named without a trailing '/') was pulled into
    `local` under its own name, so that item is pushed to the remote's
    parent directory."""
    remote = f"{remote_base}/{sp.remote}"
    if sp.remote.endswith("/"):
        return sp.local.rstrip("/") + "/", remote, remote.rstrip("/")
    name = os.path.basename(sp.remote.rstrip("/"))
    parent = os.path.dirname(remote.rstrip("/"))
    return os.path.join(sp.local, name), parent + "/", parent


def sync_push(config: JobConfig, pod_state: PodState) -> bool:
    """Push local synced results back to the pod (spot recovery). An entry
    with nothing synced locally yet is skipped."""
    if not config.sync_paths:
        return True

    host = pod_state.ssh_host
    port = pod_state.ssh_port
    key = config.ssh_key_path
    remote_base = config.remote_project_dir.rstrip("/")
    all_ok = True

    for sp in config.sync_paths:
        src, dst, remote_dir = push_spec(remote_base, sp)
        if not os.path.exists(src.rstrip("/") or src):
            log.info(f"[{config.name}] Nothing to push for {sp.remote}: {src} does not exist locally")
            continue
        ssh_exec(host, port, key, f"mkdir -p {remote_dir}")
        log.info(f"[{config.name}] Pushing {src} → {dst}")
        result = rsync(src, dst, host, port, key, timeout=config.sync_timeout_seconds)
        if result.returncode != 0:
            log.warning(f"[{config.name}] Push failed for {sp.remote}: {result.stderr}")
            all_ok = False

    return all_ok
