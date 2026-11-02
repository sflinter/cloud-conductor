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

    def get_ssh_info(self, instance_id: str) -> tuple[str, int] | None:
        """Single query for the current (host, port); None if not available."""
        ...

    def check_exists(self, instance_id: str) -> bool | None:
        """True if the instance is alive, False if the provider reports it
        exited or missing, None if the answer could not be obtained
        (exception, non-JSON body). Callers must not treat None as False."""
        ...

    def terminate(self, instance_id: str) -> bool: ...

    def list_instances(self) -> list[dict]:
        """All of the account's instances as {"id": str, "name": str, "alive": bool}.
        Raises ConductorError if the list could not be fetched."""
        ...

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


def get_configured_provider(provider_name: str) -> CloudProvider | None:
    """Create and configure a provider using environment variables.

    Returns None if the required API key is not available."""
    import os
    from pathlib import Path

    provider = get_provider(provider_name)

    if provider_name == "runpod":
        api_key = os.environ.get("RUNPOD_API_KEY", "")
    elif provider_name == "vastai":
        api_key = os.environ.get("VASTAI_API_KEY", "")
        if not api_key:
            key_file = Path.home() / ".config" / "vastai" / "vast_api_key"
            if key_file.exists():
                api_key = key_file.read_text().strip()
    else:
        return None

    if not api_key:
        return None

    provider.configure(api_key)
    return provider
