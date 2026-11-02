# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class SyncPath:
    remote: str
    local: str
    # True: any failure to sync this entry is logged and ignored. False (the
    # default): a source missing on the pod is "nothing to sync", but a
    # transfer failure of an existing source fails the sync.
    optional: bool = False


@dataclass
class NotificationConfig:
    on_job_complete: bool = True
    on_job_failure: bool = True
    on_budget_threshold: float = 0.8
    on_spot_recovery: bool = True
    backend: str = "terminal-notifier"
    pushover_user_key: str = ""
    pushover_app_token: str = ""
    notify_command: str = ""


@dataclass
class JobConfig:
    name: str
    run_command: str

    # Provider
    provider: str = "runpod"  # "runpod" or "vastai"

    # GPU / provisioning
    gpu_type_id: str = ""
    gpu_type_ids_fallback: list[str] = field(default_factory=list)
    auto_select_cheapest_gpu: bool = False
    gpu_min_vram_gb: int = 0
    cloud_type: str = "ALL"
    image_name: str = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
    container_disk_in_gb: int = 40
    volume_in_gb: int = 0
    volume_mount_path: str = "/workspace"
    ssh_key_path: str = "~/.ssh/id_rsa"
    provision_timeout_seconds: int = 900  # max wait for a new pod's SSH to come up

    # Deployment
    deploy_method: str = "rsync"
    local_project_dir: str = "."
    remote_project_dir: str = "/workspace/project"
    rsync_excludes: list[str] = field(default_factory=lambda: [
        ".venv/", "__pycache__/", "*.pyc", ".git/",
    ])
    setup_command: str = ""
    upload_paths: list[SyncPath] = field(default_factory=list)
    setup_timeout_seconds: int = 1800     # max time for setup_command
    deploy_timeout_seconds: int = 600     # max time for each deploy rsync

    # Sync
    sync_interval_minutes: int = 5
    sync_paths: list[SyncPath] = field(default_factory=list)
    sync_timeout_seconds: int = 600       # max time for each sync rsync

    # Cost
    budget_usd: float = 0.0
    job_budget_usd: float = 0.0
    idle_timeout_minutes: int = 5
    stall_timeout_minutes: int = 30
    stall_gpu_threshold: int = 5          # GPU util % below which = stalled
    cost_per_hour_override: float = 0.0

    # Spot recovery / provisioning
    max_provision_attempts: int = 5
    backoff_base_seconds: int = 30
    provision_retries: int = 3            # passes over the GPU candidate list when none has capacity
    recovery_confirmations: int = 3       # consecutive "pod gone" ticks before recovering

    # Pod reuse
    keep_pod_alive: bool = False

    # A job that failed (non-zero exit, stall, ...) is always torn down,
    # even if its final sync failed, unless this is set
    keep_pod_on_failure: bool = False

    # Crash handling: tear down every live pod if the orchestrator itself dies
    teardown_on_crash: bool = False

    # State files
    state_file: str = ".conductor_state.json"
    cost_log_file: str = ".conductor_cost_log.jsonl"

    # Notifications
    notifications: NotificationConfig | None = None

    # Vast.ai-specific (ignored by RunPod)
    vastai_min_reliability: float = 0.95
    vastai_bid_price: float = 0.0
    vastai_num_gpus: int = 1
    vastai_geolocation: str = ""

    # Dependencies
    depends_on: list[str] = field(default_factory=list)


# Fields that are per-job only (not inherited from global)
_JOB_ONLY_FIELDS = {"name", "run_command", "depends_on", "job_budget_usd"}

# All valid fields on JobConfig
_ALL_FIELDS = {f.name for f in JobConfig.__dataclass_fields__.values()}


def _parse_sync_paths(raw: list[dict]) -> list[SyncPath]:
    return [SyncPath(remote=sp["remote"], local=sp["local"], optional=bool(sp.get("optional", False)))
            for sp in raw]


# Local paths in the config that are resolved against the config file's directory
_LOCAL_PATH_FIELDS = ("state_file", "cost_log_file", "local_project_dir", "ssh_key_path")


def resolve_path(base_dir: str, path: str) -> str:
    """A relative path in the config means relative to the config file,
    not to wherever the CLI happens to be run from. Absolute paths (and
    `~`, expanded first) are returned as they are."""
    path = os.path.expanduser(path)
    if not path or os.path.isabs(path):
        return path
    resolved = os.path.normpath(os.path.join(base_dir, path))
    return resolved + "/" if path.endswith("/") else resolved


def _resolve_local_paths(merged: dict, base_dir: str) -> None:
    for key in _LOCAL_PATH_FIELDS:
        if key in merged:
            merged[key] = resolve_path(base_dir, merged[key])
    for key in ("sync_paths", "upload_paths"):
        if key in merged:
            merged[key] = [SyncPath(remote=sp.remote, local=resolve_path(base_dir, sp.local),
                                    optional=sp.optional) for sp in merged[key]]


def _parse_notifications(raw: dict) -> NotificationConfig:
    return NotificationConfig(**{k: v for k, v in raw.items() if k in NotificationConfig.__dataclass_fields__})


def load_config(path: str, job_names: list[str] | None = None) -> list[JobConfig]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    base_dir = os.path.dirname(os.path.abspath(path))

    global_cfg = dict(data.get("global", {}))
    jobs_raw = data.get("jobs", [])
    if not jobs_raw:
        raise ValueError("No [[jobs]] defined in config")

    # Extract notifications from global
    global_notifications = None
    if "notifications" in global_cfg:
        global_notifications = _parse_notifications(global_cfg.pop("notifications"))

    # Parse global sync_paths and upload_paths
    if "sync_paths" in global_cfg:
        global_cfg["sync_paths"] = _parse_sync_paths(global_cfg["sync_paths"])
    if "upload_paths" in global_cfg:
        global_cfg["upload_paths"] = _parse_sync_paths(global_cfg["upload_paths"])

    configs = []
    for job in jobs_raw:
        if "name" not in job:
            raise ValueError("Each [[jobs]] entry must have a 'name' field")
        if "run_command" not in job:
            raise ValueError(f"Job '{job['name']}' must have a 'run_command' field")

        merged = {}
        # Start with global defaults (skip job-only fields)
        for k, v in global_cfg.items():
            if k in _ALL_FIELDS and k not in _JOB_ONLY_FIELDS:
                merged[k] = v

        # Override with per-job values
        job_notifications = None
        for k, v in job.items():
            if k == "notifications":
                job_notifications = _parse_notifications(v)
            elif k in ("sync_paths", "upload_paths"):
                merged[k] = _parse_sync_paths(v)
            elif k in _ALL_FIELDS:
                merged[k] = v

        # Resolve notifications: job-level overrides global
        merged["notifications"] = job_notifications or global_notifications

        # Expand ssh_key_path
        if "ssh_key_path" in merged:
            merged["ssh_key_path"] = os.path.expanduser(merged["ssh_key_path"])

        # Default state files live next to the config, not in the cwd
        merged.setdefault("state_file", JobConfig.state_file)
        merged.setdefault("cost_log_file", JobConfig.cost_log_file)
        merged.setdefault("local_project_dir", JobConfig.local_project_dir)
        _resolve_local_paths(merged, base_dir)

        configs.append(JobConfig(**merged))

    # Filter by requested job names
    if job_names:
        name_set = set(job_names)
        filtered = [c for c in configs if c.name in name_set]
        missing = name_set - {c.name for c in filtered}
        if missing:
            raise ValueError(f"Unknown job names: {', '.join(sorted(missing))}")
        configs = filtered

    return configs
