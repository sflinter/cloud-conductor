from __future__ import annotations

import time
from dataclasses import dataclass

import runpod

_cache: list[dict] | None = None
_cache_time: float = 0.0
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


def get_gpu_types(force_refresh: bool = False) -> list[GpuInfo]:
    global _cache, _cache_time
    if not force_refresh and _cache is not None and (time.time() - _cache_time) < _CACHE_TTL:
        return _parse_gpu_list(_cache)

    raw = runpod.get_gpus()
    _cache = raw
    _cache_time = time.time()
    return _parse_gpu_list(raw)


def _parse_gpu_list(raw: list[dict]) -> list[GpuInfo]:
    results = []
    for gpu in raw:
        results.append(GpuInfo(
            id=gpu.get("id", ""),
            display_name=gpu.get("displayName", gpu.get("id", "")),
            memory_mb=gpu.get("memoryInGb", 0) * 1024,
            community_price=gpu.get("communityPrice", 0.0) or 0.0,
            secure_price=gpu.get("securePrice", 0.0) or 0.0,
            community_available=bool(gpu.get("communityAvailable")),
            secure_available=bool(gpu.get("secureAvailable")),
        ))
    return results


def select_cheapest_gpus(min_vram_gb: int = 0, cloud_type: str = "ALL") -> list[GpuInfo]:
    gpus = get_gpu_types()
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


def validate_gpu_id(gpu_id: str) -> bool:
    gpus = get_gpu_types()
    return any(g.id == gpu_id for g in gpus)


def get_gpu_price(gpu_id: str, cloud_type: str = "ALL") -> float:
    gpus = get_gpu_types()
    for g in gpus:
        if g.id == gpu_id:
            return _get_best_price(g, cloud_type)
    return 0.0


def clear_cache() -> None:
    global _cache, _cache_time
    _cache = None
    _cache_time = 0.0
