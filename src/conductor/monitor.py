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
from conductor.errors import ConductorError, describe
from conductor.notify import send_notification
from conductor.providers import CloudProvider, get_provider
from conductor.provisioner import check_pod_exists, provision_pod, refresh_ssh_info, teardown_pod
from conductor.registry import PodRecord, add_pod as registry_add, update_pod as registry_update
from conductor.runner import LOG_TAIL_LINES, fetch_log_tail, get_log_path, get_utilization, is_alive, launch
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
    provider: CloudProvider | None = None,
) -> RunState:
    if budget_override > 0:
        state.budget_usd = budget_override

    # Create provider if not supplied (backward compat)
    if provider is None:
        provider = get_provider(configs[0].provider)

    config_map = {c.name: c for c in configs}
    provider_map = {c.name: get_provider(c.provider) for c in configs}
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
        _start_unblocked_jobs(configs, config_map, provider_map, state, state_path, cost_log_path)
        save_state(state, state_path)

        while not _all_done(state) and not shutdown["requested"]:
            _monitor_tick(configs, config_map, provider_map, state, state_path, cost_log_path)
            save_state(state, state_path)

            if shutdown["requested"]:
                break

            # Check global budget
            if state.budget_usd > 0 and state.total_cost_usd >= state.budget_usd:
                log.warning("Global budget exceeded! Tearing down all pods.")
                _teardown_all(configs, config_map, provider_map, state, state_path, cost_log_path, reason="budget_exceeded")
                break

            _print_status(state)
            time.sleep(POLL_INTERVAL)

        if shutdown["requested"]:
            _graceful_shutdown(configs, config_map, provider_map, state, state_path, cost_log_path)
    except BaseException as e:
        _handle_crash(e, configs, config_map, provider_map, state, state_path, cost_log_path)
        raise
    finally:
        signal.signal(signal.SIGINT, prev_handler)
        save_state(state, state_path)

    return state


def _handle_crash(
    exc: BaseException,
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    provider_map: dict[str, CloudProvider],
    state: RunState,
    state_path: str,
    cost_log_path: str,
) -> None:
    """Last line of defence: never let the orchestrator die with pods unaccounted for."""
    reason = describe(exc)
    log.error(f"Orchestrator exiting abnormally: {reason}", exc_info=not isinstance(exc, KeyboardInterrupt))
    try:
        save_state(state, state_path)
    except Exception as save_err:
        log.error(f"Could not save state: {save_err}")

    live = live_pods(state)
    print_live_pods(live, header=f"Orchestrator exiting abnormally ({reason}). Live pods:")

    notified = set()
    for job in live:
        cfg = config_map.get(job.name)
        if cfg and cfg.notifications and id(cfg.notifications) not in notified:
            notified.add(id(cfg.notifications))
            send_notification(cfg.notifications, "orchestrator_crashed",
                              error=reason, pods=[(j.name, j.pod_id) for j in live])

    teardown = any(config_map[j.name].teardown_on_crash for j in live if j.name in config_map)
    if live and teardown:
        log.warning("teardown_on_crash is set: tearing down all live pods")
        _teardown_all(configs, config_map, provider_map, state, state_path, cost_log_path,
                      reason=f"orchestrator crashed: {reason}")
    elif live:
        log.warning("Pods are still running and billing. Resume with 'conductor run' "
                    "or terminate with 'conductor teardown'.")


def live_pods(state: RunState) -> list[PodState]:
    """Jobs whose pod may still exist on the provider."""
    return [j for j in state.jobs
            if j.pod_id and j.status in ("provisioning", "deploying", "running")]


def print_live_pods(jobs: list[PodState], header: str = "Live pods:") -> None:
    if not jobs:
        print("No live pods.", file=sys.stderr)
        return
    print(header, file=sys.stderr)
    for j in jobs:
        print(f"  {j.name}: {j.pod_id} ({j.status}, {j.gpu_type or '?'}, "
              f"ssh {j.ssh_host or '?'}:{j.ssh_port or '?'})", file=sys.stderr)


def _monitor_tick(
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    provider_map: dict[str, CloudProvider],
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

        prov = provider_map.get(job.name)
        try:
            _tick_job(job, config, prov, state, state_path, cost_log_path)
        except ConductorError as e:
            # Transient infrastructure error: keep the job, retry next tick
            log.warning(f"[{job.name}] Monitor tick failed, will retry: {e}")
        except Exception as e:
            log.exception(f"[{job.name}] Unexpected error while monitoring")
            _fail_job_safely(prov, job, config, state, cost_log_path,
                             error=f"monitor error: {describe(e)}")

    # Start newly unblocked jobs
    _start_unblocked_jobs(configs, config_map, provider_map, state, state_path, cost_log_path)


def _fail_job_safely(
    provider: CloudProvider,
    job: PodState,
    config: JobConfig,
    state: RunState,
    cost_log_path: str,
    error: str,
) -> None:
    """Mark a job failed and try to release its pod, never raising."""
    try:
        _finish_job(provider, job, config, state, cost_log_path, "failed", error=error)
    except Exception as e:
        log.error(f"[{job.name}] Could not tear down pod {job.pod_id}: {describe(e)}")
        job.status = "failed"
        job.error = f"{error} (pod {job.pod_id} may still be running)"
        _propagate_failure(job, state)
    send_notification(config.notifications, "job_failed", job=job.name, error=job.error)


def _tick_job(
    job: PodState,
    config: JobConfig,
    prov: CloudProvider,
    state: RunState,
    state_path: str,
    cost_log_path: str,
) -> None:
    # Update cost
    _update_cost(job, state, config, cost_log_path)

    # Check per-job budget
    if config.job_budget_usd > 0 and job.cost_usd >= config.job_budget_usd:
        log.warning(f"[{job.name}] Per-job budget exceeded (${job.cost_usd:.2f} >= ${config.job_budget_usd:.2f})")
        _finish_job(prov, job, config, state, cost_log_path, "failed", error="job budget exceeded")
        send_notification(config.notifications, "job_budget_exceeded",
                          job=job.name, cost_usd=job.cost_usd)
        return

    # Check the process: exit file first (immediate, real exit code), then pid
    status = is_alive(job, config.ssh_key_path, config.remote_project_dir)

    if status.exited and status.exit_code is not None:
        _handle_exit(prov, job, config, state, cost_log_path, status.exit_code)
        return

    if status.running:
        job.idle_since = None
        job.gone_confirmations = 0

        # GPU stall detection
        if config.stall_timeout_minutes > 0:
            util = get_utilization(job, config.ssh_key_path)
            gpu_util = util.get("gpu_util") if util else None
            if gpu_util is not None and gpu_util < config.stall_gpu_threshold:
                if job.stalled_since is None:
                    job.stalled_since = time.time()
                    log.info(f"[{job.name}] GPU util {gpu_util}% < {config.stall_gpu_threshold}%, starting stall timer")
                elif (time.time() - job.stalled_since) / 60 >= config.stall_timeout_minutes:
                    log.warning(f"[{job.name}] GPU stall timeout reached ({config.stall_timeout_minutes}m)")
                    _finish_job(prov, job, config, state, cost_log_path, "failed", error="gpu stall detected")
                    send_notification(config.notifications, "job_failed",
                                      job=job.name, error="gpu stall detected")
                    return
            else:
                if job.stalled_since is not None:
                    log.info(f"[{job.name}] GPU active again, resetting stall timer")
                job.stalled_since = None

        # Periodic sync
        if config.sync_paths and job.last_sync_at:
            elapsed_min = (time.time() - job.last_sync_at) / 60
            if elapsed_min >= config.sync_interval_minutes:
                sync_pull(config, job)
                job.last_sync_at = time.time()
    elif status.exited:
        # Process gone without an exit file (killed, container restarted, pid
        # reused). Fall back to the idle timer if the pod is still there.
        exists = check_pod_exists(prov, job.pod_id) if job.pod_id else False
        if exists is True:
            job.gone_confirmations = 0
            if job.idle_since is None:
                job.idle_since = time.time()
                log.warning(f"[{job.name}] Process not running ({status.detail}), starting idle timer")
            elif (time.time() - job.idle_since) / 60 >= config.idle_timeout_minutes:
                log.info(f"[{job.name}] Idle timeout reached, marking completed")
                _finish_job(prov, job, config, state, cost_log_path, "completed")
                send_notification(config.notifications, "job_complete",
                                  job=job.name, cost_usd=job.cost_usd,
                                  gpu_type=job.gpu_type)
        elif _pod_gone_confirmed(job, config, exists):
            log.warning(f"[{job.name}] Spot interruption detected")
            send_notification(config.notifications, "spot_recovery", job=job.name)
            _spot_recover(prov, job, config, state, state_path, cost_log_path)
    elif status.unknown:
        # SSH unreachable. Only a provider answer of "gone" on several
        # consecutive ticks means a spot interruption; an API error or a
        # live pod with a dead SSH link is not one.
        log.info(f"[{job.name}] Process state unknown ({status.detail})")
        exists = check_pod_exists(prov, job.pod_id) if job.pod_id else None
        if exists is True:
            job.gone_confirmations = 0
            # The port may have been remapped: refresh before the next tick
            if refresh_ssh_info(prov, job):
                log.info(f"[{job.name}] Pod alive; SSH info refreshed to "
                         f"{job.ssh_host}:{job.ssh_port}")
        elif _pod_gone_confirmed(job, config, exists):
            log.warning(f"[{job.name}] Pod gone (spot interruption)")
            send_notification(config.notifications, "spot_recovery", job=job.name)
            _spot_recover(prov, job, config, state, state_path, cost_log_path)


def _pod_gone_confirmed(job: PodState, config: JobConfig, exists: bool | None) -> bool:
    """Count consecutive provider answers of 'pod gone'. None (could not
    tell) neither counts nor resets."""
    if exists is None:
        log.warning(f"[{job.name}] Could not determine whether pod {job.pod_id} exists; "
                    f"will retry next tick")
        return False
    if exists:
        job.gone_confirmations = 0
        return False
    job.gone_confirmations += 1
    needed = max(1, config.recovery_confirmations)
    if job.gone_confirmations < needed:
        log.warning(f"[{job.name}] Provider reports pod {job.pod_id} gone "
                    f"({job.gone_confirmations}/{needed} confirmations)")
        return False
    return True


def _handle_exit(
    provider: CloudProvider,
    job: PodState,
    config: JobConfig,
    state: RunState,
    cost_log_path: str,
    exit_code: int,
) -> None:
    job.exit_code = exit_code
    if exit_code == 0:
        log.info(f"[{job.name}] Job exited with code 0, marking completed")
        _finish_job(provider, job, config, state, cost_log_path, "completed")
        send_notification(config.notifications, "job_complete",
                          job=job.name, cost_usd=job.cost_usd, gpu_type=job.gpu_type)
        return

    log.warning(f"[{job.name}] Job exited with code {exit_code}")
    tail = fetch_log_tail(job, config.ssh_key_path, config.remote_project_dir)
    error = f"exit code {exit_code}"
    if tail:
        error += f"\n--- last {LOG_TAIL_LINES} log lines ---\n{tail}"
    _finish_job(provider, job, config, state, cost_log_path, "failed", error=error)
    send_notification(config.notifications, "job_failed",
                      job=job.name, error=f"exit code {exit_code}", cost_usd=job.cost_usd)


def _start_unblocked_jobs(
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    provider_map: dict[str, CloudProvider],
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
        prov = provider_map.get(job.name)
        log.info(f"[{job.name}] Starting job")
        _provision_deploy_launch(prov, job, config, state, state_path, cost_log_path)
    else:
        lock = threading.Lock()
        log.info(f"Starting {len(ready)} jobs in parallel: {', '.join(j.name for j, _ in ready)}")

        def _launch(pair):
            job, config = pair
            prov = provider_map.get(job.name)
            log.info(f"[{job.name}] Starting job")
            _provision_deploy_launch(prov, job, config, state, state_path, cost_log_path, lock=lock)

        with ThreadPoolExecutor(max_workers=len(ready)) as pool:
            list(pool.map(_launch, ready))


def _provision_deploy_launch(
    provider: CloudProvider,
    job: PodState,
    config: JobConfig,
    state: RunState,
    state_path: str,
    cost_log_path: str,
    lock: threading.Lock | None = None,
) -> None:
    """Run provision → deploy → launch for one job. Never raises: any error
    marks the job failed with job.error set, keeping the pod ID in state so
    it can be torn down or resumed."""
    cm = lock or contextlib.nullcontext()
    try:
        _pipeline(provider, job, config, state, state_path, cost_log_path, cm)
    except Exception as e:
        if isinstance(e, ConductorError):
            log.error(f"[{job.name}] {e}")
        else:
            log.exception(f"[{job.name}] Unexpected error during {job.status}")
        job.error = f"{job.status} failed: {describe(e)}"
        job.status = "failed"
        if job.pod_id:
            log.warning(f"[{job.name}] Pod {job.pod_id} may still be running; tearing down")
            try:
                teardown_pod(provider, job.pod_id)
                registry_update(job.pod_id, status="terminated", cost_usd=job.cost_usd)
            except Exception as td:
                log.error(f"[{job.name}] Teardown of {job.pod_id} failed: {describe(td)}")
                job.error += f" (pod {job.pod_id} may still be running)"
        _propagate_failure(job, state)
        send_notification(config.notifications, "job_failed", job=job.name, error=job.error)
    finally:
        with cm:
            save_state(state, state_path)


def _pipeline(
    provider: CloudProvider,
    job: PodState,
    config: JobConfig,
    state: RunState,
    state_path: str,
    cost_log_path: str,
    cm,
) -> None:
    is_reuse = config.keep_pod_alive and job.pod_id is not None

    # Terminate any existing pod before provisioning a replacement
    if job.pod_id and not is_reuse:
        try:
            teardown_pod(provider, job.pod_id)
            registry_update(job.pod_id, status="terminated")
        except Exception:
            pass

    # Provision
    provision_pod(provider, config, job)
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

    # Register in global pod registry
    try:
        registry_add(PodRecord(
            pod_id=job.pod_id,
            name=job.name,
            gpu_type=job.gpu_type or "",
            gpu_cost_per_hour=job.gpu_cost_per_hour,
            ssh_host=job.ssh_host,
            ssh_port=job.ssh_port,
            ssh_key_path=config.ssh_key_path,
            image_name=config.image_name,
            status="running",
            created_at=job.started_at or time.time(),
            source="declarative",
            provider=config.provider,
            remote_project_dir=config.remote_project_dir,
            job_name=job.name,
        ))
    except Exception:
        pass  # registry is best-effort

    # Deploy
    job.status = "deploying"
    with cm:
        save_state(state, state_path)
    if not deploy(config, job, is_reuse=is_reuse):
        job.status = "failed"
        job.error = "deploy failed"
        if job.pod_id:
            teardown_pod(provider, job.pod_id)
        _propagate_failure(job, state)
        send_notification(config.notifications, "job_failed",
                          job=job.name, error=job.error)
        return

    # Launch
    launched = launch(config, job)
    if launched is None:
        job.status = "failed"
        job.error = "launch failed"
        if job.pod_id:
            teardown_pod(provider, job.pod_id)
        _propagate_failure(job, state)
        send_notification(config.notifications, "job_failed",
                          job=job.name, error=job.error)
        return

    job.pid = launched.pid
    job.pid_start = launched.pid_start
    job.exit_code = None
    job.status = "running"
    job.started_at = job.started_at or time.time()
    job.last_sync_at = time.time()
    with cm:
        save_state(state, state_path)


def _spot_recover(
    provider: CloudProvider,
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
        teardown_pod(provider, job.pod_id)
        registry_update(job.pod_id, status="terminated", cost_usd=job.cost_usd)
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
    job.pid_start = None
    job.gone_confirmations = 0

    # Re-provision, re-deploy, push synced results, re-launch
    _provision_deploy_launch(provider, job, config, state, state_path, cost_log_path)

    if job.status == "running" and config.sync_paths:
        # Push previously synced results back to pod
        sync_push(config, job)


def _finish_job(
    provider: CloudProvider,
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
    pod_kept_alive = status == "completed" and config.keep_pod_alive
    if job.pod_id and not pod_kept_alive:
        teardown_pod(provider, job.pod_id)

    elapsed_hours = 0.0
    if job.started_at:
        elapsed_hours = (time.time() - job.started_at) / 3600

    append_cost_event(cost_log_path, {
        "event": "pod_stopped", "job": job.name, "pod_id": job.pod_id,
        "reason": status, "total_hours": elapsed_hours, "total_cost_usd": job.cost_usd,
    })

    job.status = status
    job.error = error

    # Update global registry
    if job.pod_id:
        try:
            reg_status = "running" if pod_kept_alive else "terminated"
            registry_update(job.pod_id, status=reg_status, cost_usd=job.cost_usd)
        except Exception:
            pass

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
    provider_map: dict[str, CloudProvider],
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
            prov = provider_map.get(job.name)
            _finish_job(prov, job, config, state, cost_log_path, "failed", error=reason)
    save_state(state, state_path)


def _graceful_shutdown(
    configs: list[JobConfig],
    config_map: dict[str, JobConfig],
    provider_map: dict[str, CloudProvider],
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
        prov = provider_map.get(job.name)
        # Final sync
        sync_pull(config, job)
        # Teardown unless keep_pod_alive
        if job.pod_id and not config.keep_pod_alive:
            teardown_pod(prov, job.pod_id)
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
    header = f"{'Job':<16} {'Pod':<14} {'GPU':<24} {'Status':<12} {'Exit':<5} {'$/hr':<8} {'Elapsed':<10} {'Cost':<8}"
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
        rate = f"${job.gpu_cost_per_hour:.2f}" if job.gpu_cost_per_hour > 0 else "---"
        exit_code = "---" if job.exit_code is None else str(job.exit_code)
        print(f"{job.name:<16} {pod_id:<14} {gpu:<24} {job.status:<12} {exit_code:<5} {rate:<8} {elapsed:<10} {cost:<8}")

    if state.budget_usd > 0:
        print(f"\nBudget: ${state.budget_usd:.2f} | Spent: ${state.total_cost_usd:.2f} | "
              f"Remaining: ${state.budget_usd - state.total_cost_usd:.2f}")
