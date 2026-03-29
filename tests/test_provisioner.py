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


def _mock_provider():
    p = MagicMock()
    p.name = "runpod"
    return p


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_success(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.return_value = {"id": "pod123", "cost_per_hour": 0.12}
    provider.wait_for_ssh.return_value = ("1.2.3.4", 22222)

    config = _make_config()
    pod = _make_pod()
    result = provision_pod(provider, config, pod)
    assert result.pod_id == "pod123"
    assert result.ssh_host == "1.2.3.4"
    assert result.ssh_port == 22222
    assert result.gpu_cost_per_hour == 0.12


def test_provision_all_fail():
    provider = _mock_provider()
    provider.create_instance.return_value = None

    config = _make_config()
    pod = _make_pod()
    result = provision_pod(provider, config, pod)
    assert result.status == "failed"
    assert "exhausted" in result.error


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_fallback(mock_ssh):
    provider = _mock_provider()
    # First GPU fails, second succeeds
    provider.create_instance.side_effect = [
        None,
        {"id": "pod456", "cost_per_hour": 0.25},
    ]
    provider.wait_for_ssh.return_value = ("5.6.7.8", 33333)

    config = _make_config(gpu_type_ids_fallback=["NVIDIA RTX A4000"])
    pod = _make_pod()
    result = provision_pod(provider, config, pod)
    assert result.pod_id == "pod456"
    assert result.provision_attempts == 2


@patch("conductor.provisioner.check_pod_exists", return_value=True)
def test_pod_reuse(mock_exists):
    provider = _mock_provider()
    config = _make_config(keep_pod_alive=True)
    pod = _make_pod(pod_id="existing123", ssh_host="1.2.3.4", ssh_port=22222)
    result = provision_pod(provider, config, pod)
    assert result.pod_id == "existing123"  # reused, not re-provisioned


def test_get_gpu_candidates_manual():
    provider = _mock_provider()
    config = _make_config(gpu_type_id="A", gpu_type_ids_fallback=["B", "C"])
    assert _get_gpu_candidates(provider, config) == ["A", "B", "C"]


@patch("conductor.provisioner.select_cheapest_gpus")
def test_get_gpu_candidates_auto(mock_select):
    provider = _mock_provider()
    mock_gpu = MagicMock()
    mock_gpu.id = "CHEAP_GPU"
    mock_select.return_value = [mock_gpu]
    config = _make_config(auto_select_cheapest_gpu=True)
    assert _get_gpu_candidates(provider, config) == ["CHEAP_GPU"]


def test_check_pod_exists():
    provider = _mock_provider()
    provider.check_exists.return_value = True
    assert check_pod_exists(provider, "pod123") is True

    provider.check_exists.return_value = False
    assert check_pod_exists(provider, "pod123") is False


def test_teardown_pod():
    provider = _mock_provider()
    provider.terminate.return_value = True
    assert teardown_pod(provider, "pod123") is True
    provider.terminate.assert_called_once_with("pod123")
