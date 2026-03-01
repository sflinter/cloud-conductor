import os
import json
import tempfile

import pytest

from conductor.cli import main


def test_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "conductor" in captured.out
    for cmd in ["run", "status", "sync", "teardown", "dry-run", "validate", "logs", "ssh", "report"]:
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
