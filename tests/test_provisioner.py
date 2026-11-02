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
    assert result.provision_attempts == 1  # one pass over the list, not one per candidate


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


def test_teardown_pod_verifies_with_provider():
    provider = _mock_provider()
    provider.terminate.return_value = True
    provider.check_exists.return_value = False
    assert teardown_pod(provider, "pod123") is True
    provider.terminate.assert_called_once_with("pod123")
    provider.check_exists.assert_called_with("pod123")


def test_teardown_pod_unverified_when_still_alive():
    provider = _mock_provider()
    provider.terminate.return_value = True
    provider.check_exists.return_value = True
    assert teardown_pod(provider, "pod123") is False
    provider.check_exists.return_value = None
    assert teardown_pod(provider, "pod123") is False
    assert teardown_pod(provider, "pod123", verify=False) is True


def test_teardown_pod_retries_terminate():
    provider = _mock_provider()
    provider.terminate.side_effect = [False, RuntimeError("502"), True]
    provider.check_exists.return_value = False
    assert teardown_pod(provider, "pod123") is True
    assert provider.terminate.call_count == 3

    provider.terminate.side_effect = None
    provider.terminate.return_value = False
    provider.terminate.reset_mock()
    assert teardown_pod(provider, "pod123") is False
    assert provider.terminate.call_count == 3


@patch("conductor.provisioner.select_cheapest_gpus")
def test_provision_catalog_error_fails_job(mock_select):
    from conductor.errors import ConductorError
    mock_select.side_effect = ConductorError("runpod.get_gpus: timeout")
    provider = _mock_provider()
    config = _make_config(auto_select_cheapest_gpu=True)
    pod = _make_pod()
    result = provision_pod(provider, config, pod)
    assert result.status == "failed"
    assert "GPU catalog" in result.error
    provider.create_instance.assert_not_called()


@patch("conductor.provisioner.check_pod_exists", return_value=True)
def test_pod_reuse_refreshes_ssh_info(mock_exists):
    provider = _mock_provider()
    provider.get_ssh_info.return_value = ("9.9.9.9", 44444)
    config = _make_config(keep_pod_alive=True)
    pod = _make_pod(pod_id="existing123", ssh_host="1.2.3.4", ssh_port=22222)
    result = provision_pod(provider, config, pod)
    assert result.pod_id == "existing123"
    assert (result.ssh_host, result.ssh_port) == ("9.9.9.9", 44444)
    provider.get_ssh_info.assert_called_once_with("existing123")


@patch("conductor.provisioner.wait_ssh", return_value=True)
@patch("conductor.provisioner.check_pod_exists", return_value=None)
def test_pod_reuse_unknown_existence_provisions_fresh(mock_exists, mock_ssh):
    # None (API error) must not be treated as "pod exists"
    provider = _mock_provider()
    provider.create_instance.return_value = {"id": "newpod", "cost_per_hour": 0.1}
    provider.wait_for_ssh.return_value = ("1.1.1.1", 1)
    config = _make_config(keep_pod_alive=True)
    pod = _make_pod(pod_id="old", ssh_host="1.2.3.4", ssh_port=22222)
    result = provision_pod(provider, config, pod)
    assert result.pod_id == "newpod"


def test_check_pod_exists_tri_state():
    provider = _mock_provider()
    provider.check_exists.return_value = None
    assert check_pod_exists(provider, "pod123") is None
    provider.check_exists.side_effect = RuntimeError("boom")
    assert check_pod_exists(provider, "pod123") is None


def test_refresh_ssh_info():
    from conductor.provisioner import refresh_ssh_info
    provider = _mock_provider()
    pod = _make_pod(pod_id="p", ssh_host="old", ssh_port=1)
    provider.get_ssh_info.return_value = None
    assert refresh_ssh_info(provider, pod) is False
    assert pod.ssh_host == "old"
    provider.get_ssh_info.side_effect = RuntimeError("api down")
    assert refresh_ssh_info(provider, pod) is False
    provider.get_ssh_info.side_effect = None
    provider.get_ssh_info.return_value = ("new", "2")
    assert refresh_ssh_info(provider, pod) is True
    assert (pod.ssh_host, pod.ssh_port) == ("new", 2)
    assert refresh_ssh_info(provider, _make_pod()) is False  # no pod_id


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_persists_pod_id_before_ssh_wait(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.return_value = {"id": "pod123", "cost_per_hour": 0.12}
    seen = []

    def _wait(instance_id, timeout=300):
        seen.append(("wait_for_ssh", list(persisted)))
        return ("1.2.3.4", 22222)
    provider.wait_for_ssh.side_effect = _wait
    persisted = []

    def on_created(pod_state):
        persisted.append((pod_state.pod_id, pod_state.gpu_type, pod_state.gpu_cost_per_hour, pod_state.status))

    config = _make_config()
    pod = _make_pod()
    result = provision_pod(provider, config, pod, run_id="abc123", on_created=on_created)
    assert result.pod_id == "pod123"
    # on_created ran with the pod id before the provider's SSH wait started
    assert seen[0][1] == [("pod123", "NVIDIA RTX A2000", 0.12, "provisioning")]
    assert provider.create_instance.call_args[1]["name"] == "conductor-abc123-test"


def test_provision_ssh_failure_releases_and_reports(mock_sleep=None):
    provider = _mock_provider()
    provider.create_instance.side_effect = [
        {"id": "dead1", "cost_per_hour": 0.1},
        {"id": "ok2", "cost_per_hour": 0.2},
    ]
    provider.wait_for_ssh.side_effect = [None, ("1.1.1.1", 1)]
    provider.terminate.return_value = True
    provider.check_exists.return_value = False
    created, released = [], []
    config = _make_config(gpu_type_ids_fallback=["NVIDIA RTX A4000"])
    pod = _make_pod()
    with patch("conductor.provisioner.wait_ssh", return_value=True):
        result = provision_pod(provider, config, pod,
                               on_created=lambda p: created.append(p.pod_id),
                               on_released=lambda pid, ok: released.append((pid, ok)))
    assert created == ["dead1", "ok2"]
    assert released == [("dead1", True)]
    provider.terminate.assert_called_once_with("dead1")
    assert result.pod_id == "ok2"
    assert result.status == "provisioning"


def test_provision_release_failure_is_reported():
    provider = _mock_provider()
    provider.create_instance.return_value = {"id": "stuck", "cost_per_hour": 0.1}
    provider.wait_for_ssh.return_value = None
    provider.terminate.return_value = False
    released = []
    result = provision_pod(provider, _make_config(), _make_pod(),
                           on_released=lambda pid, ok: released.append((pid, ok)))
    assert released == [("stuck", False)]
    assert result.status == "failed"
    assert result.pod_id is None  # no longer tracked by the job (registry keeps it)


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_timeout_is_configurable(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.return_value = {"id": "pod123", "cost_per_hour": 0.12}
    provider.wait_for_ssh.return_value = ("1.2.3.4", 22222)
    config = _make_config(provision_timeout_seconds=1234)
    provision_pod(provider, config, _make_pod())
    assert provider.wait_for_ssh.call_args[1]["timeout"] == 1234
    assert mock_ssh.call_args[1]["timeout"] == 1234


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_timeout_default(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.return_value = {"id": "pod123", "cost_per_hour": 0.12}
    provider.wait_for_ssh.return_value = ("1.2.3.4", 22222)
    provision_pod(provider, _make_config(), _make_pod())
    assert provider.wait_for_ssh.call_args[1]["timeout"] == 900
    assert mock_ssh.call_args[1]["timeout"] == 900


def test_provision_auth_error_fails_job_without_trying_next_gpu():
    from conductor.errors import SshAuthError
    provider = _mock_provider()
    provider.create_instance.return_value = {"id": "pod-key", "cost_per_hour": 0.1}
    provider.wait_for_ssh.return_value = ("1.2.3.4", 22222)
    provider.terminate.return_value = True
    provider.check_exists.return_value = False
    released = []
    config = _make_config(gpu_type_ids_fallback=["NVIDIA RTX A4000", "NVIDIA L4"])
    with patch("conductor.provisioner.wait_ssh",
               side_effect=SshAuthError("SSH authentication to 1.2.3.4:22222 failed 3 times in a row")):
        result = provision_pod(provider, config, _make_pod(),
                               on_released=lambda pid, ok: released.append((pid, ok)))
    assert result.status == "failed"
    assert "authentication" in result.error
    assert provider.create_instance.call_count == 1       # no further GPUs with the same key
    provider.terminate.assert_called_once_with("pod-key")  # the pod is not left billing
    assert released == [("pod-key", True)]
    assert result.pod_id is None


def _no_capacity(gpu):
    from conductor.errors import NoCapacityError
    return NoCapacityError(f"{gpu}: There are no longer any instances available")


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_retries_candidate_list_when_no_capacity(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.side_effect = [
        _no_capacity("A"), _no_capacity("B"),          # pass 1: nothing anywhere
        _no_capacity("A"), {"id": "pod-b", "cost_per_hour": 0.2},  # pass 2: B has capacity
    ]
    provider.wait_for_ssh.return_value = ("1.1.1.1", 1)
    waits = []
    config = _make_config(gpu_type_id="A", gpu_type_ids_fallback=["B"], provision_retries=3,
                          backoff_base_seconds=30)
    with patch("conductor.provisioner.shutdown.wait", side_effect=lambda s: waits.append(s) or False), \
         patch("conductor.provisioner.random.random", return_value=0.5):
        result = provision_pod(provider, config, _make_pod())
    assert result.pod_id == "pod-b" and result.gpu_type == "B"
    assert result.provision_attempts == 2          # two passes
    assert waits == [30.0]                         # base * 2**0 * (0.5 + 0.5), one backoff


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_gives_up_after_provision_retries_passes(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.side_effect = lambda gpu_type_id, **kw: (_ for _ in ()).throw(_no_capacity(gpu_type_id))
    waits = []
    config = _make_config(gpu_type_id="A", gpu_type_ids_fallback=["B", "C"], provision_retries=3,
                          backoff_base_seconds=10)
    with patch("conductor.provisioner.shutdown.wait", side_effect=lambda s: waits.append(s) or False), \
         patch("conductor.provisioner.random.random", return_value=0.0):
        result = provision_pod(provider, config, _make_pod())
    assert result.status == "failed" and "3 pass" in result.error
    assert provider.create_instance.call_count == 9
    assert result.provision_attempts == 3
    assert waits == [5.0, 10.0]                     # jittered exponential backoff between passes


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_does_not_retry_non_capacity_failures(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.return_value = None   # e.g. bad request
    config = _make_config(gpu_type_id="A", gpu_type_ids_fallback=["B"], provision_retries=3)
    with patch("conductor.provisioner.shutdown.wait") as mock_wait:
        result = provision_pod(provider, config, _make_pod())
    assert result.status == "failed"
    assert provider.create_instance.call_count == 2
    mock_wait.assert_not_called()
    assert result.provision_attempts == 1


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_backoff_is_interruptible(mock_ssh):
    from conductor import shutdown
    from conductor.shutdown import Interrupted
    provider = _mock_provider()
    provider.create_instance.side_effect = _no_capacity("A")
    config = _make_config(gpu_type_id="A", provision_retries=3, backoff_base_seconds=600)
    shutdown.request()
    import time as _t
    t0 = _t.time()
    with patch("conductor.provisioner.random.random", return_value=0.5):
        import pytest
        with pytest.raises(Interrupted):
            provision_pod(provider, config, _make_pod())
    assert _t.time() - t0 < 2
    assert provider.create_instance.call_count == 1


@patch("conductor.provisioner.wait_ssh", return_value=True)
def test_provision_retries_disabled_with_single_pass(mock_ssh):
    provider = _mock_provider()
    provider.create_instance.side_effect = _no_capacity("A")
    config = _make_config(gpu_type_id="A", provision_retries=1)
    with patch("conductor.provisioner.shutdown.wait") as mock_wait:
        result = provision_pod(provider, config, _make_pod())
    assert result.status == "failed" and result.error == "All GPU types exhausted"
    mock_wait.assert_not_called()
