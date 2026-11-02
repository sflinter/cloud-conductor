import os
import json
import tempfile

import pytest

from conductor.cli import main, _resolve_config, _STARTER_TOML


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "conductor" in captured.out
    for cmd in ["run", "status", "sync", "teardown", "dry-run", "validate",
                "logs", "ssh", "report", "version", "init", "completions", "attach"]:
        assert cmd in captured.out


def test_no_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 1


def test_validate_command(capsys, tmp_dir):
    key_path = os.path.join(tmp_dir, "test_key")
    with open(key_path, "w") as f:
        f.write("fake key")
    os.chmod(key_path, 0o600)

    config_path = os.path.join(tmp_dir, "jobs.toml")
    with open(config_path, "w") as f:
        f.write(f'''
[global]
gpu_type_id = "NVIDIA RTX A2000"
ssh_key_path = "{key_path}"
budget_usd = 40.0

[[jobs]]
name = "test"
run_command = "echo hi"
''')

    main(["validate", "--config", config_path])
    captured = capsys.readouterr()
    assert "Config OK" in captured.out


def test_validate_bad_config(capsys, tmp_dir):
    config_path = os.path.join(tmp_dir, "jobs.toml")
    with open(config_path, "w") as f:
        f.write('''
[[jobs]]
name = "test"
run_command = "echo hi"
ssh_key_path = "/no/such/key"
''')

    with pytest.raises(SystemExit) as exc:
        main(["validate", "--config", config_path])
    assert exc.value.code == 1


def test_status_no_state(capsys, tmp_dir):
    config_path = os.path.join(tmp_dir, "jobs.toml")
    with open(config_path, "w") as f:
        f.write('''
[[jobs]]
name = "test"
run_command = "echo hi"
state_file = "{}/state.json"
'''.format(tmp_dir))

    main(["status", "--config", config_path])
    captured = capsys.readouterr()
    assert "No state file" in captured.out


def test_report_no_data(capsys, tmp_dir):
    config_path = os.path.join(tmp_dir, "jobs.toml")
    with open(config_path, "w") as f:
        f.write(f'''
[[jobs]]
name = "test"
run_command = "echo hi"
cost_log_file = "{tmp_dir}/cost.jsonl"
''')

    main(["report", "--config", config_path])
    captured = capsys.readouterr()
    assert "No cost data" in captured.out


def test_dry_run(capsys, tmp_dir):
    config_path = os.path.join(tmp_dir, "jobs.toml")
    with open(config_path, "w") as f:
        f.write('''
[global]
gpu_type_id = "NVIDIA RTX A2000"
budget_usd = 40.0

[[jobs]]
name = "train"
run_command = "echo train"

[[jobs]]
name = "eval"
depends_on = ["train"]
run_command = "echo eval"
''')

    main(["dry-run", "--config", config_path])
    captured = capsys.readouterr()
    assert "Dry Run" in captured.out
    assert "train" in captured.out
    assert "eval" in captured.out
    assert "Depends on: train" in captured.out


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "conductor" in captured.out


def test_version_subcommand(capsys):
    main(["version"])
    captured = capsys.readouterr()
    assert "conductor" in captured.out


def test_init_creates_file(capsys, tmp_dir, monkeypatch):
    monkeypatch.chdir(tmp_dir)
    main(["init"])
    captured = capsys.readouterr()
    assert "Created conductor.toml" in captured.out
    assert os.path.exists(os.path.join(tmp_dir, "conductor.toml"))


def test_init_refuses_overwrite(capsys, tmp_dir, monkeypatch):
    monkeypatch.chdir(tmp_dir)
    with open(os.path.join(tmp_dir, "conductor.toml"), "w") as f:
        f.write("existing")
    with pytest.raises(SystemExit) as exc:
        main(["init"])
    assert exc.value.code == 1


def test_init_force_overwrite(capsys, tmp_dir, monkeypatch):
    monkeypatch.chdir(tmp_dir)
    with open(os.path.join(tmp_dir, "conductor.toml"), "w") as f:
        f.write("existing")
    main(["init", "--force"])
    with open(os.path.join(tmp_dir, "conductor.toml")) as f:
        assert "[[jobs]]" in f.read()


def test_init_generates_valid_config(capsys, tmp_dir, monkeypatch):
    monkeypatch.chdir(tmp_dir)
    main(["init"])
    main(["validate", "--config", os.path.join(tmp_dir, "conductor.toml")])
    captured = capsys.readouterr()
    assert "Config OK" in captured.out


def test_completions_bash(capsys):
    main(["completions", "bash"])
    captured = capsys.readouterr()
    assert "complete" in captured.out or "COMPREPLY" in captured.out


def test_config_auto_discovery(tmp_dir, monkeypatch):
    monkeypatch.chdir(tmp_dir)
    import argparse
    args = argparse.Namespace(config=None)
    assert _resolve_config(args) == "jobs.toml"

    with open(os.path.join(tmp_dir, "conductor.toml"), "w") as f:
        f.write("x")
    assert _resolve_config(args) == "conductor.toml"

    args.config = "custom.toml"
    assert _resolve_config(args) == "custom.toml"


def test_status_shows_exit_code_and_first_error_line(capsys, tmp_dir, monkeypatch):
    from conductor.state import PodState, RunState, save_state
    state_path = os.path.join(tmp_dir, "state.json")
    config_path = os.path.join(tmp_dir, "jobs.toml")
    with open(config_path, "w") as f:
        f.write(f'''
[[jobs]]
name = "train"
run_command = "echo hi"
state_file = "{state_path}"
''')
    save_state(RunState(jobs=[
        PodState(name="train", status="failed", exit_code=2,
                 error="exit code 2\n--- last 50 log lines ---\nTraceback"),
    ]), state_path)
    monkeypatch.setattr("conductor.cli.reconcile_with_provider", lambda: [])

    main(["status", "--config", config_path])
    out = capsys.readouterr().out
    assert "Exit" in out
    row = [l for l in out.splitlines() if l.startswith("train")][0]
    assert " 2 " in row
    assert "exit code 2" in row
    assert "Traceback" not in row


def _write_run_fixture(tmp_dir, jobs_state):
    from conductor.state import RunState, save_state
    state_path = os.path.join(tmp_dir, "state.json")
    config_path = os.path.join(tmp_dir, "jobs.toml")
    key_path = os.path.join(tmp_dir, "key")
    with open(key_path, "w") as f:
        f.write("k")
    os.chmod(key_path, 0o600)
    with open(config_path, "w") as f:
        f.write(f'''
[global]
ssh_key_path = "{key_path}"
state_file = "{state_path}"
cost_log_file = "{tmp_dir}/cost.jsonl"
local_project_dir = "{tmp_dir}"

[[jobs]]
name = "train"
run_command = "echo hi"

[[jobs]]
name = "eval"
depends_on = ["train"]
run_command = "echo eval"
''')
    save_state(RunState(jobs=jobs_state, run_id="abc123"), state_path)
    return config_path, state_path


def test_run_retry_failed_resets_jobs(tmp_dir, monkeypatch, capsys):
    from unittest.mock import MagicMock
    from conductor.state import PodState, load_state
    config_path, state_path = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="failed", pod_id=None, error="exit code 1", exit_code=1),
        PodState(name="eval", status="skipped", error="dependency 'train' failed"),
    ])
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    captured = {}

    def fake_lifecycle(configs, state, state_path, cost_log_path, budget_override=0.0, **kwargs):
        captured["state"] = state
        return state
    monkeypatch.setattr("conductor.cli.run_lifecycle", fake_lifecycle)

    main(["run", "--config", config_path, "--retry-failed"])
    state = captured["state"]
    assert state.run_id == "abc123"
    assert [j.status for j in state.jobs] == ["pending", "pending"]
    assert state.jobs[0].error is None and state.jobs[0].exit_code is None
    assert "Re-running train" in capsys.readouterr().out


def test_run_without_retry_leaves_finished_jobs(tmp_dir, monkeypatch):
    from unittest.mock import MagicMock
    from conductor.state import PodState
    config_path, state_path = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="completed"),
        PodState(name="eval", status="failed", error="boom"),
    ])
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    captured = {}
    monkeypatch.setattr("conductor.cli.run_lifecycle",
                        lambda configs, state, *a, **k: captured.setdefault("state", state))
    main(["run", "--config", config_path])
    assert [j.status for j in captured["state"].jobs] == ["completed", "failed"]


def test_run_retry_completed_keeps_alive_pod(tmp_dir, monkeypatch):
    from unittest.mock import MagicMock
    from conductor.state import PodState
    config_path, state_path = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="completed", pod_id="kept", ssh_host="h", ssh_port=1, pid=3),
        PodState(name="eval", status="completed"),
    ])
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    captured = {}
    monkeypatch.setattr("conductor.cli.run_lifecycle",
                        lambda configs, state, *a, **k: captured.setdefault("state", state))
    main(["run", "--config", config_path, "--retry-completed"])
    train = captured["state"].jobs[0]
    assert train.status == "pending"
    assert train.pod_id == "kept"  # pipeline terminates or reuses it, never loses it
    assert train.pid is None


def test_run_assigns_run_id_to_old_state(tmp_dir, monkeypatch):
    from unittest.mock import MagicMock
    from conductor.state import PodState
    config_path, state_path = _write_run_fixture(tmp_dir, [PodState(name="train"), PodState(name="eval")])
    with open(state_path) as f:
        data = json.load(f)
    del data["run_id"]
    with open(state_path, "w") as f:
        json.dump(data, f)
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    captured = {}
    monkeypatch.setattr("conductor.cli.run_lifecycle",
                        lambda configs, state, *a, **k: captured.setdefault("state", state))
    main(["run", "--config", config_path])
    assert len(captured["state"].run_id) == 6


def _live_fixture(tmp_dir, fresh=True):
    import time
    from conductor.state import PodState, RunState, load_state, save_state
    config_path, state_path = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="running", pod_id="p1", ssh_host="h", ssh_port=1, pid=1),
        PodState(name="eval", status="pending"),
    ])
    state = load_state(state_path)
    state.orchestrator_pid = 4242
    state.heartbeat_at = time.time() - (5 if fresh else 600)
    save_state(state, state_path)
    return config_path, state_path


def test_run_refuses_when_live(tmp_dir, monkeypatch, capsys):
    from unittest.mock import MagicMock
    config_path, _ = _live_fixture(tmp_dir)
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    lifecycle = MagicMock()
    monkeypatch.setattr("conductor.cli.run_lifecycle", lifecycle)
    with pytest.raises(SystemExit) as exc:
        main(["run", "--config", config_path])
    assert exc.value.code == 1
    assert "live (pid 4242" in capsys.readouterr().err
    lifecycle.assert_not_called()

    main(["run", "--config", config_path, "--force"])
    lifecycle.assert_called_once()


def test_run_proceeds_when_heartbeat_stale(tmp_dir, monkeypatch):
    from unittest.mock import MagicMock
    config_path, _ = _live_fixture(tmp_dir, fresh=False)
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    lifecycle = MagicMock()
    monkeypatch.setattr("conductor.cli.run_lifecycle", lifecycle)
    main(["run", "--config", config_path, "--teardown-on-interrupt"])
    assert lifecycle.call_args[1]["teardown_on_interrupt"] is True


def test_teardown_refuses_when_live(tmp_dir, monkeypatch, capsys):
    from unittest.mock import MagicMock
    config_path, state_path = _live_fixture(tmp_dir)
    provider = MagicMock()
    provider.terminate.return_value = True
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: provider)
    with pytest.raises(SystemExit) as exc:
        main(["teardown", "--config", config_path])
    assert exc.value.code == 1
    assert "Refusing to teardown" in capsys.readouterr().err
    provider.terminate.assert_not_called()

    main(["teardown", "--config", config_path, "--force"])
    provider.terminate.assert_called_once_with("p1")


def test_sync_refuses_when_live(tmp_dir, monkeypatch, capsys):
    from unittest.mock import MagicMock
    config_path, state_path = _live_fixture(tmp_dir)
    pull = MagicMock(return_value=True)
    monkeypatch.setattr("conductor.cli.sync_pull", pull)
    with pytest.raises(SystemExit):
        main(["sync", "--config", config_path])
    assert "Refusing to sync" in capsys.readouterr().err
    pull.assert_not_called()

    main(["sync", "--config", config_path, "--force"])
    pull.assert_called_once()
    # The live run's state file was not overwritten
    from conductor.state import load_state
    assert load_state(state_path).orchestrator_pid == 4242


def test_status_shows_orchestrator_liveness(tmp_dir, monkeypatch, capsys):
    from unittest.mock import MagicMock
    config_path, _ = _live_fixture(tmp_dir)
    monkeypatch.setattr("conductor.cli.reconcile_with_provider", lambda: [])
    provider = MagicMock()
    provider.check_exists.return_value = True
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: provider)
    main(["status", "--config", config_path])
    out = capsys.readouterr().out
    assert "Run abc123: orchestrator live (pid 4242" in out


def test_sync_covers_sync_failed_jobs(tmp_dir, monkeypatch, capsys):
    from unittest.mock import MagicMock
    from conductor.state import PodState
    config_path, state_path = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="sync_failed", pod_id="p1", ssh_host="h", ssh_port=1),
        PodState(name="eval", status="completed"),
    ])
    pull = MagicMock(return_value=True)
    monkeypatch.setattr("conductor.cli.sync_pull", pull)
    main(["sync", "--config", config_path])
    assert pull.call_count == 1
    out = capsys.readouterr().out
    assert "conductor teardown --jobs train" in out


def test_validate_fails_fast_on_passphrase_key(capsys, tmp_dir, monkeypatch):
    import subprocess
    key_path = os.path.join(tmp_dir, "enc_key")
    with open(key_path, "w") as f:
        f.write("encrypted")
    os.chmod(key_path, 0o600)
    config_path = os.path.join(tmp_dir, "jobs.toml")
    with open(config_path, "w") as f:
        f.write(f'''
[[jobs]]
name = "test"
run_command = "echo hi"
ssh_key_path = "{key_path}"
local_project_dir = "{tmp_dir}"
''')

    def run(cmd, **kwargs):
        if cmd[0] == "ssh-keygen" and "-y" in cmd:
            return subprocess.CompletedProcess(cmd, 255, stdout="", stderr="incorrect passphrase")
        if cmd[0] == "ssh-keygen":
            return subprocess.CompletedProcess(cmd, 0, stdout="3072 SHA256:zzz c (RSA)\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="The agent has no identities.\n", stderr="")
    monkeypatch.setattr("conductor.validator.subprocess.run", run)

    with pytest.raises(SystemExit) as exc:
        main(["validate", "--config", config_path])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"ssh-add --apple-use-keychain {key_path}" in out

    # `conductor run` refuses before any provider is configured or pod created
    from unittest.mock import MagicMock
    lifecycle = MagicMock()
    monkeypatch.setattr("conductor.cli.run_lifecycle", lifecycle)
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    with pytest.raises(SystemExit):
        main(["run", "--config", config_path])
    lifecycle.assert_not_called()


def test_teardown_records_outcome_and_cost(tmp_dir, monkeypatch, capsys):
    import time
    from unittest.mock import MagicMock
    from conductor.state import PodState, load_state, read_cost_log
    config_path, state_path = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="provisioning", pod_id="p1", gpu_type="RTX 2000 Ada",
                 gpu_cost_per_hour=0.24, started_at=time.time() - 3600),
        PodState(name="eval", status="pending"),
    ])
    cost_path = os.path.join(tmp_dir, "cost.jsonl")
    with open(cost_path, "w") as f:
        f.write(json.dumps({"ts": time.time() - 3600, "event": "pod_started", "job": "train",
                            "pod_id": "p1", "gpu_type": "RTX 2000 Ada", "cost_per_hour": 0.24}) + "\n")
    provider = MagicMock()
    provider.terminate.return_value = True
    provider.check_exists.return_value = False
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: provider)
    registry = MagicMock()
    monkeypatch.setattr("conductor.cli.update_pod", registry)

    main(["teardown", "--config", config_path, "--force"])

    provider.terminate.assert_called_once_with("p1")
    registry.assert_called_once()
    assert registry.call_args[0][0] == "p1"
    assert registry.call_args[1]["status"] == "terminated"
    assert registry.call_args[1]["cost_usd"] > 0.2

    saved = load_state(state_path)
    train = saved.jobs[0]
    assert train.status == "failed" and train.error == "torn down manually"
    assert 0.2 < train.cost_usd < 0.3 and saved.total_cost_usd == train.cost_usd
    assert saved.jobs[1].status == "pending"  # never had a pod: untouched

    events = read_cost_log(cost_path)
    stop = [e for e in events if e["event"] == "pod_stopped"]
    assert len(stop) == 1
    assert stop[0]["reason"] == "manual_teardown" and stop[0]["pod_id"] == "p1"
    assert stop[0]["verified"] is True and stop[0]["total_cost_usd"] == train.cost_usd
    assert stop[0]["gpu_type"] == "RTX 2000 Ada"

    main(["report", "--config", config_path])
    out = capsys.readouterr().out
    row = [l for l in out.splitlines() if l.startswith("train")][0]
    assert "manual_teardown" in row and "running" not in row


def test_teardown_unverified_is_recorded_as_such(tmp_dir, monkeypatch, capsys):
    from unittest.mock import MagicMock
    from conductor.state import PodState, load_state, read_cost_log
    config_path, state_path = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="running", pod_id="p1"),
        PodState(name="eval", status="pending"),
    ])
    provider = MagicMock()
    provider.terminate.return_value = False
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: provider)
    registry = MagicMock()
    monkeypatch.setattr("conductor.cli.update_pod", registry)

    main(["teardown", "--config", config_path, "--force"])

    assert registry.call_args[1]["status"] == "running"
    train = load_state(state_path).jobs[0]
    assert train.status == "failed" and "unverified" in train.error
    assert read_cost_log(os.path.join(tmp_dir, "cost.jsonl"))[0]["verified"] is False
    assert "may still be billing" in capsys.readouterr().out


def test_status_accrues_cost_for_provisioning_pod(tmp_dir, monkeypatch, capsys):
    import time
    from unittest.mock import MagicMock
    from conductor.state import PodState
    config_path, _ = _write_run_fixture(tmp_dir, [
        PodState(name="train", status="provisioning", pod_id="p1", gpu_cost_per_hour=0.5,
                 started_at=time.time() - 3600),
        PodState(name="eval", status="pending"),
    ])
    monkeypatch.setattr("conductor.cli.reconcile_with_provider", lambda: [])
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    main(["status", "--config", config_path])
    out = capsys.readouterr().out
    row = [l for l in out.splitlines() if l.startswith("train")][0]
    assert "$0.50" in row
