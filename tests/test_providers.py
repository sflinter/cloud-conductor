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
