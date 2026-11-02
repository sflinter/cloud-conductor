# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from conductor.filelock import locked

REGISTRY_DIR = Path.home() / ".conductor"
REGISTRY_FILE = REGISTRY_DIR / "pods.json"


@dataclass
class PodRecord:
    pod_id: str
    name: str
    gpu_type: str = ""
    gpu_cost_per_hour: float = 0.0
    ssh_host: str | None = None
    ssh_port: int | None = None
    ssh_key_path: str = ""
    image_name: str = ""
    status: str = "running"  # running, stopped, terminated
    created_at: float = 0.0
    source: str = "imperative"  # imperative, declarative
    provider: str = "runpod"  # runpod, vastai
    remote_project_dir: str = "/workspace/project"
    cost_usd: float = 0.0
    pid: int | None = None
    job_name: str | None = None
    config_file: str | None = None
    run_id: str | None = None


def _record_from_dict(d: dict) -> PodRecord:
    valid = {f.name for f in PodRecord.__dataclass_fields__.values()}
    return PodRecord(**{k: v for k, v in d.items() if k in valid})


def _ensure_dir() -> None:
    os.makedirs(REGISTRY_DIR, exist_ok=True)


def _load_unlocked() -> list[PodRecord]:
    if not os.path.exists(REGISTRY_FILE):
        return []
    with open(REGISTRY_FILE) as f:
        data = json.load(f)
    return [_record_from_dict(r) for r in data]


def _save_unlocked(pods: list[PodRecord]) -> None:
    _ensure_dir()
    data = [asdict(r) for r in pods]
    fd, tmp = tempfile.mkstemp(dir=str(REGISTRY_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, str(REGISTRY_FILE))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_registry() -> list[PodRecord]:
    _ensure_dir()
    with locked(str(REGISTRY_FILE)):
        return _load_unlocked()


def save_registry(pods: list[PodRecord]) -> None:
    _ensure_dir()
    with locked(str(REGISTRY_FILE)):
        _save_unlocked(pods)


@contextlib.contextmanager
def _modify():
    """Read-modify-write the registry under the cross-process lock."""
    _ensure_dir()
    with locked(str(REGISTRY_FILE)):
        pods = _load_unlocked()
        yield pods
        _save_unlocked(pods)


def get_pod(identifier: str) -> PodRecord | None:
    pods = load_registry()
    for p in pods:
        if p.name == identifier:
            return p
    for p in pods:
        if p.pod_id.startswith(identifier):
            return p
    return None


def add_pod(record: PodRecord) -> None:
    with _modify() as pods:
        pods[:] = [p for p in pods if p.pod_id != record.pod_id]
        pods.append(record)


def update_pod(pod_id: str, **kwargs) -> PodRecord | None:
    with _modify() as pods:
        for p in pods:
            if p.pod_id == pod_id:
                for k, v in kwargs.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
                return p
    return None


def remove_pod(pod_id: str) -> bool:
    with _modify() as pods:
        filtered = [p for p in pods if p.pod_id != pod_id]
        removed = len(filtered) != len(pods)
        pods[:] = filtered
    return removed


def list_pods(status: str | None = None) -> list[PodRecord]:
    pods = load_registry()
    if status:
        return [p for p in pods if p.status == status]
    return pods


def reconcile_with_provider() -> list[PodRecord]:
    """Check all pods against their provider and update stale entries.

    Marks running pods as terminated if the provider says they're gone,
    and resurrects terminated pods if the provider says they're still alive.
    A provider answer of None (could not tell) leaves the record alone.

    Returns the updated list of all pods."""
    from conductor.providers import get_configured_provider

    providers: dict[str, object] = {}
    with _modify() as pods:
        for p in pods:
            if p.provider not in providers:
                prov = get_configured_provider(p.provider)
                if not prov:
                    continue
                providers[p.provider] = prov
            prov = providers[p.provider]
            try:
                exists = prov.check_exists(p.pod_id)
            except Exception:
                continue
            if exists is None:
                continue
            if exists and p.status == "terminated":
                p.status = "running"
            elif not exists and p.status != "terminated":
                p.status = "terminated"
        return list(pods)
