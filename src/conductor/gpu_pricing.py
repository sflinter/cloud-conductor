# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conductor.providers import CloudProvider

_cache: dict[str, list[GpuInfo]] = {}
_cache_time: dict[str, float] = {}
_CACHE_TTL = 300  # 5 minutes


@dataclass
class GpuInfo:
    id: str
    display_name: str
    memory_mb: int
    community_price: float
    secure_price: float
    community_available: bool
    secure_available: bool


def get_gpu_types(provider: CloudProvider, force_refresh: bool = False) -> list[GpuInfo]:
    name = provider.name
    if (not force_refresh and name in _cache
            and (time.time() - _cache_time.get(name, 0)) < _CACHE_TTL):
        return _cache[name]

    result = provider.get_gpu_catalog()
    _cache[name] = result
    _cache_time[name] = time.time()
    return result


def select_cheapest_gpus(
    provider: CloudProvider,
    min_vram_gb: int = 0,
    cloud_type: str = "ALL",
) -> list[GpuInfo]:
    gpus = get_gpu_types(provider)
    filtered = []
    for gpu in gpus:
        vram_gb = gpu.memory_mb / 1024
        if vram_gb < min_vram_gb:
            continue
        if cloud_type == "COMMUNITY" and not gpu.community_available:
            continue
        if cloud_type == "SECURE" and not gpu.secure_available:
            continue
        if cloud_type == "ALL" and not (gpu.community_available or gpu.secure_available):
            continue

        price = _get_best_price(gpu, cloud_type)
        if price <= 0:
            continue
        filtered.append(gpu)

    filtered.sort(key=lambda g: _get_best_price(g, cloud_type))
    return filtered


def _get_best_price(gpu: GpuInfo, cloud_type: str) -> float:
    if cloud_type == "COMMUNITY":
        return gpu.community_price
    if cloud_type == "SECURE":
        return gpu.secure_price
    # ALL — pick the cheaper available option
    prices = []
    if gpu.community_available and gpu.community_price > 0:
        prices.append(gpu.community_price)
    if gpu.secure_available and gpu.secure_price > 0:
        prices.append(gpu.secure_price)
    return min(prices) if prices else 0.0


def validate_gpu_id(provider: CloudProvider, gpu_id: str) -> bool:
    gpus = get_gpu_types(provider)
    return any(g.id == gpu_id for g in gpus)


def get_gpu_price(provider: CloudProvider, gpu_id: str, cloud_type: str = "ALL") -> float:
    gpus = get_gpu_types(provider)
    for g in gpus:
        if g.id == gpu_id:
            return _get_best_price(g, cloud_type)
    return 0.0


def clear_cache() -> None:
    _cache.clear()
    _cache_time.clear()
