from unittest.mock import patch

from conductor.gpu_pricing import (
    get_gpu_types, select_cheapest_gpus, validate_gpu_id, get_gpu_price, clear_cache,
)


MOCK_GPUS = [
    {
        "id": "NVIDIA RTX A2000",
        "displayName": "NVIDIA RTX A2000",
        "memoryInGb": 6,
        "communityPrice": 0.12,
        "securePrice": 0.15,
        "communityAvailable": True,
        "secureAvailable": True,
    },
    {
        "id": "NVIDIA RTX A4000",
        "displayName": "NVIDIA RTX A4000",
        "memoryInGb": 16,
        "communityPrice": 0.25,
        "securePrice": 0.30,
        "communityAvailable": True,
        "secureAvailable": False,
    },
    {
        "id": "NVIDIA A100 80GB",
        "displayName": "NVIDIA A100 80GB",
        "memoryInGb": 80,
        "communityPrice": 1.50,
        "securePrice": 2.00,
        "communityAvailable": True,
        "secureAvailable": True,
    },
]


@patch("conductor.gpu_pricing.runpod.get_gpus", return_value=MOCK_GPUS)
def test_get_gpu_types(mock_get):
    clear_cache()
    gpus = get_gpu_types()
    assert len(gpus) == 3
    assert gpus[0].id == "NVIDIA RTX A2000"
    assert gpus[0].memory_mb == 6 * 1024
    mock_get.assert_called_once()


@patch("conductor.gpu_pricing.runpod.get_gpus", return_value=MOCK_GPUS)
def test_cache(mock_get):
    clear_cache()
    get_gpu_types()
    get_gpu_types()
    assert mock_get.call_count == 1  # cached


@patch("conductor.gpu_pricing.runpod.get_gpus", return_value=MOCK_GPUS)
def test_select_cheapest(mock_get):
    clear_cache()
    gpus = select_cheapest_gpus(min_vram_gb=0, cloud_type="ALL")
    assert len(gpus) == 3
    assert gpus[0].id == "NVIDIA RTX A2000"
    assert gpus[-1].id == "NVIDIA A100 80GB"


@patch("conductor.gpu_pricing.runpod.get_gpus", return_value=MOCK_GPUS)
def test_select_cheapest_vram_filter(mock_get):
    clear_cache()
    gpus = select_cheapest_gpus(min_vram_gb=16, cloud_type="ALL")
    assert len(gpus) == 2
    assert gpus[0].id == "NVIDIA RTX A4000"


@patch("conductor.gpu_pricing.runpod.get_gpus", return_value=MOCK_GPUS)
def test_select_cheapest_secure_only(mock_get):
    clear_cache()
    gpus = select_cheapest_gpus(min_vram_gb=0, cloud_type="SECURE")
    # A4000 not secure-available
    assert all(g.id != "NVIDIA RTX A4000" for g in gpus)


@patch("conductor.gpu_pricing.runpod.get_gpus", return_value=MOCK_GPUS)
def test_validate_gpu_id(mock_get):
    clear_cache()
    assert validate_gpu_id("NVIDIA RTX A2000") is True
    assert validate_gpu_id("FAKE GPU") is False


@patch("conductor.gpu_pricing.runpod.get_gpus", return_value=MOCK_GPUS)
def test_get_gpu_price(mock_get):
    clear_cache()
    assert get_gpu_price("NVIDIA RTX A2000", "COMMUNITY") == 0.12
    assert get_gpu_price("NVIDIA RTX A2000", "SECURE") == 0.15
    assert get_gpu_price("FAKE", "ALL") == 0.0
