# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import time

import runpod

from conductor.gpu_pricing import GpuInfo

log = logging.getLogger(__name__)


class RunPodProvider:
    name = "runpod"

    def configure(self, api_key: str) -> None:
        runpod.api_key = api_key

    def create_instance(
        self,
        gpu_type_id: str,
        image_name: str,
        name: str = "",
        disk_gb: int = 40,
        **provider_kwargs,
    ) -> dict | None:
        cloud_type = provider_kwargs.get("cloud_type", "ALL")
        volume_in_gb = provider_kwargs.get("volume_in_gb", 0)
        volume_mount_path = provider_kwargs.get("volume_mount_path", "/workspace")

        try:
            pod = runpod.create_pod(
                name=name,
                image_name=image_name,
                gpu_type_id=gpu_type_id,
                cloud_type=cloud_type,
                container_disk_in_gb=disk_gb,
                volume_in_gb=volume_in_gb if volume_in_gb > 0 else None,
                volume_mount_path=volume_mount_path if volume_in_gb > 0 else None,
                ports="22/tcp",
                support_public_ip=True,
            )
        except Exception as e:
            log.error(f"[{name}] Failed to create pod: {e}")
            return None

        if not pod or "id" not in pod:
            log.error(f"[{name}] No pod returned for {gpu_type_id}")
            return None

        pod_id = pod["id"]
        cost = self._get_cost(pod_id, gpu_type_id, cloud_type)
        return {"id": pod_id, "cost_per_hour": cost}

    def wait_for_ssh(self, instance_id: str, timeout: int = 300) -> tuple[str, int] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                pod = runpod.get_pod(instance_id)
            except Exception:
                time.sleep(5)
                continue

            if not pod:
                time.sleep(5)
                continue

            runtime = pod.get("runtime")
            if runtime and runtime.get("ports"):
                for port_info in runtime["ports"]:
                    if port_info.get("privatePort") == 22:
                        host = port_info.get("ip")
                        port = port_info.get("publicPort")
                        if host and port:
                            return (host, int(port))

            status = pod.get("desiredStatus", "")
            if status in ("EXITED", "TERMINATED"):
                return None

            time.sleep(5)
        return None

    def check_exists(self, instance_id: str) -> bool:
        try:
            pod = runpod.get_pod(instance_id)
            if pod and pod.get("desiredStatus") not in ("EXITED", "TERMINATED"):
                return True
        except Exception:
            pass
        return False

    def terminate(self, instance_id: str) -> bool:
        try:
            runpod.terminate_pod(instance_id)
            return True
        except Exception as e:
            log.warning(f"Failed to terminate pod {instance_id}: {e}")
            return False

    def get_gpu_catalog(self) -> list[GpuInfo]:
        raw = runpod.get_gpus()
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

    def _get_cost(self, pod_id: str, gpu_id: str, cloud_type: str) -> float:
        try:
            pod = runpod.get_pod(pod_id)
            cost = pod.get("costPerHr", 0) if pod else 0
            if cost and cost > 0:
                return float(cost)
        except Exception:
            pass
        from conductor.gpu_pricing import get_gpu_price
        return get_gpu_price(gpu_id, cloud_type)
