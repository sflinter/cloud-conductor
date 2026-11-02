# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import time

import runpod

from conductor import shutdown
from conductor.errors import guarded
from conductor.gpu_pricing import GpuInfo
from conductor.retry import retry
from conductor.shutdown import Interrupted

log = logging.getLogger(__name__)

WAIT_POLL_SECONDS = 5.0


def _pause(instance_id: str) -> None:
    if shutdown.wait(WAIT_POLL_SECONDS):
        raise Interrupted(f"shutdown requested while waiting for SSH info of {instance_id}")


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
                _pause(instance_id)
                continue

            if not pod:
                _pause(instance_id)
                continue

            ssh = self._pod_ssh(pod)
            if ssh:
                return ssh

            status = pod.get("desiredStatus", "")
            if status in ("EXITED", "TERMINATED"):
                return None

            _pause(instance_id)
        return None

    def _get_pod(self, instance_id: str) -> dict | None:
        """get_pod with retries on transport errors, 429 and 5xx."""
        return retry(guarded, "runpod.get_pod", runpod.get_pod, instance_id, what="runpod.get_pod")

    def get_ssh_info(self, instance_id: str) -> tuple[str, int] | None:
        try:
            pod = self._get_pod(instance_id)
        except Exception as e:
            log.warning(f"Could not query pod {instance_id} for SSH info: {e}")
            return None
        return self._pod_ssh(pod) if pod else None

    @staticmethod
    def _pod_ssh(pod: dict) -> tuple[str, int] | None:
        runtime = pod.get("runtime") or {}
        for port_info in runtime.get("ports") or []:
            if port_info.get("privatePort") == 22:
                host = port_info.get("ip")
                port = port_info.get("publicPort")
                if host and port:
                    return (host, int(port))
        return None

    def check_exists(self, instance_id: str) -> bool | None:
        try:
            pod = self._get_pod(instance_id)
        except Exception as e:
            log.warning(f"Could not check pod {instance_id}: {e}")
            return None
        if not isinstance(pod, dict):
            # None/empty means the API no longer knows the pod
            return False if pod is None else None
        return pod.get("desiredStatus") not in ("EXITED", "TERMINATED")

    def terminate(self, instance_id: str) -> bool:
        try:
            retry(guarded, "runpod.terminate_pod", runpod.terminate_pod, instance_id,
                  what="runpod.terminate_pod")
            return True
        except Exception as e:
            log.warning(f"Failed to terminate pod {instance_id}: {e}")
            return False

    def list_instances(self) -> list[dict]:
        raw = retry(guarded, "runpod.get_pods", runpod.get_pods, what="runpod.get_pods") or []
        return [{
            "id": str(pod.get("id", "")),
            "name": pod.get("name") or "",
            "alive": pod.get("desiredStatus") not in ("EXITED", "TERMINATED"),
        } for pod in raw if pod.get("id")]

    def get_gpu_catalog(self) -> list[GpuInfo]:
        raw = retry(guarded, "runpod.get_gpus", runpod.get_gpus, what="runpod.get_gpus")
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
            pod = self._get_pod(pod_id)
            cost = pod.get("costPerHr", 0) if pod else 0
            if cost and cost > 0:
                return float(cost)
        except Exception:
            pass
        from conductor.gpu_pricing import get_gpu_price
        try:
            return get_gpu_price(self, gpu_id, cloud_type)
        except Exception as e:
            log.warning(f"Could not look up price for {gpu_id}: {e}")
            return 0.0
