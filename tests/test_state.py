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
    # No leftover tmp files
    files = os.listdir(tmp_dir)
    assert len(files) == 1


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
