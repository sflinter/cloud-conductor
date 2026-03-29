# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

from typing import Protocol

from conductor.gpu_pricing import GpuInfo


class CloudProvider(Protocol):
    name: str

    def configure(self, api_key: str) -> None: ...

    def create_instance(
        self,
        gpu_type_id: str,
        image_name: str,
        name: str = "",
        disk_gb: int = 40,
        **provider_kwargs,
    ) -> dict | None:
        """Create an instance. Returns {"id": str, "cost_per_hour": float} or None."""
        ...

    def wait_for_ssh(self, instance_id: str, timeout: int = 300) -> tuple[str, int] | None:
        """Poll until SSH is ready. Returns (host, port) or None."""
        ...

    def check_exists(self, instance_id: str) -> bool: ...

    def terminate(self, instance_id: str) -> bool: ...

    def get_gpu_catalog(self) -> list[GpuInfo]: ...


def get_provider(provider_name: str) -> CloudProvider:
    from conductor.providers.runpod import RunPodProvider
    from conductor.providers.vastai import VastaiProvider

    providers: dict[str, type] = {
        "runpod": RunPodProvider,
        "vastai": VastaiProvider,
    }
    cls = providers.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown provider: {provider_name}. Available: {', '.join(providers)}")
    return cls()
