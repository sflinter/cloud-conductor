import json
import os

from conductor.state import (
    PodState, RunState, append_cost_event, init_state, load_state,
    read_cost_log, save_state, get_job,
)
from conductor.config import JobConfig


def test_round_trip(tmp_dir):
    path = os.path.join(tmp_dir, "state.json")
    state = RunState(
        jobs=[
            PodState(name="a", pod_id="pod1", status="running", cost_usd=1.23),
            PodState(name="b", status="pending", depends_on=["a"]),
        ],
        total_cost_usd=1.23,
        budget_usd=40.0,
    )
    save_state(state, path)
    loaded = load_state(path)
    assert len(loaded.jobs) == 2
    assert loaded.jobs[0].name == "a"
    assert loaded.jobs[0].pod_id == "pod1"
    assert loaded.jobs[0].cost_usd == 1.23
    assert loaded.jobs[1].depends_on == ["a"]
    assert loaded.total_cost_usd == 1.23
    assert loaded.budget_usd == 40.0


def test_atomic_write(tmp_dir):
    path = os.path.join(tmp_dir, "state.json")
    state = RunState(jobs=[PodState(name="x")], budget_usd=10.0)
    save_state(state, path)
    assert os.path.exists(path)
    # No leftover tmp files (the hidden .lock sibling is expected)
    files = [f for f in os.listdir(tmp_dir) if not f.startswith(".")]
    assert files == ["state.json"]


def test_init_state():
    configs = [
        JobConfig(name="a", run_command="echo a", depends_on=["b"], job_budget_usd=5.0),
        JobConfig(name="b", run_command="echo b", budget_usd=40.0),
    ]
    state = init_state(configs, budget_usd=40.0)
    assert len(state.jobs) == 2
    assert state.jobs[0].name == "a"
    assert state.jobs[0].depends_on == ["b"]
    assert state.jobs[0].job_budget_usd == 5.0
    assert state.budget_usd == 40.0


def test_get_job():
    state = RunState(jobs=[PodState(name="a"), PodState(name="b")])
    assert get_job(state, "a").name == "a"
    assert get_job(state, "c") is None


def test_cost_log(tmp_dir):
    path = os.path.join(tmp_dir, "cost.jsonl")
    append_cost_event(path, {"event": "pod_started", "job": "a", "ts": 1000.0})
    append_cost_event(path, {"event": "pod_stopped", "job": "a", "ts": 2000.0})
    events = read_cost_log(path)
    assert len(events) == 2
    assert events[0]["event"] == "pod_started"
    assert events[1]["ts"] == 2000.0


def test_read_cost_log_missing(tmp_dir):
    path = os.path.join(tmp_dir, "missing.jsonl")
    assert read_cost_log(path) == []


def test_run_id_generated_and_persisted(tmp_dir):
    from conductor.state import new_run_id, pod_name_for
    configs = [JobConfig(name="a", run_command="echo a")]
    state = init_state(configs)
    assert len(state.run_id) == 6
    path = os.path.join(tmp_dir, "state.json")
    save_state(state, path)
    assert load_state(path).run_id == state.run_id
    assert pod_name_for(state.run_id, "a") == f"conductor-{state.run_id}-a"
    assert pod_name_for("", "a") == "conductor-a"
    assert new_run_id() != new_run_id()


def test_load_old_state_without_run_id(tmp_dir):
    path = os.path.join(tmp_dir, "state.json")
    with open(path, "w") as f:
        json.dump({"jobs": [{"name": "a", "status": "running", "pod_id": "p"}],
                   "total_cost_usd": 1.0, "budget_usd": 5.0}, f)
    state = load_state(path)
    assert state.run_id == ""
    assert state.jobs[0].pod_id == "p"


def test_reset_job_for_retry():
    from conductor.state import reset_job_for_retry
    import time
    job = PodState(name="a", status="failed", pod_id="p", ssh_host="h", ssh_port=1,
                   pid=5, pid_start="x", exit_code=1, error="boom", cost_usd=2.0,
                   started_at=time.time(), provision_attempts=3, gone_confirmations=2,
                   gpu_type="A2000", gpu_cost_per_hour=0.5)
    reset_job_for_retry(job)
    assert job.status == "pending"
    assert job.pod_id is None and job.ssh_host is None
    assert job.pid is None and job.exit_code is None and job.error is None
    assert job.cost_usd == 0.0 and job.started_at is None
    assert job.provision_attempts == 0 and job.gone_confirmations == 0

    job2 = PodState(name="b", status="completed", pod_id="keep", ssh_host="h", ssh_port=1, pid=5)
    reset_job_for_retry(job2, keep_pod=True)
    assert job2.pod_id == "keep" and job2.ssh_host == "h"
    assert job2.pid is None and job2.status == "pending"


def test_concurrent_save_state_is_safe(tmp_dir):
    import threading
    path = os.path.join(tmp_dir, "state.json")
    state = RunState(jobs=[PodState(name=f"j{i}") for i in range(20)], run_id="abc123")

    def writer():
        for _ in range(15):
            save_state(state, path)
            loaded = load_state(path)
            assert len(loaded.jobs) == 20  # never a partial read

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert load_state(path).run_id == "abc123"


def test_run_is_live_and_owner_description(tmp_dir):
    import time
    from conductor.state import run_is_live, describe_owner
    state = RunState(jobs=[], run_id="abc123")
    assert run_is_live(state) is False
    assert "not running" in describe_owner(state)
    state.orchestrator_pid = 4242
    state.heartbeat_at = time.time() - 10
    assert run_is_live(state, poll_interval=30) is True
    assert "live (pid 4242" in describe_owner(state)
    state.heartbeat_at = time.time() - 61
    assert run_is_live(state, poll_interval=30) is False
    assert "crashed or hard-killed" in describe_owner(state)
    # Round trip
    path = os.path.join(tmp_dir, "state.json")
    save_state(state, path)
    loaded = load_state(path)
    assert loaded.orchestrator_pid == 4242 and loaded.heartbeat_at == state.heartbeat_at


def test_accrue_cost_from_pod_creation():
    from conductor.state import accrue_cost
    job = PodState(name="a", status="provisioning", started_at=1000.0, gpu_cost_per_hour=0.5)
    state = RunState(jobs=[job], total_cost_usd=1.0)
    assert accrue_cost(job, state, now=1000.0 + 3600) == 0.5
    assert job.cost_usd == 0.5 and state.total_cost_usd == 1.5
    assert accrue_cost(job, state, now=1000.0 + 7200) == 0.5
    assert job.cost_usd == 1.0 and state.total_cost_usd == 2.0
    # Never goes backwards and never counts a pod with no rate or no start
    assert accrue_cost(job, state, now=1000.0) == 0.0 and job.cost_usd == 1.0
    assert accrue_cost(PodState(name="b", started_at=1.0), state) == 0.0
    assert accrue_cost(PodState(name="c", gpu_cost_per_hour=1.0), state) == 0.0
