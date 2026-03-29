# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import time

from conductor.gpu_pricing import select_cheapest_gpus
from conductor.providers import CloudProvider
from conductor.registry import PodRecord
from conductor.ssh import wait_ssh
from conductor.state import PodState

log = logging.getLogger(__name__)


def provision_pod_direct(
    provider: CloudProvider,
    gpu_type_id: str,
    image_name: str,
    ssh_key_path: str,
    name: str = "",
    disk_gb: int = 40,
    **provider_kwargs,
) -> PodRecord | None:
    pod_name = f"conductor-{name}" if name else "conductor-pod"
    log.info(f"[{pod_name}] Attempting to provision with {gpu_type_id} on {provider.name}")

    result = provider.create_instance(
        gpu_type_id=gpu_type_id,
        image_name=image_name,
        name=pod_name,
        disk_gb=disk_gb,
        **provider_kwargs,
    )
    if not result:
        return None

    instance_id = result["id"]
    cost = result["cost_per_hour"]

    ssh_info = provider.wait_for_ssh(instance_id)
    if not ssh_info:
        log.warning(f"[{pod_name}] Could not get SSH info for instance {instance_id}")
        provider.terminate(instance_id)
        return None

    ssh_host, ssh_port = ssh_info
    log.info(f"[{pod_name}] Waiting for SSH at {ssh_host}:{ssh_port}")

    if not wait_ssh(ssh_host, ssh_port, ssh_key_path):
        log.warning(f"[{pod_name}] SSH timeout for instance {instance_id}")
        provider.terminate(instance_id)
        return None

    log.info(f"[{pod_name}] Instance {instance_id} ready with {gpu_type_id}")
    return PodRecord(
        pod_id=instance_id,
        name=name or instance_id[:8],
        gpu_type=gpu_type_id,
        gpu_cost_per_hour=cost,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_key_path=ssh_key_path,
        image_name=image_name,
        status="running",
        created_at=time.time(),
        provider=provider.name,
    )


def provision_pod(provider: CloudProvider, config, pod_state: PodState) -> PodState:
    from conductor.config import JobConfig
    cfg: JobConfig = config

    if cfg.keep_pod_alive and pod_state.pod_id:
        if check_pod_exists(provider, pod_state.pod_id):
            log.info(f"[{cfg.name}] Reusing existing pod {pod_state.pod_id}")
            return pod_state

    gpu_candidates = _get_gpu_candidates(provider, cfg)
    if not gpu_candidates:
        pod_state.status = "failed"
        pod_state.error = "No GPU candidates available"
        return pod_state

    pod_state.status = "provisioning"
    for gpu_id in gpu_candidates:
        pod_state.provision_attempts += 1
        record = provision_pod_direct(
            provider=provider,
            gpu_type_id=gpu_id,
            image_name=cfg.image_name,
            ssh_key_path=cfg.ssh_key_path,
            name=cfg.name,
            disk_gb=cfg.container_disk_in_gb,
            cloud_type=cfg.cloud_type,
            volume_in_gb=cfg.volume_in_gb,
            volume_mount_path=cfg.volume_mount_path,
            vastai_min_reliability=cfg.vastai_min_reliability,
            vastai_bid_price=cfg.vastai_bid_price,
            vastai_num_gpus=cfg.vastai_num_gpus,
            vastai_geolocation=cfg.vastai_geolocation,
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


def _get_gpu_candidates(provider: CloudProvider, config) -> list[str]:
    if config.auto_select_cheapest_gpu:
        gpus = select_cheapest_gpus(
            provider=provider,
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


def check_pod_exists(provider: CloudProvider, pod_id: str) -> bool:
    return provider.check_exists(pod_id)


def teardown_pod(provider: CloudProvider, pod_id: str) -> bool:
    return provider.terminate(pod_id)
