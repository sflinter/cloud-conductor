# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import contextlib
import json
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field

from conductor.filelock import locked


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
    gone_confirmations: int = 0    # consecutive ticks the provider reported the pod gone
    cost_usd: float = 0.0
    job_budget_usd: float = 0.0
    depends_on: list[str] = field(default_factory=list)
    pid: int | None = None
    pid_start: str | None = None   # process start time, detects pid reuse
    exit_code: int | None = None   # from the remote exit file, once the job ends
    error: str | None = None


@dataclass
class RunState:
    jobs: list[PodState] = field(default_factory=list)
    total_cost_usd: float = 0.0
    budget_usd: float = 0.0
    run_id: str = ""  # short id naming this run's pods: conductor-<run_id>-<job>
    orchestrator_pid: int | None = None  # pid of the live `conductor run`, if any
    heartbeat_at: float | None = None    # last tick of the live `conductor run`


def run_is_live(state: RunState, poll_interval: float = 30.0, now: float | None = None) -> bool:
    """True if an orchestrator has heart-beaten this state within the last
    two poll intervals. A detached or finished run clears its heartbeat."""
    if not state.heartbeat_at:
        return False
    now = time.time() if now is None else now
    return (now - state.heartbeat_at) < 2 * poll_interval


def describe_owner(state: RunState, poll_interval: float = 30.0) -> str:
    if run_is_live(state, poll_interval):
        age = int(time.time() - state.heartbeat_at)
        return f"live (pid {state.orchestrator_pid}, heartbeat {age}s ago)"
    if state.heartbeat_at:
        age = int(time.time() - state.heartbeat_at)
        return f"not running (last heartbeat {age}s ago; crashed or hard-killed)"
    return "not running (detached or finished)"


def new_run_id() -> str:
    return secrets.token_hex(3)


def pod_name_for(run_id: str, job_name: str) -> str:
    return f"conductor-{run_id}-{job_name}" if run_id else f"conductor-{job_name}"


def _pod_from_dict(d: dict) -> PodState:
    valid = {f.name for f in PodState.__dataclass_fields__.values()}
    return PodState(**{k: v for k, v in d.items() if k in valid})


def load_state(path: str, lock: bool = True) -> RunState:
    with locked(path) if lock else contextlib.nullcontext():
        with open(path) as f:
            data = json.load(f)
    jobs = [_pod_from_dict(j) for j in data.get("jobs", [])]
    return RunState(
        jobs=jobs,
        total_cost_usd=data.get("total_cost_usd", 0.0),
        budget_usd=data.get("budget_usd", 0.0),
        run_id=data.get("run_id", ""),
        orchestrator_pid=data.get("orchestrator_pid"),
        heartbeat_at=data.get("heartbeat_at"),
    )


def save_state(state: RunState, path: str, lock: bool = True) -> None:
    data = {
        "jobs": [asdict(j) for j in state.jobs],
        "total_cost_usd": state.total_cost_usd,
        "budget_usd": state.budget_usd,
        "run_id": state.run_id,
        "orchestrator_pid": state.orchestrator_pid,
        "heartbeat_at": state.heartbeat_at,
    }
    dir_name = os.path.dirname(path) or "."
    with locked(path) if lock else contextlib.nullcontext():
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
    return RunState(jobs=jobs, budget_usd=budget_usd or (configs[0].budget_usd if configs else 0.0),
                    run_id=new_run_id())


def get_job(state: RunState, name: str) -> PodState | None:
    for j in state.jobs:
        if j.name == name:
            return j
    return None


def reset_job_for_retry(job: PodState, keep_pod: bool = False) -> None:
    """Return a finished job to pending so `conductor run` re-runs it.
    Cost already spent stays counted in total_cost_usd; the job's own
    counter restarts with its new pod."""
    if not keep_pod:
        job.pod_id = None
        job.ssh_host = None
        job.ssh_port = None
        job.gpu_type = None
        job.gpu_cost_per_hour = 0.0
    job.status = "pending"
    job.started_at = None
    job.last_sync_at = None
    job.idle_since = None
    job.stalled_since = None
    job.provision_attempts = 0
    job.gone_confirmations = 0
    job.cost_usd = 0.0
    job.pid = None
    job.pid_start = None
    job.exit_code = None
    job.error = None


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
