from unittest.mock import patch, MagicMock

from conductor.config import JobConfig
from conductor.provisioner import provision_pod, check_pod_exists, teardown_pod, _get_gpu_candidates
from conductor.state import PodState


def _make_config(**overrides):
    defaults = dict(name="test", run_command="echo hi", gpu_type_id="NVIDIA RTX A2000",
                    ssh_key_path="/key", image_name="img", cloud_type="ALL")
    defaults.update(overrides)
    return JobConfig(**defaults)


def _make_pod(**overrides):
    defaults = dict(name="test")
    defaults.update(overrides)
    return PodState(**defaults)


@patch("conductor.provisioner.wait_ssh", return_value=True)
@patch("conductor.provisioner.get_gpu_price", return_value=0.12)
@patch("conductor.provisioner.runpod")
def test_provision_success(mock_runpod, mock_price, mock_ssh):
    mock_runpod.create_pod.return_value = {"id": "pod123"}
    mock_runpod.get_pod.return_value = {
        "runtime": {"ports": [{"privatePort": 22, "ip": "1.2.3.4", "publicPort": 22222}]},
        "desiredStatus": "RUNNING",
    }
    config = _make_config()
    pod = _make_pod()
    result = provision_pod(config, pod)
    assert result.pod_id == "pod123"
    assert result.ssh_host == "1.2.3.4"
    assert result.ssh_port == 22222
    assert result.gpu_cost_per_hour == 0.12


@patch("conductor.provisioner.runpod")
def test_provision_all_fail(mock_runpod):
    mock_runpod.create_pod.side_effect = Exception("no capacity")
    config = _make_config()
    pod = _make_pod()
    result = provision_pod(config, pod)
    assert result.status == "failed"
    assert "exhausted" in result.error


@patch("conductor.provisioner.wait_ssh", return_value=True)
@patch("conductor.provisioner.get_gpu_price", return_value=0.25)
@patch("conductor.provisioner.runpod")
def test_provision_fallback(mock_runpod, mock_price, mock_ssh):
    # First GPU fails, second succeeds
    mock_runpod.create_pod.side_effect = [
        Exception("no capacity"),
        {"id": "pod456"},
    ]
    mock_runpod.get_pod.return_value = {
        "runtime": {"ports": [{"privatePort": 22, "ip": "5.6.7.8", "publicPort": 33333}]},
    }
    config = _make_config(gpu_type_ids_fallback=["NVIDIA RTX A4000"])
    pod = _make_pod()
    result = provision_pod(config, pod)
    assert result.pod_id == "pod456"
    assert result.provision_attempts == 2


@patch("conductor.provisioner.check_pod_exists", return_value=True)
def test_pod_reuse(mock_exists):
    config = _make_config(keep_pod_alive=True)
    pod = _make_pod(pod_id="existing123", ssh_host="1.2.3.4", ssh_port=22222)
    result = provision_pod(config, pod)
    assert result.pod_id == "existing123"  # reused, not re-provisioned


def test_get_gpu_candidates_manual():
    config = _make_config(gpu_type_id="A", gpu_type_ids_fallback=["B", "C"])
    assert _get_gpu_candidates(config) == ["A", "B", "C"]


@patch("conductor.provisioner.select_cheapest_gpus")
def test_get_gpu_candidates_auto(mock_select):
    mock_gpu = MagicMock()
    mock_gpu.id = "CHEAP_GPU"
    mock_select.return_value = [mock_gpu]
    config = _make_config(auto_select_cheapest_gpu=True)
    assert _get_gpu_candidates(config) == ["CHEAP_GPU"]


@patch("conductor.provisioner.runpod")
def test_check_pod_exists(mock_runpod):
    mock_runpod.get_pod.return_value = {"desiredStatus": "RUNNING"}
    assert check_pod_exists("pod123") is True

    mock_runpod.get_pod.return_value = {"desiredStatus": "EXITED"}
    assert check_pod_exists("pod123") is False


@patch("conductor.provisioner.runpod")
def test_teardown_pod(mock_runpod):
    assert teardown_pod("pod123") is True
    mock_runpod.terminate_pod.assert_called_once_with("pod123")
