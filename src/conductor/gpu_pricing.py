# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from conductor.providers import CloudProvider

_cache: dict[str, list[GpuInfo]] = {}
_cache_time: dict[str, float] = {}
_detail_cache: dict[tuple[str, str], tuple[float, GpuInfo | None]] = {}
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
    """Hourly price from the catalog; when the catalog carries no price for
    the GPU (RunPod's list query omits prices) and the provider can describe
    a single GPU type (`get_gpu_detail`), that is used instead. 0.0 if
    unknown."""
    gpus = get_gpu_types(provider)
    for g in gpus:
        if g.id == gpu_id:
            price = _get_best_price(g, cloud_type)
            if price > 0:
                return price
            break
    detail = get_gpu_detail(provider, gpu_id)
    return _get_best_price(detail, cloud_type) if detail else 0.0


@dataclass
class PriceQuote:
    """The hourly price of what create_instance would actually request.
    price is the estimate (the highest price the request can be billed at);
    low is the lowest; label says which offer type and cloud it is."""
    price: float
    low: float
    label: str


def _gpu_info(provider: CloudProvider, gpu_id: str) -> GpuInfo | None:
    """The catalog entry if it carries a price, else the per-GPU detail."""
    for g in get_gpu_types(provider):
        if g.id == gpu_id:
            if g.community_price > 0 or g.secure_price > 0:
                return g
            break
    return get_gpu_detail(provider, gpu_id)


def quote_price(provider: CloudProvider, gpu_id: str, cloud_type: str = "ALL",
                bid_price: float = 0.0) -> PriceQuote | None:
    """Price the request create_instance makes for gpu_id, labelled.

    RunPod pods are created on-demand (podFindAndDeployOnDemand) in
    cloud_type; with ALL RunPod may place the pod on community or secure
    cloud, so the estimate is the higher of the two on-demand prices (a
    pod reported at the community price was billed at the secure one).
    Vast.ai rents the cheapest matching on-demand offer, or bids
    vastai_bid_price for an interruptible instance. None if unknown."""
    if provider.name == "vastai" and bid_price > 0:
        return PriceQuote(bid_price, bid_price, "interruptible, vastai_bid_price")
    info = _gpu_info(provider, gpu_id)
    if info is None:
        return None
    if provider.name == "vastai":
        price = info.community_price or info.secure_price
        return PriceQuote(price, price, "on-demand, cheapest offer") if price > 0 else None
    if cloud_type in ("COMMUNITY", "SECURE"):
        price = info.community_price if cloud_type == "COMMUNITY" else info.secure_price
        if price <= 0:
            return None
        return PriceQuote(price, price, f"on-demand, {cloud_type.lower()} cloud")
    options = {}
    if info.community_price > 0 and (info.community_available or not info.secure_available):
        options["community"] = info.community_price
    if info.secure_price > 0 and (info.secure_available or not info.community_available):
        options["secure"] = info.secure_price
    if not options:
        return None
    if len(options) == 1 or len(set(options.values())) == 1:
        cloud, price = next(iter(options.items()))
        where = cloud if len(options) == 1 else "community or secure"
        return PriceQuote(price, price, f"on-demand, cloud_type ALL: {where} cloud")
    low, high = min(options.values()), max(options.values())
    return PriceQuote(high, low, f"on-demand, cloud_type ALL: community ${options['community']:.2f} "
                                 f"or secure ${options['secure']:.2f}, estimate assumes the higher")


def get_gpu_detail(provider: CloudProvider, gpu_id: str) -> GpuInfo | None:
    """One GPU type with prices, via the provider's optional
    get_gpu_detail(gpu_id); cached for _CACHE_TTL. None if unsupported or
    the lookup failed."""
    fetch = getattr(provider, "get_gpu_detail", None)
    if fetch is None:
        return None
    key = (provider.name, gpu_id)
    cached = _detail_cache.get(key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]
    try:
        info = fetch(gpu_id)
    except Exception:
        info = None
    if not isinstance(info, GpuInfo):
        info = None
    _detail_cache[key] = (time.time(), info)
    return info


def clear_cache() -> None:
    _cache.clear()
    _cache_time.clear()
    _detail_cache.clear()
