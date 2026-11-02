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

    def _provision(provider, config, pod_state, run_id="", on_created=None, on_released=None):
        pod_state.pod_id = "pod123"
        pod_state.status = "provisioning"
        if on_created:
            on_created(pod_state)
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


# --- persist-first, reconcile, resume -------------------------------------

@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.registry_add")
@patch("conductor.monitor.launch")
@patch("conductor.monitor.deploy", return_value=True)
@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_pipeline_persists_pod_id_before_ssh_wait(mock_wait, mock_deploy, mock_launch, mock_add, mock_upd, mock_notify, paths):
    from conductor.runner import LaunchResult
    state_path, cost_path = paths
    provider = _provider()
    provider.create_instance.return_value = {"id": "pod-new", "cost_per_hour": 0.3}
    observed = {}

    def _wait_for_ssh(instance_id, timeout=300):
        observed["state_at_wait"] = load_state(state_path)
        observed["registry_calls"] = mock_add.call_count
        return ("1.2.3.4", 22222)
    provider.wait_for_ssh.side_effect = _wait_for_ssh
    mock_launch.return_value = LaunchResult(pid=7, pid_start="99")

    job = PodState(name="a", status="pending")
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a", gpu_type_id="NVIDIA RTX A2000")
    _provision_deploy_launch(provider, job, config, state, state_path, cost_path)

    persisted = observed["state_at_wait"].jobs[0]
    assert persisted.pod_id == "pod-new"
    assert persisted.status == "provisioning"
    assert observed["registry_calls"] == 1
    record = mock_add.call_args_list[0][0][0]
    assert record.name == "conductor-abc123-a" and record.run_id == "abc123"
    assert provider.create_instance.call_args[1]["name"] == "conductor-abc123-a"
    assert job.status == "running" and job.pid == 7 and job.pid_start == "99"
    events = [__import__("json").loads(l) for l in open(cost_path)]
    assert events[0]["event"] == "pod_started" and events[0]["pod_id"] == "pod-new"


@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.registry_add")
def test_reconcile_adopts_and_terminates_strays(mock_add, mock_upd, paths, capsys):
    from conductor.monitor import reconcile_run_pods
    state_path, cost_path = paths
    provider = _provider()
    provider.list_instances.return_value = [
        {"id": "p-train", "name": "conductor-abc123-train", "alive": True},   # tracked: fine
        {"id": "p-eval", "name": "conductor-abc123-eval", "alive": True},     # unpersisted: adopt
        {"id": "p-ghost", "name": "conductor-abc123-ghost", "alive": True},   # no job: terminate
        {"id": "p-done", "name": "conductor-abc123-done", "alive": True},     # job finished: terminate
        {"id": "p-other", "name": "conductor-zzz999-train", "alive": True},   # other run: ignore
        {"id": "p-dead", "name": "conductor-abc123-dead", "alive": False},    # not alive: ignore
    ]
    state = RunState(run_id="abc123", jobs=[
        PodState(name="train", status="running", pod_id="p-train"),
        PodState(name="eval", status="pending"),
        PodState(name="done", status="completed"),
        PodState(name="dead", status="pending"),
    ])
    configs = {n: _make_config(name=n) for n in ("train", "eval", "done", "dead")}
    providers = {n: provider for n in configs}

    reconcile_run_pods(configs, providers, state, state_path, cost_path)

    assert sorted(c[0][0] for c in provider.terminate.call_args_list) == ["p-done", "p-ghost"]
    eval_job = [j for j in state.jobs if j.name == "eval"][0]
    assert eval_job.pod_id == "p-eval" and eval_job.status == "provisioning"
    assert [j for j in state.jobs if j.name == "dead"][0].pod_id is None
    assert load_state(state_path).jobs[1].pod_id == "p-eval"
    err = capsys.readouterr().err
    assert "p-ghost" in err and "p-done" in err and "TERMINATING" in err


@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.registry_add")
def test_reconcile_duplicate_pod_keeps_tracked_one(mock_add, mock_upd, paths):
    from conductor.monitor import reconcile_run_pods
    state_path, cost_path = paths
    provider = _provider()
    provider.list_instances.return_value = [
        {"id": "p-extra", "name": "conductor-abc123-train", "alive": True},
    ]
    state = RunState(run_id="abc123", jobs=[PodState(name="train", status="running", pod_id="p-tracked")])
    cfgs = {"train": _make_config(name="train")}

    provider.check_exists.return_value = True
    reconcile_run_pods(cfgs, {"train": provider}, state, state_path, cost_path)
    provider.terminate.assert_called_once_with("p-extra")
    assert state.jobs[0].pod_id == "p-tracked"

    # Tracked pod confirmed gone: adopt the extra instead
    provider.terminate.reset_mock()
    provider.check_exists.return_value = False
    reconcile_run_pods(cfgs, {"train": provider}, state, state_path, cost_path)
    provider.terminate.assert_not_called()
    assert state.jobs[0].pod_id == "p-extra"
    assert state.jobs[0].status == "provisioning"


def test_reconcile_survives_list_failure(paths):
    from conductor.monitor import reconcile_run_pods
    state_path, cost_path = paths
    provider = _provider()
    provider.list_instances.side_effect = ConductorError("runpod.get_pods: 502")
    state = RunState(run_id="abc123", jobs=[PodState(name="a", status="running", pod_id="p")])
    reconcile_run_pods({"a": _make_config(name="a")}, {"a": provider}, state, state_path, cost_path)
    assert state.jobs[0].pod_id == "p"
    provider.terminate.assert_not_called()


def test_reconcile_noop_without_run_id(paths):
    from conductor.monitor import reconcile_run_pods
    provider = _provider()
    state = RunState(run_id="", jobs=[PodState(name="a", status="running", pod_id="p")])
    reconcile_run_pods({"a": _make_config(name="a")}, {"a": provider}, state, *paths)
    provider.list_instances.assert_not_called()


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.registry_add")
@patch("conductor.monitor.launch")
@patch("conductor.monitor.deploy", return_value=True)
@patch("conductor.monitor.wait_ssh", return_value=True)
@patch("conductor.monitor.provision_pod")
def test_tick_resumes_deploying_job_on_existing_pod(mock_prov, mock_wait, mock_deploy, mock_launch, mock_add, mock_upd, mock_start, mock_notify, paths):
    from conductor.runner import LaunchResult
    state_path, cost_path = paths
    provider = _provider()
    provider.check_exists.return_value = True
    provider.get_ssh_info.return_value = ("9.9.9.9", 4444)
    mock_launch.return_value = LaunchResult(pid=11, pid_start="5")
    job = PodState(name="a", status="deploying", pod_id="p-old", ssh_host="1.1.1.1", ssh_port=1)
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a")

    _monitor_tick([config], {"a": config}, {"a": provider}, state, state_path, cost_path)

    mock_prov.assert_not_called()            # no new pod
    provider.terminate.assert_not_called()   # old pod kept
    assert (job.ssh_host, job.ssh_port) == ("9.9.9.9", 4444)
    mock_deploy.assert_called_once()
    assert mock_deploy.call_args[1]["is_reuse"] is False  # setup re-run
    assert job.status == "running" and job.pid == 11 and job.pod_id == "p-old"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.registry_add")
@patch("conductor.monitor.launch")
@patch("conductor.monitor.deploy", return_value=True)
@patch("conductor.monitor.provision_pod")
def test_tick_reprovisions_when_persisted_pod_is_gone(mock_prov, mock_deploy, mock_launch, mock_add, mock_upd, mock_start, mock_notify, paths):
    from conductor.runner import LaunchResult
    state_path, cost_path = paths
    provider = _provider()
    provider.check_exists.return_value = False

    def _provision(provider, config, pod_state, run_id="", on_created=None, on_released=None):
        assert pod_state.pod_id is None
        pod_state.pod_id, pod_state.ssh_host, pod_state.ssh_port = "p-new", "h", 2
        return pod_state
    mock_prov.side_effect = _provision
    mock_launch.return_value = LaunchResult(pid=1)
    job = PodState(name="a", status="provisioning", pod_id="p-gone")
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a")

    _monitor_tick([config], {"a": config}, {"a": provider}, state, state_path, cost_path)

    provider.terminate.assert_not_called()  # nothing to terminate: it was gone
    assert job.pod_id == "p-new" and job.status == "running"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor._provision_deploy_launch")
def test_tick_waits_when_pod_existence_unknown(mock_pdl, mock_start, mock_notify, paths):
    provider = _provider()
    provider.check_exists.return_value = None
    job = PodState(name="a", status="provisioning", pod_id="p")
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a")
    _monitor_tick([config], {"a": config}, {"a": provider}, state, *paths)
    mock_pdl.assert_not_called()
    assert job.status == "provisioning" and job.pod_id == "p"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.launch")
@patch("conductor.monitor.is_alive")
def test_tick_launches_running_job_without_pid(mock_alive, mock_launch, mock_start, mock_notify, paths):
    from conductor.runner import LaunchResult, ProcessStatus
    mock_alive.return_value = ProcessStatus("unknown", detail="no pid recorded")
    mock_launch.return_value = LaunchResult(pid=42, pid_start="7")
    provider = _provider()
    job = PodState(name="a", status="running", pod_id="p", ssh_host="h", ssh_port=1, pid=None)
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a")
    _monitor_tick([config], {"a": config}, {"a": provider}, state, *paths)
    mock_launch.assert_called_once()
    assert job.pid == 42 and job.status == "running"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._teardown_all")
@patch("conductor.monitor._monitor_tick")
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.reconcile_run_pods")
def test_lifecycle_reconciles_on_start(mock_reconcile, mock_start, mock_tick, mock_teardown, mock_notify, paths):
    state_path, cost_path = paths
    state = RunState(jobs=[PodState(name="a", status="completed")], run_id="abc123")
    config = _make_config(name="a")
    with patch("conductor.monitor.get_provider", return_value=_provider()):
        run_lifecycle([config], state, state_path, cost_path)
    mock_reconcile.assert_called_once()
    assert mock_reconcile.call_args[0][2] is state
