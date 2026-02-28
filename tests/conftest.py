import os
import tempfile

import pytest


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def sample_toml(tmp_dir):
    path = os.path.join(tmp_dir, "jobs.toml")
    with open(path, "w") as f:
        f.write('''
[global]
gpu_type_id = "NVIDIA RTX A2000"
image_name = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
ssh_key_path = "~/.ssh/id_rsa"
remote_project_dir = "/workspace/project"
budget_usd = 40.00

[[jobs]]
name = "train"
run_command = "python train.py"

[[jobs]]
name = "eval"
depends_on = ["train"]
run_command = "python eval.py"
''')
    return path
