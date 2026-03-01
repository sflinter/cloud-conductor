# Copyright (c) 2026 Steve Flinter. MIT License.
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from conductor.config import load_config
from conductor.gpu_pricing import get_gpu_price, select_cheapest_gpus
from conductor.monitor import run_lifecycle
from conductor.provisioner import check_pod_exists, teardown_pod
from conductor.runner import get_log_path
from conductor.ssh import ssh_exec, ssh_interactive, tail_remote_log
from conductor.state import (
    RunState, append_cost_event, get_job, init_state, load_state,
    read_cost_log, save_state,
)
from conductor.syncer import sync_pull
from conductor.validator import validate


def main(argv=None):
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--config", default=argparse.SUPPRESS, help="Path to TOML config file")

    parser = argparse.ArgumentParser(prog="conductor", description="Cloud Conductor — RunPod GPU orchestrator")
    parser.add_argument("--config", default="jobs.toml", help="Path to TOML config file")
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", parents=[parent], help="Full lifecycle: provision → deploy → launch → monitor → teardown")
    p_run.add_argument("--jobs", help="Comma-separated job names to run")
    p_run.add_argument("--budget", type=float, default=0.0, help="Override global budget")

    # status
    sub.add_parser("status", parents=[parent], help="Show current job status")

    # sync
    p_sync = sub.add_parser("sync", parents=[parent], help="Force sync results from all running pods")
    p_sync.add_argument("--jobs", help="Comma-separated job names to sync")

    # teardown
    p_td = sub.add_parser("teardown", parents=[parent], help="Terminate all tracked pods")
    p_td.add_argument("--jobs", help="Comma-separated job names to teardown")
    p_td.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    # dry-run
    sub.add_parser("dry-run", parents=[parent], help="Show what would be provisioned")

    # validate
    sub.add_parser("validate", parents=[parent], help="Pre-flight config validation")

    # logs
    p_logs = sub.add_parser("logs", parents=[parent], help="View remote job log")
    p_logs.add_argument("job_name", help="Job name")
    p_logs.add_argument("--tail", action="store_true", help="Stream log in real time")

    # ssh
    p_ssh = sub.add_parser("ssh", parents=[parent], help="SSH into a job's pod")
    p_ssh.add_argument("job_name", help="Job name")

    # report
    sub.add_parser("report", parents=[parent], help="Cost report from historical data")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        sys.exit(1)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    try:
        dispatch = {
            "run": cmd_run,
            "status": cmd_status,
            "sync": cmd_sync,
            "teardown": cmd_teardown,
            "dry-run": cmd_dry_run,
            "validate": cmd_validate,
            "logs": cmd_logs,
            "ssh": cmd_ssh,
            "report": cmd_report,
        }
        dispatch[args.command](args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _ensure_api_key():
    """Ensure RunPod API key is set on the SDK (env var may not be picked up after import)."""
    api_key = os.environ.get("RUNPOD_API_KEY", "")
    if api_key:
        import runpod
        runpod.api_key = api_key


def cmd_run(args):
    _ensure_api_key()
    job_names = args.jobs.split(",") if args.jobs else None
    configs = load_config(args.config, job_names=job_names)

    # Validate first
    result = validate(configs, check_gpu=False)
    if not result.ok:
        print("Validation errors:")
        for err in result.errors:
            print(f"  ✗ {err}")
        sys.exit(1)
    for w in result.warnings:
        print(f"  ⚠ {w}")

    state_path = configs[0].state_file
    cost_log_path = configs[0].cost_log_file

    # Load or init state
    if os.path.exists(state_path):
        state = load_state(state_path)
        # Merge in any new jobs not in existing state
        existing = {j.name for j in state.jobs}
        for cfg in configs:
            if cfg.name not in existing:
                from conductor.state import PodState
                state.jobs.append(PodState(
                    name=cfg.name,
                    depends_on=list(cfg.depends_on),
                    job_budget_usd=cfg.job_budget_usd,
                ))
    else:
        state = init_state(configs, budget_usd=args.budget or configs[0].budget_usd)

    run_lifecycle(configs, state, state_path, cost_log_path, budget_override=args.budget)
    print("\nAll jobs finished.")


def cmd_status(args):
    configs = load_config(args.config)
    state_path = configs[0].state_file
    if not os.path.exists(state_path):
        print("No state file found. Run 'conductor run' first.")
        return

    state = load_state(state_path)

    header = f"{'Job':<16} {'Pod':<14} {'GPU':<24} {'Status':<12} {'Elapsed':<10} {'Cost':<8}"
    sep = "─" * len(header)
    print(f"{header}\n{sep}")
    for job in state.jobs:
        pod_id = (job.pod_id or "---")[:12]
        gpu = (job.gpu_type or "---")[:22]
        elapsed = "---"
        if job.started_at:
            secs = time.time() - job.started_at
            h, m = int(secs // 3600), int((secs % 3600) // 60)
            elapsed = f"{h}h {m:02d}m"
        cost = f"${job.cost_usd:.2f}" if job.cost_usd > 0 else "---"
        info = job.error or ""
        if job.depends_on and job.status == "pending":
            info = f"waiting on {', '.join(job.depends_on)}"
        print(f"{job.name:<16} {pod_id:<14} {gpu:<24} {job.status:<12} {elapsed:<10} {cost:<8} {info}")

    if state.budget_usd > 0:
        print(f"\nBudget: ${state.budget_usd:.2f} | Spent: ${state.total_cost_usd:.2f} | "
              f"Remaining: ${max(0, state.budget_usd - state.total_cost_usd):.2f}")


def cmd_sync(args):
    job_names = args.jobs.split(",") if args.jobs else None
    configs = load_config(args.config, job_names=job_names)
    config_map = {c.name: c for c in configs}
    state_path = configs[0].state_file

    if not os.path.exists(state_path):
        print("No state file found.")
        return

    state = load_state(state_path)
    for job in state.jobs:
        if job.status != "running" or not job.ssh_host:
            continue
        config = config_map.get(job.name)
        if not config:
            continue
        print(f"Syncing {job.name}...")
        sync_pull(config, job)
        job.last_sync_at = time.time()

    save_state(state, state_path)
    print("Sync complete.")


def cmd_teardown(args):
    job_names = args.jobs.split(",") if args.jobs else None
    configs = load_config(args.config, job_names=job_names)
    state_path = configs[0].state_file

    if not os.path.exists(state_path):
        print("No state file found.")
        return

    state = load_state(state_path)
    pods = [(j.name, j.pod_id) for j in state.jobs if j.pod_id]
    if not pods:
        print("No active pods to teardown.")
        return

    if not args.force:
        print(f"About to teardown {len(pods)} pod(s):")
        for name, pid in pods:
            print(f"  {name}: {pid}")
        answer = input("Continue? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    for job in state.jobs:
        if not job.pod_id:
            continue
        print(f"Tearing down {job.name} ({job.pod_id})...")
        teardown_pod(job.pod_id)
        job.status = "failed"
        job.error = "manual teardown"

    save_state(state, state_path)
    print("All pods terminated.")


def cmd_dry_run(args):
    configs = load_config(args.config)

    print("=== Dry Run ===\n")
    for cfg in configs:
        print(f"Job: {cfg.name}")
        print(f"  GPU: {cfg.gpu_type_id or '(auto-select cheapest)'}")
        if cfg.gpu_type_ids_fallback:
            print(f"  Fallback: {', '.join(cfg.gpu_type_ids_fallback)}")
        print(f"  Image: {cfg.image_name}")
        print(f"  Disk: {cfg.container_disk_in_gb} GB")
        print(f"  Deploy: {cfg.deploy_method}")
        if cfg.depends_on:
            print(f"  Depends on: {', '.join(cfg.depends_on)}")

        # Try to get price estimate
        if cfg.auto_select_cheapest_gpu:
            try:
                gpus = select_cheapest_gpus(cfg.gpu_min_vram_gb, cfg.cloud_type)
                if gpus:
                    print(f"  Cheapest GPU: {gpus[0].id} (${gpus[0].community_price:.2f}/hr)")
            except Exception:
                print("  (Could not fetch GPU prices)")
        elif cfg.gpu_type_id:
            try:
                price = get_gpu_price(cfg.gpu_type_id, cfg.cloud_type)
                if price > 0:
                    print(f"  Est. cost: ${price:.2f}/hr")
            except Exception:
                pass

        print()

    if configs[0].budget_usd > 0:
        print(f"Budget: ${configs[0].budget_usd:.2f}")


def cmd_validate(args):
    configs = load_config(args.config)
    result = validate(configs, check_gpu=False)

    if result.errors:
        print("Errors:")
        for err in result.errors:
            print(f"  ✗ {err}")
    if result.warnings:
        print("Warnings:")
        for w in result.warnings:
            print(f"  ⚠ {w}")

    if result.ok:
        print(f"Config OK ({len(configs)} job(s))")
    else:
        sys.exit(1)


def cmd_logs(args):
    configs = load_config(args.config)
    config_map = {c.name: c for c in configs}
    state_path = configs[0].state_file

    if not os.path.exists(state_path):
        print("No state file found.")
        sys.exit(1)

    config = config_map.get(args.job_name)
    if not config:
        print(f"Unknown job: {args.job_name}")
        sys.exit(1)

    state = load_state(state_path)
    job = get_job(state, args.job_name)
    if not job or not job.ssh_host:
        print(f"Job '{args.job_name}' has no active pod.")
        sys.exit(1)

    log_path = get_log_path(config)

    if args.tail:
        tail_remote_log(job.ssh_host, job.ssh_port, config.ssh_key_path, log_path)
    else:
        result = ssh_exec(job.ssh_host, job.ssh_port, config.ssh_key_path,
                          f"cat {log_path}")
        if result.returncode == 0:
            print(result.stdout)
        else:
            print(f"Could not read log: {result.stderr}")
            sys.exit(1)


def cmd_ssh(args):
    configs = load_config(args.config)
    state_path = configs[0].state_file

    if not os.path.exists(state_path):
        print("No state file found.")
        sys.exit(1)

    config_map = {c.name: c for c in configs}
    config = config_map.get(args.job_name)
    if not config:
        print(f"Unknown job: {args.job_name}")
        sys.exit(1)

    state = load_state(state_path)
    job = get_job(state, args.job_name)
    if not job or not job.ssh_host:
        print(f"Job '{args.job_name}' has no active pod.")
        sys.exit(1)

    ssh_interactive(job.ssh_host, job.ssh_port, config.ssh_key_path)


def cmd_report(args):
    configs = load_config(args.config)
    cost_log_path = configs[0].cost_log_file

    events = read_cost_log(cost_log_path)
    if not events:
        print("No cost data found.")
        return

    print("=== Cost Report ===\n")

    # Find earliest timestamp
    first_ts = min(e.get("ts", 0) for e in events)
    if first_ts:
        from datetime import datetime
        print(f"Run started: {datetime.fromtimestamp(first_ts).strftime('%Y-%m-%d %H:%M')}\n")

    # Aggregate by job
    job_data: dict[str, list[dict]] = {}
    for e in events:
        job_name = e.get("job", "unknown")
        job_data.setdefault(job_name, []).append(e)

    header = f"{'Job':<16} {'GPU':<24} {'Status':<12} {'Duration':<12} {'Cost':<8}"
    sep = "─" * len(header)
    print(f"{header}\n{sep}")

    total_cost = 0.0
    gpu_summary: dict[str, dict] = {}

    for job_name, job_events in job_data.items():
        # Pair up start/stop events
        starts = [e for e in job_events if e.get("event") == "pod_started"]
        stops = [e for e in job_events if e.get("event") == "pod_stopped"]

        for stop in stops:
            gpu = stop.get("gpu_type", "unknown")
            hours = stop.get("total_hours", 0)
            cost = stop.get("total_cost_usd", 0)
            reason = stop.get("reason", "unknown")
            total_cost += cost

            h, m = int(hours), int((hours % 1) * 60)
            duration = f"{h}h {m:02d}m"
            print(f"{job_name:<16} {gpu:<24} {reason:<12} {duration:<12} ${cost:<7.2f}")

            gpu_summary.setdefault(gpu, {"hours": 0.0, "cost": 0.0})
            gpu_summary[gpu]["hours"] += hours
            gpu_summary[gpu]["cost"] += cost

        # Handle active (no stop yet)
        if starts and not stops:
            start = starts[-1]
            gpu = start.get("gpu_type", "unknown")
            cost_per_hr = start.get("cost_per_hour", 0)
            hours = (time.time() - start["ts"]) / 3600
            cost = hours * cost_per_hr
            total_cost += cost

            h, m = int(hours), int((hours % 1) * 60)
            duration = f"{h}h {m:02d}m"
            print(f"{job_name:<16} {gpu:<24} {'running':<12} {duration:<12} ${cost:<7.2f}")

            gpu_summary.setdefault(gpu, {"hours": 0.0, "cost": 0.0})
            gpu_summary[gpu]["hours"] += hours
            gpu_summary[gpu]["cost"] += cost

    print(f"{sep}")
    print(f"{'Total':<56} ${total_cost:.2f}")

    if gpu_summary:
        print(f"\nBy GPU type:")
        for gpu, data in sorted(gpu_summary.items()):
            h, m = int(data["hours"]), int((data["hours"] % 1) * 60)
            print(f"  {gpu}:  {h}h {m:02d}m   ${data['cost']:.2f}")

    budget = configs[0].budget_usd
    if budget > 0:
        print(f"\nBudget: ${budget:.2f} | Spent: ${total_cost:.2f} | Remaining: ${max(0, budget - total_cost):.2f}")


if __name__ == "__main__":
    main()
