"""Lifecycle-level tests: crash handling, resume, detach. All provider and
SSH calls are mocked; nothing here talks to a cloud API."""
from unittest.mock import patch, MagicMock
import os
import tempfile
import time

import pytest

from conductor.config import JobConfig
from conductor.errors import ConductorError
from conductor.monitor import (
    _monitor_tick, _provision_deploy_launch, live_pods, run_lifecycle,
)
from conductor.state import PodState, RunState, load_state


def _make_config(**overrides):
    defaults = dict(name="test", run_command="echo hi", ssh_key_path="/key",
                    remote_project_dir="/workspace/proj", budget_usd=40.0)
    defaults.update(overrides)
    return JobConfig(**defaults)


def _provider():
    p = MagicMock()
    p.name = "runpod"
    p.check_exists.return_value = True
    p.terminate.return_value = True
    return p


@pytest.fixture
def paths(tmp_dir):
    return os.path.join(tmp_dir, "state.json"), os.path.join(tmp_dir, "cost.jsonl")


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.provision_pod")
def test_pipeline_exception_fails_job_instead_of_raising(mock_prov, mock_reg, mock_notify, paths):
    state_path, cost_path = paths
    mock_prov.side_effect = ConductorError("runpod.get_pod: QueryError: 502")
    job = PodState(name="a", status="pending")
    state = RunState(jobs=[job])
    config = _make_config(name="a")

    _provision_deploy_launch(_provider(), job, config, state, state_path, cost_path)

    assert job.status == "failed"
    assert "502" in job.error
    assert os.path.exists(state_path)  # state saved in finally
    mock_notify.assert_called_once()


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.registry_add")
@patch("conductor.monitor.deploy")
@patch("conductor.monitor.provision_pod")
def test_pipeline_unexpected_error_tears_down_pod(mock_prov, mock_deploy, mock_add, mock_reg, mock_notify, paths):
    state_path, cost_path = paths

    def _provision(provider, config, pod_state):
        pod_state.pod_id = "pod123"
        pod_state.status = "provisioning"
        return pod_state
    mock_prov.side_effect = _provision
    mock_deploy.side_effect = TypeError("bug in deploy")
    provider = _provider()
    job = PodState(name="a", status="pending")
    state = RunState(jobs=[job])

    _provision_deploy_launch(provider, job, _make_config(name="a"), state, state_path, cost_path)

    assert job.status == "failed"
    assert "TypeError" in job.error
    provider.terminate.assert_called_once_with("pod123")


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.is_alive", side_effect=ConductorError("ssh: TimeoutExpired"))
def test_tick_transient_error_keeps_job_running(mock_alive, mock_start, mock_notify, paths):
    state_path, cost_path = paths
    job = PodState(name="a", status="running", pod_id="pod1", ssh_host="h", ssh_port=1, pid=5)
    state = RunState(jobs=[job])
    config = _make_config(name="a")
    _monitor_tick([config], {"a": config}, {"a": _provider()}, state, state_path, cost_path)
    assert job.status == "running"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.sync_pull", return_value=True)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.is_alive", side_effect=TypeError("bug"))
def test_tick_unexpected_error_fails_job_and_releases_pod(mock_alive, mock_reg, mock_sync, mock_start, mock_notify, paths):
    state_path, cost_path = paths
    provider = _provider()
    job = PodState(name="a", status="running", pod_id="pod1", ssh_host="h", ssh_port=1, pid=5)
    state = RunState(jobs=[job])
    config = _make_config(name="a")
    _monitor_tick([config], {"a": config}, {"a": provider}, state, state_path, cost_path)
    assert job.status == "failed"
    assert "TypeError" in job.error
    provider.terminate.assert_called_once_with("pod1")


def test_live_pods():
    state = RunState(jobs=[
        PodState(name="a", status="running", pod_id="p1"),
        PodState(name="b", status="deploying", pod_id="p2"),
        PodState(name="c", status="completed", pod_id="p3"),
        PodState(name="d", status="pending"),
    ])
    assert [j.name for j in live_pods(state)] == ["a", "b"]


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._teardown_all")
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor._monitor_tick", side_effect=RuntimeError("tick exploded"))
def test_lifecycle_crash_saves_state_and_prints_pods(mock_tick, mock_start, mock_teardown, mock_notify, paths, capsys):
    state_path, cost_path = paths
    job = PodState(name="a", status="running", pod_id="pod-live", gpu_type="A2000")
    state = RunState(jobs=[job])
    config = _make_config(name="a", notifications=MagicMock())

    with patch("conductor.monitor.get_provider", return_value=_provider()):
        with pytest.raises(RuntimeError, match="tick exploded"):
            run_lifecycle([config], state, state_path, cost_path)

    err = capsys.readouterr().err
    assert "pod-live" in err
    assert "a:" in err
    assert load_state(state_path).jobs[0].pod_id == "pod-live"
    assert job.status == "running"  # nothing marked failed, nothing torn down
    mock_teardown.assert_not_called()
    assert mock_notify.call_args[0][1] == "orchestrator_crashed"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._teardown_all")
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor._monitor_tick", side_effect=RuntimeError("tick exploded"))
def test_lifecycle_crash_teardown_on_crash(mock_tick, mock_start, mock_teardown, mock_notify, paths):
    state_path, cost_path = paths
    job = PodState(name="a", status="running", pod_id="pod-live")
    state = RunState(jobs=[job])
    config = _make_config(name="a", teardown_on_crash=True)

    with patch("conductor.monitor.get_provider", return_value=_provider()):
        with pytest.raises(RuntimeError):
            run_lifecycle([config], state, state_path, cost_path)

    mock_teardown.assert_called_once()
    assert "crashed" in mock_teardown.call_args[1]["reason"]
