# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field


@dataclass
class PodState:
    name: str
    pod_id: str | None = None
    gpu_type: str | None = None
    gpu_cost_per_hour: float = 0.0
    ssh_host: str | None = None
    ssh_port: int | None = None
    status: str = "pending"  # pending, provisioning, deploying, running, completed, failed, skipped
    started_at: float | None = None
    last_sync_at: float | None = None
    idle_since: float | None = None
    stalled_since: float | None = None
    provision_attempts: int = 0
    cost_usd: float = 0.0
    job_budget_usd: float = 0.0
    depends_on: list[str] = field(default_factory=list)
    pid: int | None = None
    error: str | None = None


@dataclass
class RunState:
    jobs: list[PodState] = field(default_factory=list)
    total_cost_usd: float = 0.0
    budget_usd: float = 0.0


def _pod_from_dict(d: dict) -> PodState:
    valid = {f.name for f in PodState.__dataclass_fields__.values()}
    return PodState(**{k: v for k, v in d.items() if k in valid})


def load_state(path: str) -> RunState:
    with open(path) as f:
        data = json.load(f)
    jobs = [_pod_from_dict(j) for j in data.get("jobs", [])]
    return RunState(
        jobs=jobs,
        total_cost_usd=data.get("total_cost_usd", 0.0),
        budget_usd=data.get("budget_usd", 0.0),
    )


def save_state(state: RunState, path: str) -> None:
    data = {
        "jobs": [asdict(j) for j in state.jobs],
        "total_cost_usd": state.total_cost_usd,
        "budget_usd": state.budget_usd,
    }
    dir_name = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def init_state(configs, budget_usd: float = 0.0) -> RunState:
    from conductor.config import JobConfig
    jobs = []
    for cfg in configs:
        jobs.append(PodState(
            name=cfg.name,
            depends_on=list(cfg.depends_on),
            job_budget_usd=cfg.job_budget_usd,
        ))
    return RunState(jobs=jobs, budget_usd=budget_usd or (configs[0].budget_usd if configs else 0.0))


def get_job(state: RunState, name: str) -> PodState | None:
    for j in state.jobs:
        if j.name == name:
            return j
    return None


def append_cost_event(path: str, event: dict) -> None:
    if "ts" not in event:
        event["ts"] = time.time()
    with open(path, "a") as f:
        f.write(json.dumps(event) + "\n")


def read_cost_log(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events
