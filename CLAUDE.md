# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cloud Conductor is a lightweight CLI orchestrator for RunPod GPU workloads. It provisions pods, deploys code via rsync, monitors jobs, syncs results, and tears down pods — all driven by a single TOML config file. It is **project-agnostic**: it runs arbitrary shell commands on GPU pods and knows nothing about the specific workloads.

The full specification is in `SPEC.md`. Read it before making architectural decisions.

## Development Setup

```bash
uv sync                                    # install dependencies
uv run conductor run --config jobs.toml    # run the orchestrator
uv run pytest                              # run all tests
uv run pytest tests/test_config.py -x      # run a single test file, stop on first failure
```

Python 3.12+. Uses `uv` for package management (never pip/conda).

## Architecture

**Package**: `src/conductor/` — CLI tool using argparse with subcommands (`run`, `status`, `sync`, `teardown`, `dry-run`, `validate`, `logs`, `ssh`, `report`).

**Lifecycle**: Provision → Deploy → Launch → Monitor → Teardown (with spot recovery loop back to Provision).

Key modules:
- `cli.py` — argparse entry point, dispatches subcommands
- `config.py` — TOML loading, merges `[global]` defaults with per-`[[jobs]]` overrides into `JobConfig` dataclasses
- `provisioner.py` — RunPod pod creation with GPU fallback list or auto-cheapest selection
- `deployer.py` — rsync code + run setup_command (or skip for prebuilt Docker images)
- `runner.py` — launches `run_command` via `nohup` over SSH, queries live GPU/CPU utilization
- `monitor.py` — main loop: status checks, periodic sync, spot recovery, cost tracking, dependency resolution
- `state.py` — `PodState` dataclass, JSON state file + JSONL cost log I/O
- `ssh.py` — SSH/rsync helpers (all connections use `-o StrictHostKeyChecking=no` for ephemeral pods)
- `gpu_pricing.py` — queries RunPod API for GPU prices, caches 5 min
- `notify.py` — notification dispatch (terminal-notifier, pushover, or custom command)
- `validator.py` — pre-flight checks (SSH keys, paths, GPU IDs, dependency cycles)

**State files** (created at runtime, not committed):
- `.conductor_state.json` — current pod/job state (source of truth for `conductor status`)
- `.conductor_cost_log.jsonl` — append-only cost events for `conductor report`

## Dependencies

Intentionally minimal:
- `runpod` — RunPod Python SDK for pod CRUD
- `httpx` — HTTP client (only for pushover notifications; optional)
- stdlib only for everything else (no click, no rich)
- System: `ssh`, `rsync`

## Key Design Decisions

- `run_command` is an opaque user-supplied shell string — the conductor never parses it
- Per-job overrides: any `[global]` config field can be overridden in a `[[jobs]]` entry
- `depends_on` supports job ordering with cycle detection; failed dependencies cascade as `skipped`
- `keep_pod_alive` enables pod reuse across runs (re-syncs code, skips setup)
- `auto_select_cheapest_gpu` queries RunPod API at provision time instead of using manual fallback lists

## RunPod Operational Notes

- Most RunPod images lack rsync — deploy step must `apt-get install -y rsync` first
- Use `</dev/null &` with nohup over SSH to prevent hangs
- Pod SSH ports are dynamic (random public port → container port 22) — query API to discover
- Always sync results before teardown — data is lost once a pod is terminated
- Check both pod status (eviction) AND process status (crash) separately
