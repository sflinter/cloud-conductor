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
