# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import time

import runpod

from conductor.gpu_pricing import get_gpu_price, select_cheapest_gpus
from conductor.registry import PodRecord
from conductor.ssh import wait_ssh
from conductor.state import PodState

log = logging.getLogger(__name__)


def provision_pod_direct(
    gpu_type_id: str,
    image_name: str,
    ssh_key_path: str,
    name: str = "",
    cloud_type: str = "ALL",
    container_disk_in_gb: int = 40,
    volume_in_gb: int = 0,
    volume_mount_path: str = "/workspace",
) -> PodRecord | None:
    pod_name = f"conductor-{name}" if name else "conductor-pod"
    log.info(f"[{pod_name}] Attempting to provision with {gpu_type_id}")

    try:
        pod = runpod.create_pod(
            name=pod_name,
            image_name=image_name,
            gpu_type_id=gpu_type_id,
            cloud_type=cloud_type,
            container_disk_in_gb=container_disk_in_gb,
            volume_in_gb=volume_in_gb if volume_in_gb > 0 else None,
            volume_mount_path=volume_mount_path if volume_in_gb > 0 else None,
            ports="22/tcp",
            support_public_ip=True,
        )
    except Exception as e:
        log.error(f"[{pod_name}] Failed to create pod: {e}")
        return None

    if not pod or "id" not in pod:
        log.error(f"[{pod_name}] No pod returned for {gpu_type_id}")
        return None

    pod_id = pod["id"]
    cost = _get_pod_cost(pod_id, gpu_type_id, cloud_type)

    ssh_info = _wait_for_ssh_info(pod_id)
    if not ssh_info:
        log.warning(f"[{pod_name}] Could not get SSH info for pod {pod_id}")
        teardown_pod(pod_id)
        return None

    ssh_host, ssh_port = ssh_info
    log.info(f"[{pod_name}] Waiting for SSH at {ssh_host}:{ssh_port}")

    if not wait_ssh(ssh_host, ssh_port, ssh_key_path):
        log.warning(f"[{pod_name}] SSH timeout for pod {pod_id}")
        teardown_pod(pod_id)
        return None

    log.info(f"[{pod_name}] Pod {pod_id} ready with {gpu_type_id}")
    return PodRecord(
        pod_id=pod_id,
        name=name or pod_id[:8],
        gpu_type=gpu_type_id,
        gpu_cost_per_hour=cost,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_key_path=ssh_key_path,
        image_name=image_name,
        status="running",
        created_at=time.time(),
    )


def provision_pod(config, pod_state: PodState) -> PodState:
    from conductor.config import JobConfig
    cfg: JobConfig = config

    if cfg.keep_pod_alive and pod_state.pod_id:
        if check_pod_exists(pod_state.pod_id):
            log.info(f"[{cfg.name}] Reusing existing pod {pod_state.pod_id}")
            return pod_state

    gpu_candidates = _get_gpu_candidates(cfg)
    if not gpu_candidates:
        pod_state.status = "failed"
        pod_state.error = "No GPU candidates available"
        return pod_state

    pod_state.status = "provisioning"
    for gpu_id in gpu_candidates:
        pod_state.provision_attempts += 1
        record = provision_pod_direct(
            gpu_type_id=gpu_id,
            image_name=cfg.image_name,
            ssh_key_path=cfg.ssh_key_path,
            name=cfg.name,
            cloud_type=cfg.cloud_type,
            container_disk_in_gb=cfg.container_disk_in_gb,
            volume_in_gb=cfg.volume_in_gb,
            volume_mount_path=cfg.volume_mount_path,
        )
        if record:
            pod_state.pod_id = record.pod_id
            pod_state.gpu_type = record.gpu_type
            pod_state.ssh_host = record.ssh_host
            pod_state.ssh_port = record.ssh_port
            pod_state.started_at = record.created_at
            if cfg.cost_per_hour_override > 0:
                pod_state.gpu_cost_per_hour = cfg.cost_per_hour_override
            else:
                pod_state.gpu_cost_per_hour = record.gpu_cost_per_hour
            return pod_state

    pod_state.status = "failed"
    pod_state.error = "All GPU types exhausted"
    return pod_state


def _get_gpu_candidates(config) -> list[str]:
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


def _get_pod_cost(pod_id: str, gpu_id: str, cloud_type: str) -> float:
    try:
        pod = runpod.get_pod(pod_id)
        cost = pod.get("costPerHr", 0) if pod else 0
        if cost and cost > 0:
            return float(cost)
    except Exception:
        pass
    return get_gpu_price(gpu_id, cloud_type)


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
