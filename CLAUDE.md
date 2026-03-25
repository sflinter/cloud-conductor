# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cloud Conductor is a lightweight CLI orchestrator for RunPod GPU workloads. It supports two modes: **imperative** (`conductor pod` commands for direct pod management without any config file) and **declarative** (`conductor run -f jobs.toml` for batch orchestration). It is **project-agnostic**: it runs arbitrary shell commands on GPU pods and knows nothing about the specific workloads.

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

**Package**: `src/conductor/` — CLI tool using argparse with subcommands.

**Two modes**:
- **Imperative**: `conductor pod provision/deploy/exec/ssh/teardown` — direct pod management, no config file needed
- **Declarative**: `conductor run -f jobs.toml` — batch lifecycle with dependency resolution

**Lifecycle** (declarative): Provision → Deploy → Launch → Monitor → Teardown (with spot recovery loop back to Provision).

Key modules:
- `cli.py` — argparse entry point with `pod` subcommand group (imperative) and traditional subcommands (declarative)
- `config.py` — TOML loading, merges `[global]` defaults with per-`[[jobs]]` overrides into `JobConfig` dataclasses
- `registry.py` — global pod registry (`~/.conductor/pods.json`), tracks all pods regardless of creation mode
- `provisioner.py` — RunPod pod creation; `provision_pod_direct()` for imperative use, `provision_pod()` wrapper for declarative
- `deployer.py` — `deploy_direct()` for imperative rsync, `deploy()` wrapper for declarative
- `runner.py` — `launch_direct()` and `exec_foreground()` for imperative, `launch()` wrapper for declarative
- `monitor.py` — main loop: status checks, periodic sync, spot recovery, cost tracking, GPU stall detection, dependency resolution
- `state.py` — `PodState` dataclass (includes `idle_since`, `stalled_since`), JSON state file + JSONL cost log I/O
- `ssh.py` — SSH/rsync helpers (all connections use `-o StrictHostKeyChecking=no` for ephemeral pods)
- `gpu_pricing.py` — queries RunPod API for GPU prices, caches 5 min
- `notify.py` — notification dispatch (terminal-notifier, pushover, or custom command)
- `validator.py` — pre-flight checks (SSH keys, paths, GPU IDs, dependency cycles)

**State files** (created at runtime, not committed):
- `~/.conductor/pods.json` — global pod registry (imperative + declarative pods)
- `.conductor_state.json` — per-project batch run state (source of truth for declarative `conductor status`)
- `.conductor_cost_log.jsonl` — append-only cost events for `conductor report`

## Dependencies

Intentionally minimal:
- `runpod` — RunPod Python SDK for pod CRUD
- `httpx` — HTTP client (only for pushover notifications; optional)
- stdlib only for everything else (no click, no rich)
- System: `ssh`, `rsync`

## Key Design Decisions

- **Dual-mode CLI**: imperative `conductor pod` commands for ad-hoc work, declarative `conductor run -f` for batch orchestration
- Core operations (`provision_pod_direct`, `deploy_direct`, `launch_direct`) are decoupled from config — imperative commands use them directly
- Global pod registry at `~/.conductor/pods.json` tracks all pods; declarative runs also register here
- `run_command` is an opaque user-supplied shell string — the conductor never parses it
- Per-job overrides: any `[global]` config field can be overridden in a `[[jobs]]` entry
- `depends_on` supports job ordering with cycle detection; failed dependencies cascade as `skipped`
- `keep_pod_alive` enables pod reuse across runs (re-syncs code, skips setup)
- `auto_select_cheapest_gpu` queries RunPod API at provision time instead of using manual fallback lists
- Aggressive cost defaults: GPU stall detection enabled (30 min), idle timeout 5 min

## RunPod Operational Notes

- Most RunPod images lack rsync — deploy step must `apt-get install -y rsync` first
- Use `</dev/null &` with nohup over SSH to prevent hangs
- Pod SSH ports are dynamic (random public port → container port 22) — query API to discover
- Always sync results before teardown — data is lost once a pod is terminated
- Check both pod status (eviction) AND process status (crash) separately
