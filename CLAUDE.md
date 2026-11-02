# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Cloud Conductor is a lightweight CLI orchestrator for GPU cloud workloads (RunPod and Vast.ai). It supports two modes: **imperative** (`conductor pod` commands for direct pod management without any config file) and **declarative** (`conductor run -f jobs.toml` for batch orchestration). It is **project-agnostic**: it runs arbitrary shell commands on GPU pods and knows nothing about the specific workloads.

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

**Lifecycle** (declarative): Reconcile (match the provider's `conductor-<run_id>-*` pods to state) → Provision → Deploy → Launch → Monitor → Teardown (with spot recovery loop back to Provision). Every run has a short `run_id`; pod IDs are persisted to the state file and registry the instant the provider returns them.

Key modules:
- `cli.py` — argparse entry point with `pod` subcommand group (imperative) and traditional subcommands (declarative); `configure_logging()` owns the root logger (`force=True`, after importing the runpod SDK which calls `basicConfig` itself) and `run`/`teardown`/`sync` add a rotating `conductor.log` next to the state file
- `config.py` — TOML loading, merges `[global]` defaults with per-`[[jobs]]` overrides into `JobConfig` dataclasses; relative local paths (`state_file`, `cost_log_file`, `local_project_dir`, `ssh_key_path`, sync/upload `local`) are resolved against the config file's directory
- `providers/__init__.py` — `CloudProvider` protocol + `get_provider()` registry + `get_configured_provider()` (with API key setup)
- `providers/runpod.py` — `RunPodProvider`: wraps `runpod` SDK for pod CRUD, SSH discovery, GPU catalog
- `providers/vastai.py` — `VastaiProvider`: REST calls via `httpx` to `console.vast.ai/api/v0/` for instance CRUD
- `registry.py` — global pod registry (`~/.conductor/pods.json`), tracks all pods regardless of creation mode; `reconcile_with_provider()` verifies pod status against the actual cloud provider
- `provisioner.py` — provider-agnostic provisioning; `provision_pod_direct()` for imperative, `provision_pod()` for declarative. Walks the GPU candidate list up to `provision_retries` passes with jittered backoff when the provider raises `NoCapacityError`; a pass (not a candidate) counts one provision attempt. `SshAuthError` from `wait_ssh` terminates the pod and fails the job without trying further GPUs
- `deployer.py` — `deploy_direct()` for imperative rsync, `deploy()` wrapper for declarative
- `runner.py` — `launch_direct()` writes `.conductor_run.sh` (`cd <dir> && ( <cmd> ); echo $? > .conductor_exit`) and `.conductor_launch.sh` (`setsid nohup bash .conductor_run.sh > conductor_job.log 2>&1 < /dev/null &`, records pid + start time from `/proc/<pid>/stat`, idempotent: reports an already-running job instead of starting another); `is_alive()` returns a tri-state `ProcessStatus` (running / exited with code / unknown); `fetch_log_tail()`; `exec_foreground()` for imperative, `launch()` wrapper for declarative
- `monitor.py` — main loop: startup reconciliation by run ID, resume of in-flight jobs, exit-file/process checks, periodic sync, spot recovery (only after `recovery_confirmations` confirmed "pod gone" answers), cost tracking (accrued from pod creation for every live job), GPU stall detection, dependency resolution, `Heartbeat` daemon thread (refreshes `heartbeat_at` every `POLL_INTERVAL/2` even while the main thread is blocked provisioning), Ctrl-C detach, crash handler that prints every live pod ID
- `state.py` — `PodState` dataclass (includes `pid`, `pid_start`, `exit_code`, `finished_at`, `gone_confirmations`, `idle_since`, `stalled_since`), `RunState` (`run_id`, `orchestrator_pid`, `heartbeat_at`), JSON state file + JSONL cost log I/O, all under the file lock. `accrue_cost` never accrues past `finished_at`; `record_pod_stopped` writes at most one `pod_stopped` event per pod ID
- `ssh.py` — SSH/rsync helpers. Never raise (except `wait_ssh`, which raises `SshAuthError` after 3 consecutive `Permission denied`-style answers and `Interrupted` on Ctrl-C): timeouts/OS errors come back as a failed `CompletedProcess`; ssh rc 255 and rsync transient codes are retried with backoff. Options: `StrictHostKeyChecking=no`, `ConnectTimeout=15`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`, `BatchMode=yes`; rsync `--partial --timeout=120`
- `errors.py` — `ConductorError` and `guarded()`: the one exception type for SDK/HTTP/JSON/OS failures; subclasses `SshAuthError` (key rejected) and `NoCapacityError` (provider has no instance of that GPU right now)
- `shutdown.py` — process-wide Ctrl-C flag; every long wait (`wait_ssh`, provider `wait_for_ssh`, retry/backoff sleeps) checks it between polls via `shutdown.wait()`. `Interrupted` means "stop here, keep the job as persisted", never "failed"
- `retry.py` — `retry()` with exponential backoff and jitter; retries transport errors, 429 and 5xx, never other 4xx or auth errors. Wraps idempotent provider calls only (never instance creation)
- `filelock.py` — `locked(path)`: `flock` on a hidden `.lock` sibling, used for every read-modify-write of the state file and registry (also serialises pool threads)
- `gpu_pricing.py` — queries provider GPU catalog for prices, caches 5 min
- `notify.py` — notification dispatch (terminal-notifier, pushover, or custom command)
- `validator.py` — pre-flight checks (SSH key exists, permissions and usable non-interactively via `ssh-keygen -y -P ''` or `ssh-add -l`; paths, GPU IDs, dependency cycles)

**State files** (created at runtime, not committed):
- `~/.conductor/pods.json` — global pod registry (imperative + declarative pods)
- `.conductor_state.json` — per-project batch run state (source of truth for declarative `conductor status`), next to the config file by default; carries `run_id`, `orchestrator_pid`, `heartbeat_at`
- `.conductor_cost_log.jsonl` — append-only cost events for `conductor report`
- `conductor.log` — rotating log (5 MB x 3) next to the state file, written by `run`/`teardown`/`sync`
- `.<name>.lock` siblings of the state file and registry — `flock` lock files, safe to leave in place
- On the pod, in `remote_project_dir`: `conductor_job.log`, `.conductor_pid`, `.conductor_pid_start`, `.conductor_exit`, `.conductor_run.sh`, `.conductor_launch.sh`

## Dependencies

Intentionally minimal:
- `runpod` — RunPod Python SDK for pod CRUD
- `httpx` — HTTP client for Vast.ai REST API and pushover notifications
- stdlib only for everything else (no click, no rich)
- System: `ssh`, `rsync`

## Key Design Decisions

- **Multi-provider**: `CloudProvider` protocol in `providers/` with RunPod and Vast.ai implementations. Provider selected via `provider` config field or `--provider` CLI flag
- **Dual-mode CLI**: imperative `conductor pod` commands for ad-hoc work, declarative `conductor run -f` for batch orchestration
- Core operations (`provision_pod_direct`, `deploy_direct`, `launch_direct`) are decoupled from config — imperative commands use them directly
- Global pod registry at `~/.conductor/pods.json` tracks all pods (includes `provider` field for teardown without config); `conductor status` reconciles registry bidirectionally against the provider API — marks missing pods as terminated and resurrects terminated pods found still running
- Before provisioning a replacement pod (spot recovery or re-provision), the old pod is always terminated first to prevent orphans; `teardown_pod()` retries and then polls the provider until the pod is confirmed gone, and an unverified teardown is never recorded as terminated
- A transient failure must never kill the orchestrator or destroy a healthy pod: SSH/rsync return failed results instead of raising, provider calls raise `ConductorError`, any exception in a job pipeline or monitor tick fails only that job, and a pod is treated as reclaimed only after `recovery_confirmations` consecutive confirmed "gone" answers (a `None` from `check_exists()` means "could not tell")
- Job completion comes from the remote exit file, immediately and with the real exit code; `idle_timeout_minutes` is only the fallback when the process vanished without one
- Ctrl-C detaches (pods keep running, `conductor run` resumes), also mid-provisioning: waits poll the shutdown flag and the pipeline stops without marking anything failed; `--teardown-on-interrupt` is the destructive variant. One live orchestrator per state file, enforced by the heartbeat thread; `teardown`/`sync` refuse while a run is live unless `--force`
- Every command that releases a pod records it: registry status, a `pod_stopped` cost event and the job outcome in state (`conductor teardown` writes reason `manual_teardown`, error "torn down manually"). Cost accrues from pod creation in every live state, not only while `running`, and stops at `finished_at`. A provider answer of "not found" to a terminate means the pod is already gone (verified, not retried); a pod with a stop event already is never billed or recorded again, and a finished job keeps its status
- The final sync is retried and, if it keeps failing after a successful job, the pod is kept alive and the job marked `sync_failed` rather than losing results; a job that failed is torn down anyway (sync failure noted in `job.error`) unless `keep_pod_on_failure = true`; budget stops still force the teardown. A `sync_paths` source missing on the pod is "nothing to sync", not a failure; `optional = true` entries never fail a sync
- On any abnormal exit, every live pod ID is printed with its job; pods are torn down on a crash only when `teardown_on_crash = true`
- `run_command` is an opaque user-supplied shell string — the conductor never parses it
- Per-job overrides: any `[global]` config field can be overridden in a `[[jobs]]` entry
- `depends_on` supports job ordering with cycle detection; failed dependencies cascade as `skipped`
- `keep_pod_alive` enables pod reuse across runs (re-syncs code, skips setup)
- `auto_select_cheapest_gpu` queries provider API at provision time instead of using manual fallback lists
- Aggressive cost defaults: GPU stall detection enabled (30 min), idle timeout 5 min

## RunPod Operational Notes

- Most RunPod images lack rsync — deploy step must `apt-get install -y rsync` first
- Use `setsid nohup ... > log 2>&1 < /dev/null &` over SSH to prevent hangs and detach the job from the SSH session; kill the process group (`kill -- -<pid>`) to stop it
- Pod SSH ports are dynamic (random public port → container port 22) — query API to discover
- Always sync results before teardown — data is lost once a pod is terminated
- Check both pod status (eviction) AND process status (crash) separately
