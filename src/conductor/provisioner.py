from __future__ import annotations

import logging
import time

import runpod

from conductor.config import JobConfig
from conductor.gpu_pricing import get_gpu_price, select_cheapest_gpus
from conductor.ssh import wait_ssh
from conductor.state import PodState

log = logging.getLogger(__name__)


def provision_pod(config: JobConfig, pod_state: PodState) -> PodState:
    # Pod reuse: if keep_pod_alive and pod already exists, verify it's still running
    if config.keep_pod_alive and pod_state.pod_id:
        if check_pod_exists(pod_state.pod_id):
            log.info(f"[{config.name}] Reusing existing pod {pod_state.pod_id}")
            return pod_state

    gpu_candidates = _get_gpu_candidates(config)
    if not gpu_candidates:
        pod_state.status = "failed"
        pod_state.error = "No GPU candidates available"
        return pod_state

    pod_state.status = "provisioning"
    for gpu_id in gpu_candidates:
        pod_state.provision_attempts += 1
        log.info(f"[{config.name}] Attempting to provision with {gpu_id} (attempt {pod_state.provision_attempts})")

        try:
            pod = runpod.create_pod(
                name=f"conductor-{config.name}",
                image_name=config.image_name,
                gpu_type_id=gpu_id,
                cloud_type=config.cloud_type if config.cloud_type != "ALL" else "ALL",
                container_disk_in_gb=config.container_disk_in_gb,
                volume_in_gb=config.volume_in_gb if config.volume_in_gb > 0 else None,
                volume_mount_path=config.volume_mount_path if config.volume_in_gb > 0 else None,
            )
        except Exception as e:
            log.warning(f"[{config.name}] Failed to create pod with {gpu_id}: {e}")
            continue

        if not pod or "id" not in pod:
            log.warning(f"[{config.name}] No pod returned for {gpu_id}")
            continue

        pod_state.pod_id = pod["id"]
        pod_state.gpu_type = gpu_id

        # Get cost per hour
        if config.cost_per_hour_override > 0:
            pod_state.gpu_cost_per_hour = config.cost_per_hour_override
        else:
            pod_state.gpu_cost_per_hour = get_gpu_price(gpu_id, config.cloud_type)

        # Wait for SSH details from RunPod API
        ssh_info = _wait_for_ssh_info(pod_state.pod_id)
        if not ssh_info:
            log.warning(f"[{config.name}] Could not get SSH info for pod {pod_state.pod_id}")
            teardown_pod(pod_state.pod_id)
            pod_state.pod_id = None
            continue

        pod_state.ssh_host, pod_state.ssh_port = ssh_info

        # Wait for SSH connectivity
        log.info(f"[{config.name}] Waiting for SSH at {pod_state.ssh_host}:{pod_state.ssh_port}")
        if wait_ssh(pod_state.ssh_host, pod_state.ssh_port, config.ssh_key_path):
            pod_state.started_at = time.time()
            log.info(f"[{config.name}] Pod {pod_state.pod_id} ready with {gpu_id}")
            return pod_state
        else:
            log.warning(f"[{config.name}] SSH timeout for pod {pod_state.pod_id}")
            teardown_pod(pod_state.pod_id)
            pod_state.pod_id = None
            continue

    pod_state.status = "failed"
    pod_state.error = "All GPU types exhausted"
    return pod_state


def _get_gpu_candidates(config: JobConfig) -> list[str]:
    if config.auto_select_cheapest_gpu:
        gpus = select_cheapest_gpus(
            min_vram_gb=config.gpu_min_vram_gb,
            cloud_type=config.cloud_type,
        )
        return [g.id for g in gpus]
    else:
        candidates = []
        if config.gpu_type_id:
            candidates.append(config.gpu_type_id)
        candidates.extend(config.gpu_type_ids_fallback)
        return candidates


def _wait_for_ssh_info(pod_id: str, timeout: int = 300) -> tuple[str, int] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pod = runpod.get_pod(pod_id)
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


def check_pod_exists(pod_id: str) -> bool:
    try:
        pod = runpod.get_pod(pod_id)
        if pod and pod.get("desiredStatus") not in ("EXITED", "TERMINATED"):
            return True
    except Exception:
        pass
    return False


def teardown_pod(pod_id: str) -> bool:
    try:
        runpod.terminate_pod(pod_id)
        return True
    except Exception as e:
        log.warning(f"Failed to terminate pod {pod_id}: {e}")
        return False
