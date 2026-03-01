# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import contextlib
import logging
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from conductor.config import JobConfig
from conductor.deployer import deploy
from conductor.notify import send_notification
from conductor.provisioner import check_pod_exists, provision_pod, teardown_pod
from conductor.runner import get_log_path, is_alive, launch
from conductor.state import (
    PodState, RunState, append_cost_event, get_job, save_state,
)
from conductor.syncer import sync_pull, sync_push

log = logging.getLogger(__name__)

POLL_INTERVAL = 30


def run_lifecycle(
    configs: list[JobConfig],
    state: RunState,
    state_path: str,
    cost_log_path: str,
    budget_override: float = 0.0,
) -> RunState:
    if budget_override > 0:
        state.budget_usd = budget_override

    config_map = {c.name: c for c in configs}
    shutdown = {"requested": False}

    def _sigint_handler(sig, frame):
        if shutdown["requested"]:
            log.warning("Force quit")
            sys.exit(1)
        log.info("Graceful shutdown requested (Ctrl+C)")
        shutdown["requested"] = True

    prev_handler = signal.signal(signal.SIGINT, _sigint_handler)

    try:
        # Start jobs that have no dependencies (or deps already met)
        _start_unblocked_jobs(configs, config_map, state, state_path, cost_log_path)
        save_state(state, state_path)

        while not _all_done(state) and not shutdown["requested"]:
            _monitor_tick(configs, config_map, state, state_path, cost_log_path)
            save_state(state, state_path)

            if shutdown["requested"]:
                break

            # Check global budget
            if state.budget_usd > 0 and state.total_cost_usd >= state.budget_usd:
                log.warning("Global budget exceeded! Tearing down all pods.")
                _teardown_all(configs, config_map, state, state_path, cost_log_path, reason="budget_exceeded")
                break

            _print_status(state)
            time.sleep(POLL_INTERVAL)

        if shutdown["requested"]:
            _graceful_shutdown(configs, config_map, state, state_path, cost_log_path)
    finally:
        signal.signal(signal.SIGINT, prev_handler)
        save_state(state, state_path)

    return state


def _monitor_tick(
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    state: RunState,
    state_path: str,
    cost_log_path: str,
) -> None:
    for job in state.jobs:
        if job.status != "running":
            continue

        config = config_map.get(job.name)
        if not config:
            continue

        # Update cost
        _update_cost(job, state, config, cost_log_path)

        # Check per-job budget
        if config.job_budget_usd > 0 and job.cost_usd >= config.job_budget_usd:
            log.warning(f"[{job.name}] Per-job budget exceeded (${job.cost_usd:.2f} >= ${config.job_budget_usd:.2f})")
            _finish_job(job, config, state, cost_log_path, "failed", error="job budget exceeded")
            send_notification(config.notifications, "job_budget_exceeded",
                              job=job.name, cost_usd=job.cost_usd)
            continue

        # Check if process is alive
        alive = is_alive(job, config.ssh_key_path)

        if alive is True:
            job.idle_since = None
            # Periodic sync
            if config.sync_paths and job.last_sync_at:
                elapsed_min = (time.time() - job.last_sync_at) / 60
                if elapsed_min >= config.sync_interval_minutes:
                    sync_pull(config, job)
                    job.last_sync_at = time.time()
        elif alive is False:
            # Process not running — check if pod is still alive
            if job.pod_id and check_pod_exists(job.pod_id):
                # Pod alive but process gone — idle detection
                if job.idle_since is None:
                    job.idle_since = time.time()
                    log.info(f"[{job.name}] Process not running, starting idle timer")
                elif (time.time() - job.idle_since) / 60 >= config.idle_timeout_minutes:
                    log.info(f"[{job.name}] Idle timeout reached, marking completed")
                    _finish_job(job, config, state, cost_log_path, "completed")
                    send_notification(config.notifications, "job_complete",
                                      job=job.name, cost_usd=job.cost_usd,
                                      gpu_type=job.gpu_type)
            else:
                # Pod gone — spot interruption
                log.warning(f"[{job.name}] Spot interruption detected")
                send_notification(config.notifications, "spot_recovery", job=job.name)
                _spot_recover(job, config, state, state_path, cost_log_path)
        elif alive is None:
            # SSH unreachable — likely spot interruption
            if job.pod_id and not check_pod_exists(job.pod_id):
                log.warning(f"[{job.name}] Pod gone (spot interruption)")
                send_notification(config.notifications, "spot_recovery", job=job.name)
                _spot_recover(job, config, state, state_path, cost_log_path)

    # Start newly unblocked jobs
    _start_unblocked_jobs(configs, config_map, state, state_path, cost_log_path)


def _start_unblocked_jobs(
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    state: RunState,
    state_path: str,
    cost_log_path: str,
) -> None:
    ready = []
    for job in state.jobs:
        if job.status != "pending":
            continue

        config = config_map.get(job.name)
        if not config:
            continue

        # Check if any dependency failed
        if _deps_failed(job, state):
            job.status = "skipped"
            job.error = "dependency failed"
            log.info(f"[{job.name}] Skipped (dependency failed)")
            _propagate_failure(job, state)
            continue

        # Check dependencies met (all completed)
        if not _deps_met(job, state):
            continue

        ready.append((job, config))

    if not ready:
        return

    if len(ready) == 1:
        job, config = ready[0]
        log.info(f"[{job.name}] Starting job")
        _provision_deploy_launch(job, config, state, state_path, cost_log_path)
    else:
        lock = threading.Lock()
        log.info(f"Starting {len(ready)} jobs in parallel: {', '.join(j.name for j, _ in ready)}")

        def _launch(pair):
            job, config = pair
            log.info(f"[{job.name}] Starting job")
            _provision_deploy_launch(job, config, state, state_path, cost_log_path, lock=lock)

        with ThreadPoolExecutor(max_workers=len(ready)) as pool:
            list(pool.map(_launch, ready))


def _provision_deploy_launch(
    job: PodState,
    config: JobConfig,
    state: RunState,
    state_path: str,
    cost_log_path: str,
    lock: threading.Lock | None = None,
) -> None:
    cm = lock or contextlib.nullcontext()
    is_reuse = config.keep_pod_alive and job.pod_id is not None

    # Provision
    provision_pod(config, job)
    if job.status == "failed":
        _propagate_failure(job, state)
        send_notification(config.notifications, "job_failed",
                          job=job.name, error=job.error)
        return

    with cm:
        save_state(state, state_path)
        append_cost_event(cost_log_path, {
            "event": "pod_started", "job": job.name, "pod_id": job.pod_id,
            "gpu_type": job.gpu_type, "cost_per_hour": job.gpu_cost_per_hour,
        })

    # Deploy
    job.status = "deploying"
    with cm:
        save_state(state, state_path)
    if not deploy(config, job, is_reuse=is_reuse):
        job.status = "failed"
        job.error = "deploy failed"
        if job.pod_id:
            teardown_pod(job.pod_id)
        _propagate_failure(job, state)
        send_notification(config.notifications, "job_failed",
                          job=job.name, error=job.error)
        return

    # Launch
    pid = launch(config, job)
    if pid is None:
        job.status = "failed"
        job.error = "launch failed"
        if job.pod_id:
            teardown_pod(job.pod_id)
        _propagate_failure(job, state)
        send_notification(config.notifications, "job_failed",
                          job=job.name, error=job.error)
        return

    job.pid = pid
    job.status = "running"
    job.started_at = job.started_at or time.time()
    job.last_sync_at = time.time()
    with cm:
        save_state(state, state_path)


def _spot_recover(
    job: PodState,
    config: JobConfig,
    state: RunState,
    state_path: str,
    cost_log_path: str,
) -> None:
    if job.provision_attempts >= config.max_provision_attempts:
        job.status = "failed"
        job.error = f"max provision attempts ({config.max_provision_attempts}) exceeded"
        _propagate_failure(job, state)
        send_notification(config.notifications, "job_failed",
                          job=job.name, error=job.error)
        return

    # Teardown dead pod
    if job.pod_id:
        teardown_pod(job.pod_id)
        append_cost_event(cost_log_path, {
            "event": "pod_stopped", "job": job.name, "pod_id": job.pod_id,
            "reason": "spot_interruption", "total_cost_usd": job.cost_usd,
        })

    # Backoff
    backoff = config.backoff_base_seconds * (2 ** (job.provision_attempts - 1))
    log.info(f"[{job.name}] Waiting {backoff}s before re-provisioning")
    time.sleep(backoff)

    # Reset pod info
    job.pod_id = None
    job.ssh_host = None
    job.ssh_port = None
    job.pid = None

    # Re-provision, re-deploy, push synced results, re-launch
    _provision_deploy_launch(job, config, state, state_path, cost_log_path)

    if job.status == "running" and config.sync_paths:
        # Push previously synced results back to pod
        sync_push(config, job)


def _finish_job(
    job: PodState,
    config: JobConfig,
    state: RunState,
    cost_log_path: str,
    status: str,
    error: str | None = None,
) -> None:
    # Final sync
    sync_pull(config, job)

    # Teardown (unless keep_pod_alive)
    if job.pod_id and not (status == "completed" and config.keep_pod_alive):
        teardown_pod(job.pod_id)

    elapsed_hours = 0.0
    if job.started_at:
        elapsed_hours = (time.time() - job.started_at) / 3600

    append_cost_event(cost_log_path, {
        "event": "pod_stopped", "job": job.name, "pod_id": job.pod_id,
        "reason": status, "total_hours": elapsed_hours, "total_cost_usd": job.cost_usd,
    })

    job.status = status
    job.error = error

    if status == "failed":
        _propagate_failure(job, state)


def _update_cost(job: PodState, state: RunState, config: JobConfig, cost_log_path: str) -> None:
    if not job.started_at or job.gpu_cost_per_hour <= 0:
        return
    elapsed_hours = (time.time() - job.started_at) / 3600
    new_cost = elapsed_hours * job.gpu_cost_per_hour
    delta = new_cost - job.cost_usd
    job.cost_usd = new_cost
    state.total_cost_usd += delta

    # Budget threshold notification
    cfg = config
    if (cfg.notifications and cfg.notifications.on_budget_threshold > 0
            and state.budget_usd > 0):
        threshold = cfg.notifications.on_budget_threshold
        if state.total_cost_usd >= state.budget_usd * threshold:
            send_notification(cfg.notifications, "budget_threshold",
                              total_cost=state.total_cost_usd, budget=state.budget_usd)


def _deps_met(job: PodState, state: RunState) -> bool:
    for dep_name in job.depends_on:
        dep = get_job(state, dep_name)
        if not dep or dep.status != "completed":
            return False
    return True


def _deps_failed(job: PodState, state: RunState) -> bool:
    for dep_name in job.depends_on:
        dep = get_job(state, dep_name)
        if dep and dep.status in ("failed", "skipped"):
            return True
    return False


def _propagate_failure(job: PodState, state: RunState) -> None:
    for j in state.jobs:
        if job.name in j.depends_on and j.status == "pending":
            j.status = "skipped"
            j.error = f"dependency '{job.name}' failed"
            log.info(f"[{j.name}] Skipped (dependency '{job.name}' failed)")
            _propagate_failure(j, state)


def _all_done(state: RunState) -> bool:
    return all(j.status in ("completed", "failed", "skipped") for j in state.jobs)


def _teardown_all(
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    state: RunState,
    state_path: str,
    cost_log_path: str,
    reason: str = "shutdown",
) -> None:
    for job in state.jobs:
        if job.status not in ("running", "deploying", "provisioning"):
            continue
        config = config_map.get(job.name)
        if config:
            _finish_job(job, config, state, cost_log_path, "failed", error=reason)
    save_state(state, state_path)


def _graceful_shutdown(
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    state: RunState,
    state_path: str,
    cost_log_path: str,
) -> None:
    log.info("Performing graceful shutdown...")
    for job in state.jobs:
        if job.status != "running":
            continue
        config = config_map.get(job.name)
        if not config:
            continue
        # Final sync
        sync_pull(config, job)
        # Teardown unless keep_pod_alive
        if job.pod_id and not config.keep_pod_alive:
            teardown_pod(job.pod_id)
            job.status = "failed"
            job.error = "interrupted"
        elif config.keep_pod_alive:
            job.status = "failed"
            job.error = "interrupted (pod kept alive)"

        if job.started_at:
            elapsed_hours = (time.time() - job.started_at) / 3600
            append_cost_event(cost_log_path, {
                "event": "pod_stopped", "job": job.name, "pod_id": job.pod_id,
                "reason": "interrupted", "total_hours": elapsed_hours,
                "total_cost_usd": job.cost_usd,
            })
    save_state(state, state_path)


def _print_status(state: RunState) -> None:
    header = f"{'Job':<16} {'Pod':<14} {'GPU':<24} {'Status':<12} {'Elapsed':<10} {'Cost':<8}"
    sep = "─" * len(header)
    print(f"\n{header}\n{sep}")
    for job in state.jobs:
        pod_id = (job.pod_id or "---")[:12]
        gpu = (job.gpu_type or "---")[:22]
        elapsed = "---"
        if job.started_at:
            secs = time.time() - job.started_at
            h, m = int(secs // 3600), int((secs % 3600) // 60)
            elapsed = f"{h}h {m:02d}m"
        cost = f"${job.cost_usd:.2f}" if job.cost_usd > 0 else "---"
        print(f"{job.name:<16} {pod_id:<14} {gpu:<24} {job.status:<12} {elapsed:<10} {cost:<8}")

    if state.budget_usd > 0:
        print(f"\nBudget: ${state.budget_usd:.2f} | Spent: ${state.total_cost_usd:.2f} | "
              f"Remaining: ${state.budget_usd - state.total_cost_usd:.2f}")
