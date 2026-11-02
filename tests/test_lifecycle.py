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


# --- ownership, detach, interrupt ------------------------------------------

def test_sigint_handler_detaches_then_hard_exits(capsys):
    from conductor.monitor import make_sigint_handler
    state = RunState(jobs=[PodState(name="a", status="running", pod_id="pod-live")])
    shutdown = {"requested": False, "teardown": False}
    exits = []
    handler = make_sigint_handler(shutdown, state, hard_exit=exits.append)

    handler(2, None)
    assert shutdown["requested"] is True
    assert exits == []
    assert state.jobs[0].status == "running"

    handler(2, None)
    assert exits == [130]
    err = capsys.readouterr().err
    assert "pod-live" in err and "STILL RUNNING" in err


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._graceful_shutdown")
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.reconcile_run_pods")
def test_lifecycle_detach_leaves_pods_and_clears_heartbeat(mock_rec, mock_start, mock_graceful, mock_notify, paths, capsys):
    state_path, cost_path = paths
    job = PodState(name="a", status="running", pod_id="pod-live", ssh_host="h", ssh_port=1, pid=1)
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a")
    provider = _provider()
    seen = {}

    def tick(configs, config_map, provider_map, st, sp, cp):
        seen["heartbeat"] = load_state(sp).heartbeat_at  # written before the first tick
        seen["pid"] = load_state(sp).orchestrator_pid
        # simulate Ctrl-C during the tick
        import signal
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    with patch("conductor.monitor._monitor_tick", side_effect=tick), \
         patch("conductor.monitor.get_provider", return_value=provider):
        run_lifecycle([config], state, state_path, cost_path)

    assert seen["heartbeat"] is not None and seen["pid"] == os.getpid()
    assert job.status == "running"
    provider.terminate.assert_not_called()
    mock_graceful.assert_not_called()
    saved = load_state(state_path)
    assert saved.heartbeat_at is None and saved.orchestrator_pid is None
    assert saved.jobs[0].pod_id == "pod-live"
    err = capsys.readouterr().err
    assert "Detached" in err and "pod-live" in err


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.sync_pull", return_value=True)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.reconcile_run_pods")
def test_lifecycle_teardown_on_interrupt(mock_rec, mock_start, mock_reg, mock_sync, mock_notify, paths):
    state_path, cost_path = paths
    job = PodState(name="a", status="running", pod_id="pod-live", ssh_host="h", ssh_port=1, pid=1,
                   started_at=time.time())
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a")
    provider = _provider()

    def tick(*a):
        import signal
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    with patch("conductor.monitor._monitor_tick", side_effect=tick), \
         patch("conductor.monitor.get_provider", return_value=provider):
        run_lifecycle([config], state, state_path, cost_path, teardown_on_interrupt=True)

    provider.terminate.assert_called_once_with("pod-live")
    assert job.status == "failed" and job.error == "interrupted"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.sync_pull", return_value=True)
@patch("conductor.monitor.registry_update")
def test_graceful_shutdown_covers_provisioning_and_deploying(mock_reg, mock_sync, mock_notify, paths):
    from conductor.monitor import _graceful_shutdown
    state_path, cost_path = paths
    provider = _provider()
    state = RunState(jobs=[
        PodState(name="prov", status="provisioning", pod_id="p1"),
        PodState(name="dep", status="deploying", pod_id="p2", ssh_host="h", ssh_port=1),
        PodState(name="run", status="running", pod_id="p3", ssh_host="h", ssh_port=1, pid=1),
        PodState(name="nopod", status="provisioning"),
        PodState(name="done", status="completed", pod_id="p4"),
    ])
    configs = {j.name: _make_config(name=j.name) for j in state.jobs}
    providers = {n: provider for n in configs}
    _graceful_shutdown(list(configs.values()), configs, providers, state, state_path, cost_path)
    assert sorted(c[0][0] for c in provider.terminate.call_args_list) == ["p1", "p2", "p3"]
    assert [j.status for j in state.jobs] == ["failed", "failed", "failed", "failed", "completed"]
    mock_sync.assert_called_once()  # only the running job had results to pull


def test_sleep_unless_returns_early():
    from conductor.monitor import _sleep_unless
    shutdown = {"requested": True, "teardown": False}
    t0 = time.time()
    _sleep_unless(shutdown, 30)
    assert time.time() - t0 < 1


# --- final sync and verified teardown ----------------------------------------

def _sync_config(**kw):
    from conductor.config import SyncPath
    return _make_config(sync_paths=[SyncPath(remote="out/", local="./r/")], **kw)


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.sync_pull", return_value=False)
def test_finish_job_keeps_pod_when_final_sync_fails(mock_sync, mock_reg, mock_notify, paths):
    from conductor.monitor import _finish_job, _all_done
    state_path, cost_path = paths
    provider = _provider()
    job = PodState(name="a", status="running", pod_id="p", ssh_host="h", ssh_port=1, exit_code=0)
    state = RunState(jobs=[job, PodState(name="b", status="pending", depends_on=["a"])])
    config = _sync_config(name="a")

    _finish_job(provider, job, config, state, cost_path, "completed")

    assert mock_sync.call_count == 3
    provider.terminate.assert_not_called()
    assert job.status == "sync_failed"
    assert "conductor sync --jobs a" in job.error and "completed" in job.error
    assert mock_reg.call_args[1]["status"] == "running"
    assert mock_notify.call_args[0][1] == "sync_failed"
    assert state.jobs[1].status == "pending"  # not propagated as a failure
    assert _all_done(RunState(jobs=[job])) is True
    assert job in __import__("conductor.monitor", fromlist=["live_pods"]).live_pods(state)


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.sync_pull", side_effect=[False, True])
def test_finish_job_retries_final_sync(mock_sync, mock_reg, mock_notify, paths):
    from conductor.monitor import _finish_job
    state_path, cost_path = paths
    provider = _provider()
    provider.check_exists.return_value = False
    job = PodState(name="a", status="running", pod_id="p", ssh_host="h", ssh_port=1)
    state = RunState(jobs=[job])

    _finish_job(provider, job, _sync_config(name="a"), state, cost_path, "completed")

    assert mock_sync.call_count == 2
    provider.terminate.assert_called_once_with("p")
    assert job.status == "completed" and job.error is None
    assert mock_reg.call_args[1]["status"] == "terminated"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.sync_pull", return_value=False)
def test_finish_job_force_teardown_ignores_sync_failure(mock_sync, mock_reg, mock_notify, paths):
    from conductor.monitor import _finish_job
    state_path, cost_path = paths
    provider = _provider()
    provider.check_exists.return_value = False
    job = PodState(name="a", status="running", pod_id="p", ssh_host="h", ssh_port=1)
    state = RunState(jobs=[job])

    _finish_job(provider, job, _sync_config(name="a"), state, cost_path, "failed",
                error="budget_exceeded", force_teardown=True)

    provider.terminate.assert_called_once_with("p")
    assert job.status == "failed"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.sync_pull", return_value=True)
def test_finish_job_records_unverified_teardown(mock_sync, mock_reg, mock_notify, paths):
    from conductor.monitor import _finish_job
    state_path, cost_path = paths
    provider = _provider()
    provider.terminate.return_value = True
    provider.check_exists.return_value = True  # still alive after terminate
    job = PodState(name="a", status="running", pod_id="p", ssh_host="h", ssh_port=1)
    state = RunState(jobs=[job])

    _finish_job(provider, job, _make_config(name="a"), state, cost_path, "completed")

    assert job.status == "completed"
    assert "teardown unverified" in job.error
    assert mock_reg.call_args[1]["status"] == "running"  # never claim a pod is gone


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.sync_pull", return_value=False)
def test_budget_stop_tears_down_sync_failed_pods(mock_sync, mock_reg, mock_notify, paths):
    from conductor.monitor import _teardown_all
    state_path, cost_path = paths
    provider = _provider()
    provider.check_exists.return_value = False
    state = RunState(jobs=[PodState(name="a", status="sync_failed", pod_id="p", ssh_host="h", ssh_port=1)])
    cfg = {"a": _sync_config(name="a")}
    _teardown_all(list(cfg.values()), cfg, {"a": provider}, state, state_path, cost_path, reason="budget_exceeded")
    provider.terminate.assert_called_once_with("p")
    assert state.jobs[0].status == "failed"


@patch("conductor.monitor.send_notification", return_value=False)
@patch("conductor.monitor._start_unblocked_jobs")
@patch("conductor.monitor.registry_update")
@patch("conductor.monitor.registry_add")
@patch("conductor.monitor.launch")
@patch("conductor.monitor.deploy", return_value=True)
@patch("conductor.monitor.wait_ssh", return_value=True)
def test_resume_uses_provision_timeout(mock_wait, mock_deploy, mock_launch, mock_add, mock_upd, mock_start, mock_notify, paths):
    from conductor.runner import LaunchResult
    mock_launch.return_value = LaunchResult(pid=1)
    provider = _provider()
    provider.get_ssh_info.return_value = ("h", 1)
    job = PodState(name="a", status="deploying", pod_id="p")
    state = RunState(jobs=[job], run_id="abc123")
    config = _make_config(name="a", provision_timeout_seconds=333)
    _monitor_tick([config], {"a": config}, {"a": provider}, state, *paths)
    assert mock_wait.call_args[1]["timeout"] == 333
