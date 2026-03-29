from unittest.mock import MagicMock

from conductor.gpu_pricing import (
    GpuInfo, get_gpu_types, select_cheapest_gpus, validate_gpu_id, get_gpu_price, clear_cache,
)


MOCK_GPU_INFO = [
    GpuInfo(
        id="NVIDIA RTX A2000", display_name="NVIDIA RTX A2000",
        memory_mb=6 * 1024, community_price=0.12, secure_price=0.15,
        community_available=True, secure_available=True,
    ),
    GpuInfo(
        id="NVIDIA RTX A4000", display_name="NVIDIA RTX A4000",
        memory_mb=16 * 1024, community_price=0.25, secure_price=0.30,
        community_available=True, secure_available=False,
    ),
    GpuInfo(
        id="NVIDIA A100 80GB", display_name="NVIDIA A100 80GB",
        memory_mb=80 * 1024, community_price=1.50, secure_price=2.00,
        community_available=True, secure_available=True,
    ),
]


def _mock_provider():
    p = MagicMock()
    p.name = "runpod"
    p.get_gpu_catalog.return_value = list(MOCK_GPU_INFO)
    return p


def test_get_gpu_types():
    clear_cache()
    provider = _mock_provider()
    gpus = get_gpu_types(provider)
    assert len(gpus) == 3
    assert gpus[0].id == "NVIDIA RTX A2000"
    assert gpus[0].memory_mb == 6 * 1024
    provider.get_gpu_catalog.assert_called_once()


def test_cache():
    clear_cache()
    provider = _mock_provider()
    get_gpu_types(provider)
    get_gpu_types(provider)
    assert provider.get_gpu_catalog.call_count == 1  # cached


def test_select_cheapest():
    clear_cache()
    provider = _mock_provider()
    gpus = select_cheapest_gpus(provider, min_vram_gb=0, cloud_type="ALL")
    assert len(gpus) == 3
    assert gpus[0].id == "NVIDIA RTX A2000"
    assert gpus[-1].id == "NVIDIA A100 80GB"


def test_select_cheapest_vram_filter():
    clear_cache()
    provider = _mock_provider()
    gpus = select_cheapest_gpus(provider, min_vram_gb=16, cloud_type="ALL")
    assert len(gpus) == 2
    assert gpus[0].id == "NVIDIA RTX A4000"


def test_select_cheapest_secure_only():
    clear_cache()
    provider = _mock_provider()
    gpus = select_cheapest_gpus(provider, min_vram_gb=0, cloud_type="SECURE")
    # A4000 not secure-available
    assert all(g.id != "NVIDIA RTX A4000" for g in gpus)


def test_validate_gpu_id():
    clear_cache()
    provider = _mock_provider()
    assert validate_gpu_id(provider, "NVIDIA RTX A2000") is True
    assert validate_gpu_id(provider, "FAKE GPU") is False


def test_get_gpu_price():
    clear_cache()
    provider = _mock_provider()
    assert get_gpu_price(provider, "NVIDIA RTX A2000", "COMMUNITY") == 0.12
    assert get_gpu_price(provider, "NVIDIA RTX A2000", "SECURE") == 0.15
    assert get_gpu_price(provider, "FAKE", "ALL") == 0.0
