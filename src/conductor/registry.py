# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    remote_project_dir: str = "/workspace/project"
    cost_usd: float = 0.0
    pid: int | None = None
    job_name: str | None = None
    config_file: str | None = None


def _record_from_dict(d: dict) -> PodRecord:
    valid = {f.name for f in PodRecord.__dataclass_fields__.values()}
    return PodRecord(**{k: v for k, v in d.items() if k in valid})


def _ensure_dir() -> None:
    os.makedirs(REGISTRY_DIR, exist_ok=True)


def load_registry() -> list[PodRecord]:
    if not os.path.exists(REGISTRY_FILE):
        return []
    with open(REGISTRY_FILE) as f:
        data = json.load(f)
    return [_record_from_dict(r) for r in data]


def save_registry(pods: list[PodRecord]) -> None:
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
    pods = load_registry()
    pods = [p for p in pods if p.pod_id != record.pod_id]
    pods.append(record)
    save_registry(pods)


def update_pod(pod_id: str, **kwargs) -> PodRecord | None:
    pods = load_registry()
    for p in pods:
        if p.pod_id == pod_id:
            for k, v in kwargs.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            save_registry(pods)
            return p
    return None


def remove_pod(pod_id: str) -> bool:
    pods = load_registry()
    filtered = [p for p in pods if p.pod_id != pod_id]
    if len(filtered) == len(pods):
        return False
    save_registry(filtered)
    return True


def list_pods(status: str | None = None) -> list[PodRecord]:
    pods = load_registry()
    if status:
        return [p for p in pods if p.status == status]
    return pods
