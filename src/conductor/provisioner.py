# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import logging
import random
import time
from typing import Callable

from conductor import shutdown
from conductor.errors import ConductorError, NoCapacityError, SshAuthError
from conductor.gpu_pricing import select_cheapest_gpus
from conductor.providers import CloudProvider
from conductor.registry import PodRecord
from conductor.shutdown import Interrupted
from conductor.ssh import wait_ssh
from conductor.state import PodState, pod_name_for, reset_deploy_progress

log = logging.getLogger(__name__)

TERMINATE_ATTEMPTS = 3
TERMINATE_VERIFY_SECONDS = 60.0   # how long to poll the provider after terminate
TERMINATE_POLL_SECONDS = 5.0


def provision_pod_direct(
    provider: CloudProvider,
    gpu_type_id: str,
    image_name: str,
    ssh_key_path: str,
    name: str = "",
    disk_gb: int = 40,
    pod_name: str = "",
    on_created: Callable[[str, str, float], None] | None = None,
    on_released: Callable[[str, bool], None] | None = None,
    provision_timeout: int = 900,
    **provider_kwargs,
) -> PodRecord | None:
    """Create an instance and wait for SSH.

    on_created(instance_id, gpu_type_id, cost_per_hour) is called the moment
    the provider returns an ID, before any SSH wait, so the caller can persist
    it: a pod must never exist that is recorded nowhere. If SSH never comes
    up the instance is terminated and on_released(instance_id, terminated)
    reports whether that termination succeeded."""
    pod_name = pod_name or (f"conductor-{name}" if name else "conductor-pod")
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

    instance_id = str(result["id"])
    cost = result["cost_per_hour"]
    log.info(f"[{pod_name}] Created instance {instance_id} ({gpu_type_id}, ${cost:.3f}/hr)")
    if on_created:
        on_created(instance_id, gpu_type_id, cost)

    ssh_info = provider.wait_for_ssh(instance_id, timeout=provision_timeout)
    if not ssh_info:
        log.warning(f"[{pod_name}] Could not get SSH info for instance {instance_id}")
        _release(provider, instance_id, pod_name, on_released)
        return None

    ssh_host, ssh_port = ssh_info
    log.info(f"[{pod_name}] Waiting for SSH at {ssh_host}:{ssh_port}")

    try:
        ready = wait_ssh(ssh_host, ssh_port, ssh_key_path, timeout=provision_timeout)
    except SshAuthError as e:
        # The pod is fine and billing; the key is not. Release it and stop.
        log.error(f"[{pod_name}] {e}")
        _release(provider, instance_id, pod_name, on_released)
        raise
    if not ready:
        log.warning(f"[{pod_name}] SSH timeout for instance {instance_id}")
        _release(provider, instance_id, pod_name, on_released)
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


def _release(provider: CloudProvider, instance_id: str, pod_name: str,
             on_released: Callable[[str, bool], None] | None = None) -> bool:
    ok = teardown_pod(provider, instance_id)
    if not ok:
        log.error(f"[{pod_name}] COULD NOT TERMINATE instance {instance_id}; "
                  f"it may still be billing. Check 'conductor pod list' / the provider console.")
    if on_released:
        on_released(instance_id, ok)
    return ok


def provision_pod(
    provider: CloudProvider,
    config,
    pod_state: PodState,
    run_id: str = "",
    on_created: Callable[[PodState], None] | None = None,
    on_released: Callable[[str, bool], None] | None = None,
) -> PodState:
    """Provision a pod for a declarative job. on_created(pod_state) is called
    as soon as the pod ID is known so the caller can persist it;
    on_released(pod_id, terminated) when a pod that never became reachable
    has been (or could not be) terminated.

    The candidate list is walked once per pass; when a pass found no
    capacity on at least one candidate it is repeated after a jittered
    backoff, up to cfg.provision_retries passes. Each pass (not each
    candidate) counts one provision attempt."""
    from conductor.config import JobConfig
    cfg: JobConfig = config

    if cfg.keep_pod_alive and pod_state.pod_id:
        if check_pod_exists(provider, pod_state.pod_id) is True:
            log.info(f"[{cfg.name}] Reusing existing pod {pod_state.pod_id}")
            # Ports can be remapped between runs: always re-read SSH info
            if not refresh_ssh_info(provider, pod_state):
                log.warning(f"[{cfg.name}] Could not refresh SSH info for {pod_state.pod_id}, "
                            f"keeping {pod_state.ssh_host}:{pod_state.ssh_port}")
            return pod_state

    try:
        gpu_candidates = _get_gpu_candidates(provider, cfg)
    except ConductorError as e:
        pod_state.status = "failed"
        pod_state.error = f"Could not fetch GPU catalog: {e}"
        return pod_state
    if not gpu_candidates:
        pod_state.status = "failed"
        pod_state.error = "No GPU candidates available"
        return pod_state

    def _created(instance_id: str, gpu_id: str, cost: float) -> None:
        pod_state.pod_id = instance_id
        pod_state.gpu_type = gpu_id
        pod_state.started_at = time.time()
        pod_state.ssh_host = None
        pod_state.ssh_port = None
        reset_deploy_progress(pod_state)
        pod_state.gpu_cost_per_hour = cfg.cost_per_hour_override if cfg.cost_per_hour_override > 0 else cost
        if on_created:
            on_created(pod_state)

    pod_state.status = "provisioning"
    passes = max(1, cfg.provision_retries)
    for attempt in range(1, passes + 1):
        pod_state.provision_attempts += 1
        no_capacity = []
        for gpu_id in gpu_candidates:
            # Never create another pod once shutdown (Ctrl-C, budget stop) is requested
            shutdown.check(f"provisioning of {cfg.name}")
            try:
                record = _provision_candidate(provider, cfg, gpu_id, run_id, _created, on_released)
            except NoCapacityError as e:
                log.warning(f"[{cfg.name}] No capacity: {e}")
                no_capacity.append(gpu_id)
                continue
            except SshAuthError as e:
                # Another GPU with the same key would fail the same way
                _forget_released_pod(pod_state)
                pod_state.status = "failed"
                pod_state.error = str(e)
                return pod_state
            if record:
                pod_state.pod_id = record.pod_id
                pod_state.gpu_type = record.gpu_type
                pod_state.ssh_host = record.ssh_host
                pod_state.ssh_port = record.ssh_port
                pod_state.started_at = pod_state.started_at or record.created_at
                if cfg.cost_per_hour_override > 0:
                    pod_state.gpu_cost_per_hour = cfg.cost_per_hour_override
                else:
                    pod_state.gpu_cost_per_hour = record.gpu_cost_per_hour
                return pod_state
            _forget_released_pod(pod_state)

        if not no_capacity or attempt >= passes:
            break
        wait = cfg.backoff_base_seconds * (2 ** (attempt - 1)) * (0.5 + random.random())
        log.warning(f"[{cfg.name}] No capacity on {len(no_capacity)}/{len(gpu_candidates)} GPU "
                    f"type(s) (pass {attempt}/{passes}); retrying the list in {wait:.0f}s")
        if shutdown.wait(wait):
            raise Interrupted(f"shutdown requested while waiting to retry provisioning of {cfg.name}")

    pod_state.status = "failed"
    pod_state.error = (f"All GPU types exhausted after {passes} pass(es)" if passes > 1
                       else "All GPU types exhausted")
    return pod_state


def _provision_candidate(provider, cfg, gpu_id, run_id, on_created, on_released) -> PodRecord | None:
    return provision_pod_direct(
        provider=provider,
        gpu_type_id=gpu_id,
        image_name=cfg.image_name,
        ssh_key_path=cfg.ssh_key_path,
        name=cfg.name,
        disk_gb=cfg.container_disk_in_gb,
        pod_name=pod_name_for(run_id, cfg.name),
        on_created=on_created,
        on_released=on_released,
        provision_timeout=cfg.provision_timeout_seconds,
        cloud_type=cfg.cloud_type,
        volume_in_gb=cfg.volume_in_gb,
        volume_mount_path=cfg.volume_mount_path,
        vastai_min_reliability=cfg.vastai_min_reliability,
        vastai_bid_price=cfg.vastai_bid_price,
        vastai_num_gpus=cfg.vastai_num_gpus,
        vastai_geolocation=cfg.vastai_geolocation,
    )


def _forget_released_pod(pod_state: PodState) -> None:
    """A pod that was created but never became reachable has been released
    (on_released recorded the outcome); the job no longer tracks it."""
    if pod_state.pod_id:
        pod_state.pod_id = None
        pod_state.ssh_host = None
        pod_state.ssh_port = None
        reset_deploy_progress(pod_state)


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


def check_pod_exists(provider: CloudProvider, pod_id: str) -> bool | None:
    """True/False from the provider, None if it could not be determined."""
    try:
        return provider.check_exists(pod_id)
    except Exception as e:
        log.warning(f"check_exists({pod_id}) raised: {e}")
        return None


def refresh_ssh_info(provider: CloudProvider, pod_state: PodState) -> bool:
    """Re-read host/port from the provider. Returns True if updated."""
    if not pod_state.pod_id:
        return False
    try:
        info = provider.get_ssh_info(pod_state.pod_id)
    except Exception as e:
        log.warning(f"[{pod_state.name}] get_ssh_info failed: {e}")
        return False
    if not isinstance(info, (tuple, list)) or len(info) != 2 or not info[0] or not info[1]:
        return False
    host, port = info[0], int(info[1])
    if (host, port) != (pod_state.ssh_host, pod_state.ssh_port):
        log.info(f"[{pod_state.name}] SSH endpoint changed: "
                 f"{pod_state.ssh_host}:{pod_state.ssh_port} → {host}:{port}")
    pod_state.ssh_host, pod_state.ssh_port = host, port
    return True


def teardown_pod(provider: CloudProvider, pod_id: str, verify: bool = True) -> bool:
    """Terminate a pod and, by default, verify with the provider that it is
    gone. Returns False if the terminate call kept failing or the pod was
    still reported alive at the end of the verification window; callers
    must then treat the pod as possibly still billing."""
    ok = False
    for attempt in range(1, TERMINATE_ATTEMPTS + 1):
        try:
            ok = bool(provider.terminate(pod_id))
        except Exception as e:
            log.warning(f"terminate({pod_id}) raised: {e}")
            ok = False
        if ok:
            break
        if attempt < TERMINATE_ATTEMPTS:
            time.sleep(TERMINATE_POLL_SECONDS)
    if not ok:
        log.error(f"Terminate of pod {pod_id} failed {TERMINATE_ATTEMPTS} times")
        return False
    if not verify:
        return True

    deadline = time.time() + TERMINATE_VERIFY_SECONDS
    last = None
    while True:
        last = check_pod_exists(provider, pod_id)
        if last is False:
            log.info(f"Pod {pod_id} confirmed gone")
            return True
        if time.time() >= deadline:
            break
        time.sleep(TERMINATE_POLL_SECONDS)
    if last is True:
        log.error(f"Pod {pod_id} is STILL REPORTED ALIVE {TERMINATE_VERIFY_SECONDS:.0f}s after terminate")
    else:
        log.warning(f"Could not confirm that pod {pod_id} is gone (provider not answering)")
    return False
