from unittest.mock import patch, MagicMock
import os
import tempfile
import threading

from conductor.config import JobConfig
from conductor.monitor import (
    _all_done, _deps_met, _deps_failed, _propagate_failure,
    _start_unblocked_jobs, _update_cost, _monitor_tick,
)
from conductor.state import PodState, RunState, get_job


def _make_config(**overrides):
    defaults = dict(name="test", run_command="echo hi", ssh_key_path="/key",
                    remote_project_dir="/workspace/proj", budget_usd=40.0)
    defaults.update(overrides)
    return JobConfig(**defaults)


def test_all_done():
    state = RunState(jobs=[
        PodState(name="a", status="completed"),
        PodState(name="b", status="failed"),
        PodState(name="c", status="skipped"),
    ])
    assert _all_done(state) is True

    state.jobs.append(PodState(name="d", status="running"))
    assert _all_done(state) is False


def test_deps_met():
    state = RunState(jobs=[
        PodState(name="a", status="completed"),
        PodState(name="b", status="pending", depends_on=["a"]),
    ])
    assert _deps_met(state.jobs[1], state) is True

    state.jobs[0].status = "running"
    assert _deps_met(state.jobs[1], state) is False


def test_deps_failed():
    state = RunState(jobs=[
        PodState(name="a", status="failed"),
        PodState(name="b", status="pending", depends_on=["a"]),
    ])
    assert _deps_failed(state.jobs[1], state) is True


def test_propagate_failure():
    state = RunState(jobs=[
        PodState(name="a", status="failed"),
        PodState(name="b", status="pending", depends_on=["a"]),
        PodState(name="c", status="pending", depends_on=["b"]),
    ])
    _propagate_failure(state.jobs[0], state)
    assert state.jobs[1].status == "skipped"
    assert state.jobs[2].status == "skipped"  # transitive


def test_update_cost():
    import time
    job = PodState(name="a", status="running", started_at=time.time() - 3600,
                   gpu_cost_per_hour=0.50, cost_usd=0.0)
    state = RunState(jobs=[job], total_cost_usd=0.0, budget_usd=40.0)
    config = _make_config(name="a")

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        cost_path = f.name

    try:
        _update_cost(job, state, config, cost_path)
        assert job.cost_usd > 0.45  # ~1 hour * $0.50
        assert state.total_cost_usd > 0.45
    finally:
        os.unlink(cost_path)


@patch("conductor.monitor._provision_deploy_launch")
def test_start_unblocked_jobs(mock_pdl):
    state = RunState(jobs=[
        PodState(name="a", status="completed"),
        PodState(name="b", status="pending", depends_on=["a"]),
    ])
    configs = [_make_config(name="a"), _make_config(name="b", depends_on=["a"])]
    config_map = {c.name: c for c in configs}

    with tempfile.NamedTemporaryFile(suffix=".json") as sf, \
         tempfile.NamedTemporaryFile(suffix=".jsonl") as cf:
        _start_unblocked_jobs(configs, config_map, state, sf.name, cf.name)
        mock_pdl.assert_called_once()


@patch("conductor.monitor._provision_deploy_launch")
def test_start_unblocked_skips_on_failed_dep(mock_pdl):
    state = RunState(jobs=[
        PodState(name="a", status="failed"),
        PodState(name="b", status="pending", depends_on=["a"]),
    ])
    configs = [_make_config(name="a"), _make_config(name="b", depends_on=["a"])]
    config_map = {c.name: c for c in configs}

    with tempfile.NamedTemporaryFile(suffix=".json") as sf, \
         tempfile.NamedTemporaryFile(suffix=".jsonl") as cf:
        _start_unblocked_jobs(configs, config_map, state, sf.name, cf.name)
        mock_pdl.assert_not_called()
        assert state.jobs[1].status == "skipped"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.check_pod_exists", return_value=True)
@patch("conductor.monitor.is_alive", return_value=True)
def test_monitor_tick_running_job(mock_alive, mock_pod_exists, mock_start, mock_notify):
    import time as _time
    job = PodState(name="a", status="running", started_at=_time.time() - 100,
                   gpu_cost_per_hour=0.12, ssh_host="1.2.3.4", ssh_port=22222, pid=123,
                   last_sync_at=_time.time())
    state = RunState(jobs=[job], total_cost_usd=0.0, budget_usd=40.0)
    config = _make_config(name="a")
    config_map = {"a": config}

    with tempfile.NamedTemporaryFile(suffix=".json") as sf, \
         tempfile.NamedTemporaryFile(suffix=".jsonl") as cf:
        _monitor_tick([config], config_map, state, sf.name, cf.name)
        assert job.idle_since is None  # process alive, no idle


@patch("conductor.monitor._finish_job")
@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.check_pod_exists", return_value=True)
@patch("conductor.monitor.is_alive", return_value=False)
def test_monitor_tick_idle_detection(mock_alive, mock_pod_exists, mock_start, mock_notify, mock_finish):
    import time as _time
    # Process dead, pod alive — should start idle timer
    job = PodState(name="a", status="running", started_at=_time.time() - 100,
                   gpu_cost_per_hour=0.12, ssh_host="1.2.3.4", ssh_port=22222, pid=123,
                   pod_id="pod123", last_sync_at=_time.time())
    state = RunState(jobs=[job], total_cost_usd=0.0, budget_usd=40.0)
    config = _make_config(name="a", idle_timeout_minutes=0)  # immediate timeout
    config_map = {"a": config}

    with tempfile.NamedTemporaryFile(suffix=".json") as sf, \
         tempfile.NamedTemporaryFile(suffix=".jsonl") as cf:
        # First tick: sets idle_since
        _monitor_tick([config], config_map, state, sf.name, cf.name)
        assert job.idle_since is not None

        # Second tick: idle timeout reached (0 minutes)
        _monitor_tick([config], config_map, state, sf.name, cf.name)
        mock_finish.assert_called_once()


@patch("conductor.monitor._provision_deploy_launch")
def test_parallel_launch(mock_pdl):
    """Multiple unblocked jobs should launch in parallel via ThreadPoolExecutor."""
    state = RunState(jobs=[
        PodState(name="a", status="pending"),
        PodState(name="b", status="pending"),
        PodState(name="c", status="pending"),
    ])
    configs = [_make_config(name="a"), _make_config(name="b"), _make_config(name="c")]
    config_map = {c.name: c for c in configs}

    launched_threads = []

    def _track_thread(job, config, state, state_path, cost_log_path, lock=None):
        launched_threads.append(threading.current_thread().name)

    mock_pdl.side_effect = _track_thread

    with tempfile.NamedTemporaryFile(suffix=".json") as sf, \
         tempfile.NamedTemporaryFile(suffix=".jsonl") as cf:
        _start_unblocked_jobs(configs, config_map, state, sf.name, cf.name)

    assert mock_pdl.call_count == 3
    # Verify lock was passed (parallel path)
    for call in mock_pdl.call_args_list:
        assert call.kwargs.get("lock") is not None or (len(call.args) > 5 and call.args[5] is not None)
