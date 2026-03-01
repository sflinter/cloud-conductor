import os
import pytest
from conductor.config import load_config, JobConfig, SyncPath


def test_basic_load(sample_toml):
    configs = load_config(sample_toml)
    assert len(configs) == 2
    assert configs[0].name == "train"
    assert configs[1].name == "eval"


def test_global_defaults_propagate(sample_toml):
    configs = load_config(sample_toml)
    for cfg in configs:
        assert cfg.gpu_type_id == "NVIDIA RTX A2000"
        assert cfg.budget_usd == 40.0
        assert cfg.image_name == "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"


def test_per_job_override(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('''
[global]
gpu_type_id = "NVIDIA RTX A2000"
container_disk_in_gb = 40

[[jobs]]
name = "a"
run_command = "echo hi"
container_disk_in_gb = 80
gpu_type_id = "NVIDIA RTX A4000"
''')
    configs = load_config(path)
    assert configs[0].container_disk_in_gb == 80
    assert configs[0].gpu_type_id == "NVIDIA RTX A4000"


def test_depends_on(sample_toml):
    configs = load_config(sample_toml)
    assert configs[0].depends_on == []
    assert configs[1].depends_on == ["train"]


def test_missing_name(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('[[jobs]]\nrun_command = "echo hi"\n')
    with pytest.raises(ValueError, match="name"):
        load_config(path)


def test_missing_run_command(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('[[jobs]]\nname = "a"\n')
    with pytest.raises(ValueError, match="run_command"):
        load_config(path)


def test_no_jobs(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('[global]\ngpu_type_id = "X"\n')
    with pytest.raises(ValueError, match="No \\[\\[jobs\\]\\]"):
        load_config(path)


def test_sync_paths_parsing(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('''
[global]
sync_paths = [
    { remote = "output/", local = "./results/" },
]

[[jobs]]
name = "a"
run_command = "echo hi"

[[jobs]]
name = "b"
run_command = "echo hi"
sync_paths = [
    { remote = "custom/", local = "./custom/" },
]
''')
    configs = load_config(path)
    assert len(configs[0].sync_paths) == 1
    assert configs[0].sync_paths[0].remote == "output/"
    assert configs[0].sync_paths[0].local == "./results/"
    # Per-job override replaces global
    assert len(configs[1].sync_paths) == 1
    assert configs[1].sync_paths[0].remote == "custom/"


def test_filter_job_names(sample_toml):
    configs = load_config(sample_toml, job_names=["train"])
    assert len(configs) == 1
    assert configs[0].name == "train"


def test_filter_unknown_job_name(sample_toml):
    with pytest.raises(ValueError, match="Unknown job names"):
        load_config(sample_toml, job_names=["nonexistent"])


def test_ssh_key_path_expanded(sample_toml):
    configs = load_config(sample_toml)
    assert configs[0].ssh_key_path == os.path.expanduser("~/.ssh/id_rsa")


def test_stall_fields_propagate(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('''
[global]
gpu_type_id = "NVIDIA RTX A2000"
stall_timeout_minutes = 10
stall_gpu_threshold = 3

[[jobs]]
name = "a"
run_command = "echo hi"

[[jobs]]
name = "b"
run_command = "echo hi"
stall_gpu_threshold = 8
''')
    configs = load_config(path)
    assert configs[0].stall_timeout_minutes == 10
    assert configs[0].stall_gpu_threshold == 3
    assert configs[1].stall_timeout_minutes == 10
    assert configs[1].stall_gpu_threshold == 8  # per-job override


def test_notifications_config(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('''
[global.notifications]
backend = "pushover"
on_job_complete = false
pushover_user_key = "abc"

[[jobs]]
name = "a"
run_command = "echo hi"
''')
    configs = load_config(path)
    assert configs[0].notifications is not None
    assert configs[0].notifications.backend == "pushover"
    assert configs[0].notifications.on_job_complete is False
    assert configs[0].notifications.pushover_user_key == "abc"
