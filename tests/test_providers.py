from unittest.mock import patch, MagicMock

import pytest

from conductor.errors import ConductorError
from conductor.providers.runpod import RunPodProvider


@patch("conductor.providers.runpod.runpod")
def test_runpod_get_cost_falls_back_to_catalog_price(mock_runpod):
    mock_runpod.get_pod.return_value = {"costPerHr": 0}
    provider = RunPodProvider()
    with patch("conductor.gpu_pricing.get_gpu_price", return_value=0.42) as mock_price:
        assert provider._get_cost("pod1", "NVIDIA RTX A2000", "ALL") == 0.42
        # Must be called with the provider, not with the GPU id in its place
        assert mock_price.call_args[0][0] is provider


@patch("conductor.providers.runpod.runpod")
def test_runpod_get_cost_never_raises(mock_runpod):
    mock_runpod.get_pod.side_effect = RuntimeError("api down")
    provider = RunPodProvider()
    with patch("conductor.gpu_pricing.get_gpu_price", side_effect=RuntimeError("also down")):
        assert provider._get_cost("pod1", "gpu", "ALL") == 0.0


@patch("conductor.providers.runpod.runpod")
def test_runpod_catalog_error_is_conductor_error(mock_runpod):
    from runpod import error as runpod_error
    mock_runpod.get_gpus.side_effect = runpod_error.QueryError("502")
    with pytest.raises(ConductorError):
        RunPodProvider().get_gpu_catalog()


@patch("conductor.providers.runpod.runpod")
def test_runpod_check_exists_tri_state(mock_runpod):
    provider = RunPodProvider()
    mock_runpod.get_pod.return_value = {"id": "p", "desiredStatus": "RUNNING"}
    assert provider.check_exists("p") is True
    mock_runpod.get_pod.return_value = {"id": "p", "desiredStatus": "EXITED"}
    assert provider.check_exists("p") is False
    mock_runpod.get_pod.return_value = None
    assert provider.check_exists("p") is False
    mock_runpod.get_pod.side_effect = RuntimeError("502 Bad Gateway")
    assert provider.check_exists("p") is None
    mock_runpod.get_pod.side_effect = ValueError("Expecting value: line 1 column 1")  # non-JSON body
    assert provider.check_exists("p") is None


@patch("conductor.providers.runpod.runpod")
def test_runpod_get_ssh_info(mock_runpod):
    provider = RunPodProvider()
    mock_runpod.get_pod.return_value = {
        "runtime": {"ports": [{"privatePort": 22, "ip": "1.2.3.4", "publicPort": "22222"}]},
    }
    assert provider.get_ssh_info("p") == ("1.2.3.4", 22222)
    mock_runpod.get_pod.return_value = {"runtime": None}
    assert provider.get_ssh_info("p") is None
    mock_runpod.get_pod.side_effect = RuntimeError("down")
    assert provider.get_ssh_info("p") is None


def _vast_response(payload=None, status=200, raise_json=False):
    resp = MagicMock()
    resp.status_code = status
    if status >= 400:
        import httpx
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=MagicMock(status_code=status))
    if raise_json:
        import json
        resp.json.side_effect = json.JSONDecodeError("bad", "<html>", 0)
    else:
        resp.json.return_value = payload
    return resp


def test_vastai_check_exists_tri_state():
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    provider.configure("key")
    with patch.object(provider, "_req") as req:
        req.return_value = _vast_response({"instances": [{"id": 42, "actual_status": "running"}]})
        assert provider.check_exists("42") is True
        req.return_value = _vast_response({"instances": [{"id": 42, "actual_status": "exited"}]})
        assert provider.check_exists("42") is False
        req.return_value = _vast_response({"instances": []})
        assert provider.check_exists("42") is False
        req.return_value = _vast_response(status=502)
        assert provider.check_exists("42") is None
        req.return_value = _vast_response(raise_json=True)
        assert provider.check_exists("42") is None
        req.side_effect = OSError("network unreachable")
        assert provider.check_exists("42") is None


def test_vastai_get_ssh_info():
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    with patch.object(provider, "_req") as req:
        req.return_value = _vast_response({"instances": [
            {"id": 42, "actual_status": "running", "ssh_host": "ssh.vast.ai", "ssh_port": 1234},
        ]})
        assert provider.get_ssh_info("42") == ("ssh.vast.ai", 1234)
        req.return_value = _vast_response(status=500)
        assert provider.get_ssh_info("42") is None


@patch("conductor.providers.runpod.runpod")
def test_runpod_list_instances(mock_runpod):
    mock_runpod.get_pods.return_value = [
        {"id": "a", "name": "conductor-abc123-train", "desiredStatus": "RUNNING"},
        {"id": "b", "name": "conductor-abc123-eval", "desiredStatus": "EXITED"},
        {"id": "c", "desiredStatus": "RUNNING"},
    ]
    insts = RunPodProvider().list_instances()
    assert insts == [
        {"id": "a", "name": "conductor-abc123-train", "alive": True},
        {"id": "b", "name": "conductor-abc123-eval", "alive": False},
        {"id": "c", "name": "", "alive": True},
    ]
    from runpod import error as runpod_error
    mock_runpod.get_pods.side_effect = runpod_error.QueryError("429")
    with pytest.raises(ConductorError):
        RunPodProvider().list_instances()


def test_vastai_list_instances():
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    with patch.object(provider, "_req") as req:
        req.return_value = _vast_response({"instances": [
            {"id": 1, "label": "conductor-abc123-train", "actual_status": "running"},
            {"id": 2, "label": "other", "actual_status": "exited"},
        ]})
        assert provider.list_instances() == [
            {"id": "1", "name": "conductor-abc123-train", "alive": True},
            {"id": "2", "name": "other", "alive": False},
        ]
        req.return_value = _vast_response(raise_json=True)
        with pytest.raises(ConductorError):
            provider.list_instances()


@patch("conductor.providers.runpod.runpod")
def test_runpod_check_exists_retries_transient_errors(mock_runpod):
    from runpod import error as runpod_error
    mock_runpod.get_pod.side_effect = [
        runpod_error.QueryError("502 Bad Gateway"),
        {"id": "p", "desiredStatus": "RUNNING"},
    ]
    assert RunPodProvider().check_exists("p") is True
    assert mock_runpod.get_pod.call_count == 2

    mock_runpod.get_pod.side_effect = runpod_error.AuthenticationError("bad key")
    mock_runpod.get_pod.reset_mock()
    assert RunPodProvider().check_exists("p") is None
    assert mock_runpod.get_pod.call_count == 1  # not retried


@patch("conductor.providers.runpod.runpod")
def test_runpod_terminate_retries(mock_runpod):
    from runpod import error as runpod_error
    mock_runpod.terminate_pod.side_effect = [runpod_error.QueryError("429"), None]
    assert RunPodProvider().terminate("p") is True
    assert mock_runpod.terminate_pod.call_count == 2


def test_vastai_list_retries_5xx_not_4xx():
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    with patch.object(provider, "_req") as req:
        req.side_effect = [_vast_response(status=503),
                           _vast_response({"instances": [{"id": 1, "label": "x", "actual_status": "running"}]})]
        assert provider.check_exists("1") is True
        assert req.call_count == 2

        req.reset_mock()
        req.side_effect = [_vast_response(status=403)]
        assert provider.check_exists("1") is None
        assert req.call_count == 1


def test_vastai_terminate_retries():
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    with patch.object(provider, "_req") as req:
        req.side_effect = [_vast_response(status=502), _vast_response({})]
        assert provider.terminate("1") is True
        assert req.call_count == 2


@patch("conductor.providers.runpod.runpod")
def test_runpod_wait_for_ssh_is_interruptible(mock_runpod):
    from conductor import shutdown
    from conductor.shutdown import Interrupted
    mock_runpod.get_pod.return_value = {"id": "p", "desiredStatus": "RUNNING", "runtime": None}
    shutdown.request()
    with pytest.raises(Interrupted):
        RunPodProvider().wait_for_ssh("p", timeout=900)
    assert mock_runpod.get_pod.call_count == 1


def test_vastai_wait_for_ssh_is_interruptible():
    from conductor import shutdown
    from conductor.shutdown import Interrupted
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    with patch.object(provider, "_req") as req:
        req.return_value = _vast_response({"instances": [{"id": 1, "actual_status": "loading"}]})
        shutdown.request()
        with pytest.raises(Interrupted):
            provider.wait_for_ssh("1", timeout=900)
        assert req.call_count == 1


@patch("conductor.providers.runpod.runpod")
def test_runpod_create_raises_no_capacity_on_availability_message(mock_runpod):
    from conductor.errors import NoCapacityError
    from runpod import error as runpod_error
    mock_runpod.create_pod.side_effect = runpod_error.QueryError(
        "There are no longer any instances available with the requested specifications. "
        "Please refresh and try again.")
    with pytest.raises(NoCapacityError) as exc:
        RunPodProvider().create_instance("NVIDIA RTX A2000", "img", name="conductor-x-a")
    assert "NVIDIA RTX A2000" in str(exc.value)

    # Any other create failure is still a plain None (not retried as capacity)
    mock_runpod.create_pod.side_effect = runpod_error.QueryError("Invalid GPU type id")
    assert RunPodProvider().create_instance("bogus", "img") is None


def test_vastai_no_offers_is_no_capacity():
    from conductor.errors import NoCapacityError
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    with patch.object(provider, "_req") as req:
        req.return_value = _vast_response({"offers": []})
        with pytest.raises(NoCapacityError):
            provider.create_instance("RTX 4090", "img")


@patch("conductor.providers.runpod.runpod")
def test_runpod_terminate_not_found_is_already_gone(mock_runpod):
    from runpod import error as runpod_error
    mock_runpod.terminate_pod.side_effect = runpod_error.QueryError("pod not found to terminate")
    assert RunPodProvider().terminate("p") is True
    assert mock_runpod.terminate_pod.call_count == 1   # not retried


@patch("conductor.providers.runpod.runpod")
def test_runpod_terminate_other_error_is_failure(mock_runpod):
    from runpod import error as runpod_error
    mock_runpod.terminate_pod.side_effect = runpod_error.QueryError("internal error")
    assert RunPodProvider().terminate("p") is False
    assert mock_runpod.terminate_pod.call_count == 4


def test_vastai_terminate_missing_instance_is_already_gone():
    from conductor.providers.vastai import VastaiProvider
    provider = VastaiProvider()
    with patch.object(provider, "_req") as req:
        req.return_value = _vast_response(status=404)
        assert provider.terminate("1") is True
        assert req.call_count == 1

        req.reset_mock()
        req.return_value = _vast_response({"success": False, "msg": "Instance not found"})
        assert provider.terminate("1") is True
        assert req.call_count == 1

        req.reset_mock()
        req.return_value = _vast_response({"success": False, "msg": "rate limited"})
        assert provider.terminate("1") is False


@patch("conductor.providers.runpod.runpod")
def test_runpod_create_instance_suppresses_sdk_stdout(mock_runpod, capsys):
    import sys
    real_stdout = sys.stdout

    def chatty_create_pod(**kwargs):
        print("raw_response: {'env': ['PUBLIC_KEY=ssh-rsa AAAA...']}")
        return {"id": "pod1"}
    mock_runpod.create_pod.side_effect = chatty_create_pod
    mock_runpod.get_pod.return_value = {"costPerHr": 0.5}

    result = RunPodProvider().create_instance("NVIDIA L4", "img", name="n")

    assert result == {"id": "pod1", "cost_per_hour": 0.5}
    assert "raw_response" not in capsys.readouterr().out
    assert sys.stdout is real_stdout


@patch("conductor.providers.runpod.runpod")
def test_runpod_create_instance_restores_stdout_on_error(mock_runpod, capsys):
    import sys
    real_stdout = sys.stdout

    def failing_create_pod(**kwargs):
        print("raw_response: {...}")
        raise RuntimeError("boom")
    mock_runpod.create_pod.side_effect = failing_create_pod

    assert RunPodProvider().create_instance("NVIDIA L4", "img", name="n") is None
    assert sys.stdout is real_stdout
    assert "raw_response" not in capsys.readouterr().out


def test_mute_stdout_is_shared_across_threads(capsys):
    """Parallel provisioning: nested enter/exit from two threads must end
    with the real stdout restored, not one thread's sink."""
    import sys
    import threading
    from conductor.providers.runpod import _MuteStdout
    mute = _MuteStdout()
    real_stdout = sys.stdout
    a_in, b_in, a_out = threading.Event(), threading.Event(), threading.Event()

    def thread_a():
        with mute:
            print("raw_response: a")
            a_in.set()
            b_in.wait(5)
        a_out.set()

    def thread_b():
        a_in.wait(5)
        with mute:
            print("raw_response: b")
            b_in.set()
            a_out.wait(5)

    ta, tb = threading.Thread(target=thread_a), threading.Thread(target=thread_b)
    ta.start(); tb.start(); ta.join(5); tb.join(5)
    assert sys.stdout is real_stdout
    print("visible")
    out = capsys.readouterr().out
    assert "raw_response" not in out and "visible" in out
