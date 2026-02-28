# Cloud Conductor — Specification

A lightweight CLI orchestrator for RunPod GPU workloads. Provisions pods, deploys code, monitors jobs, syncs results, and tears down pods when done.

## Motivation

The poker-ninja project has an ad-hoc RunPod orchestrator (`src/runpod_pipeline.py`) that grew organically. It works but has several problems:

1. **Tightly coupled to poker-ninja** — job configs, training commands, log parsing, and MLflow integration are all poker-specific
2. **Monolithic** — provisioning, deployment, monitoring, and teardown are all in one 1,300-line file
3. **No real-time visibility** — status is only visible in the Python process's stdout; if the orchestrator dies, you lose visibility
4. **No cost controls** — no budget limits, no automatic shutdown of idle pods, no alerts
5. **Fragile spot recovery** — re-provisioning works but can loop forever on scarce GPU types

Cloud Conductor extracts the orchestration concerns into a standalone, project-agnostic tool.

## Design Principles

- **Project-agnostic**: Conductor knows nothing about poker, training, or ML. It runs arbitrary shell commands on provisioned pods.
- **TOML-driven**: All configuration lives in a single TOML file. No CLI flags for job-specific settings.
- **Stateful**: Pod state is persisted to a JSON file so the conductor can be stopped and restarted without losing track of pods.
- **Observable**: Status is always queryable via `conductor status`, even if the main process is not running.
- **Cost-aware**: Built-in budget limits, per-pod cost tracking, and automatic teardown of idle or over-budget pods.
- **Composable**: Each phase (provision, deploy, run, sync, teardown) can be invoked independently.

## CLI Interface

```
conductor run [--config jobs.toml] [--jobs name1,name2] [--budget 40.00]
conductor status [--config jobs.toml]
conductor sync [--config jobs.toml] [--jobs name1,name2]
conductor teardown [--config jobs.toml] [--jobs name1,name2]
conductor dry-run [--config jobs.toml]
conductor validate [--config jobs.toml]
conductor logs <job-name> [--tail] [--config jobs.toml]
conductor ssh <job-name> [--config jobs.toml]
conductor report [--config jobs.toml]
```

### `conductor run`

Full lifecycle: provision → deploy → launch → monitor → sync → teardown.

- Runs `validate` checks before provisioning (fail fast on bad config)
- Provisions pods in parallel (one per job), respecting `depends_on` ordering
- Deploys code via rsync or Docker image
- Launches the job command via `nohup`
- Monitors pods in a loop, syncing results at a configurable interval
- Tears down each pod when its job completes or fails
- Sends notifications on job completion, failure, or budget threshold
- Exits when all jobs are done
- Ctrl+C triggers graceful shutdown: final sync, then teardown all pods

### `conductor status`

Reads the state file and queries RunPod API to show a table:

```
Job     Pod          GPU              Status     Elapsed    Cost     Info
─────────────────────────────────────────────────────────────────────────
3p      abc123def4   RTX 2000 Ada     running    2h 15m     $0.27    gen 5/15
4p      xyz789ghi0   RTX A4000        completed  4h 02m     $0.97    gen 15/15
5p      ---          ---              pending    ---        ---      queued (waiting on 4p)
```

### `conductor sync`

Forces an immediate rsync of results from all running pods.

### `conductor teardown`

Terminates all pods tracked in the state file. Prompts for confirmation unless `--force` is passed.

### `conductor dry-run`

Prints what would be provisioned (GPU type, image, disk, estimated cost) without creating anything. Includes automatic GPU price lookup.

### `conductor validate`

Pre-flight checks before provisioning. Catches configuration errors early:

- SSH key file exists and has correct permissions
- Local project directory exists (for rsync deploy)
- Seed checkpoint / file paths referenced in `run_command` exist locally
- GPU type IDs are valid (queries `runpod.get_gpu_types()`)
- Docker image is pullable (for prebuilt image deploy)
- TOML config parses correctly, required fields are present
- `depends_on` references exist and have no cycles
- Budget is set and reasonable

Exits with non-zero status and descriptive errors if any check fails. `conductor run` calls this automatically before provisioning.

### `conductor logs <job-name>`

Fetches and displays the remote log file for a specific job. With `--tail`, opens an SSH connection and streams `tail -f` of the remote log in real time.

### `conductor ssh <job-name>`

Opens an interactive SSH session to the pod running the named job.

### `conductor report`

Summarizes historical spend across all runs. Reads from the cost log file (append-only, written by the monitor). Output:

```
=== Cost Report ===
Run started: 2026-02-28 17:53

Job     GPU              Status      Duration    Cost
────────────────────────────────────────────────────────
3p      RTX 2000 Ada     completed   4h 12m      $0.50
4p      RTX A4000        completed   3h 45m      $0.94
5p      RTX 2000 Ada     failed      0h 08m      $0.02
5p      RTX A4000        completed   4h 30m      $1.13  (retry)
────────────────────────────────────────────────────────
Total                                             $2.59

By GPU type:
  RTX 2000 Ada:   8h 20m   $1.00
  RTX A4000:      8h 15m   $2.07

Budget: $40.00 | Spent: $2.59 | Remaining: $37.41
```

## Configuration (TOML)

```toml
[global]
# RunPod settings
gpu_type_id = "NVIDIA RTX A2000"
gpu_type_ids_fallback = [
    "NVIDIA RTX 2000 Ada Generation",
    "NVIDIA RTX A4000",
]
auto_select_cheapest_gpu = false      # true = ignore gpu_type_id/fallback, auto-pick cheapest available
gpu_min_vram_gb = 0                   # minimum VRAM filter when auto_select_cheapest_gpu = true
cloud_type = "ALL"                    # "COMMUNITY", "SECURE", or "ALL"
image_name = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
container_disk_in_gb = 40
volume_in_gb = 0
volume_mount_path = "/workspace"
ssh_key_path = "~/.ssh/id_rsa"

# Deployment
deploy_method = "rsync"               # "rsync" or "image" (prebuilt Docker)
local_project_dir = "."               # local directory to rsync
remote_project_dir = "/workspace/my-project"
rsync_excludes = [
    ".venv/", "__pycache__/", "*.pyc", ".git/",
    "mlruns/", "*.log", ".pytest_cache/",
]

# Post-rsync setup command (install deps, etc.)
setup_command = """
pip install -q uv && \
uv venv --seed .venv && \
uv sync --frozen
"""

# Result sync
sync_interval_minutes = 5
sync_paths = [
    { remote = "models/output/", local = "./results/sync/" },
    { remote = "training.log", local = "./logs/" },
]

# Cost controls
budget_usd = 40.00                    # global hard budget limit, teardown all when reached
idle_timeout_minutes = 10             # teardown pod if job process not running for this long
cost_per_hour_override = 0.0          # 0 = auto-detect from RunPod API

# Spot recovery
max_provision_attempts = 5
backoff_base_seconds = 30

# Pod reuse
keep_pod_alive = false                # true = don't teardown on completion, keep for re-launch

# State / cost persistence
state_file = ".conductor_state.json"
cost_log_file = ".conductor_cost_log.jsonl"  # append-only cost log for `conductor report`

# Notifications (optional — omit entire section to disable)
[global.notifications]
on_job_complete = true
on_job_failure = true
on_budget_threshold = 0.8            # notify when 80% of budget spent
on_spot_recovery = true

# Notification backends (configure one or more)
# macOS native notifications (no dependencies)
backend = "terminal-notifier"         # "terminal-notifier", "pushover", or "command"

# Pushover (cross-platform push notifications)
# backend = "pushover"
# pushover_user_key = ""             # or set PUSHOVER_USER_KEY env var
# pushover_app_token = ""            # or set PUSHOVER_APP_TOKEN env var

# Custom command (most flexible — pipe event JSON to any script)
# backend = "command"
# notify_command = "python notify.py"  # receives JSON on stdin: {"event": "job_complete", "job": "3p", ...}

[[jobs]]
name = "3p"
run_command = """
PYTHONUNBUFFERED=1 .venv/bin/python scripts/train_self_play.py \
    --checkpoint models/seeds/3p \
    --table_config "players=3,small_blind=1,stack_depth_bb=30" \
    --num_generations 15 --frames_per_generation 3000000 \
    --output_dir models/output/3p --resume
"""
# Per-job budget (in addition to global budget)
job_budget_usd = 5.00

# Per-job overrides (anything from [global] can be overridden here)
sync_paths = [
    { remote = "models/output/3p/", local = "./results/3p/" },
    { remote = "self_play_3p.log", local = "./logs/" },
]

[[jobs]]
name = "4p"
run_command = """
PYTHONUNBUFFERED=1 .venv/bin/python scripts/train_self_play.py \
    --checkpoint models/seeds/4p \
    --table_config "players=4,small_blind=2,stack_depth_bb=50" \
    --num_generations 15 --frames_per_generation 3000000 \
    --output_dir models/output/4p --resume
"""

[[jobs]]
name = "eval_4p"
depends_on = ["4p"]                   # only starts after 4p completes successfully
run_command = """
PYTHONUNBUFFERED=1 .venv/bin/python scripts/eval_with_search.py \
    --checkpoint models/output/4p/best \
    --table_config "players=4,small_blind=2,stack_depth_bb=50" \
    --num_iterations 200 --eval_episodes 500
"""
```

### Key design decisions

1. **`run_command` is opaque**: The conductor doesn't parse or construct the training command. The user specifies the full shell command. This keeps the conductor project-agnostic.

2. **`sync_paths` replaces hardcoded checkpoint/log sync**: Instead of knowing about "checkpoints" and "logs", the conductor syncs arbitrary remote→local path pairs.

3. **`setup_command` replaces hardcoded dep install**: The post-rsync setup is a user-supplied shell script. Could be `pip install`, `uv sync`, `make`, anything.

4. **`deploy_method`**: `"rsync"` rsyncs `local_project_dir` to `remote_project_dir` and runs `setup_command`. `"image"` assumes the Docker image has everything pre-installed and skips rsync/setup.

5. **Per-job overrides**: Any `[global]` field can be overridden in a `[[jobs]]` entry (different GPU type, image, disk size, etc.).

6. **`depends_on` for job ordering**: A job can declare dependencies on other jobs. The conductor only provisions/launches a job after all its dependencies have completed successfully. If a dependency fails, the dependent job is marked as `skipped`.

7. **`auto_select_cheapest_gpu`**: When enabled, ignores the manual GPU fallback list and instead queries `runpod.get_gpu_types()` at provision time, filters by `gpu_min_vram_gb`, sorts by price, and tries each in ascending price order. This eliminates the need to manually maintain and sort fallback lists.

8. **`keep_pod_alive` for pod reuse**: When set, the conductor does not teardown the pod on job completion. Useful during iterative development — tweak the config and re-run `conductor run`, which detects the existing pod, skips provisioning, re-deploys (rsync only, no full setup), and re-launches. Saves 3-5 minutes of provisioning + setup per iteration.

9. **Notifications are opt-in**: Disabled by default. Three backends cover different use cases: `terminal-notifier` for macOS desktop alerts (zero setup), `pushover` for mobile push notifications, `command` for piping to arbitrary scripts (Slack webhooks, email, etc.).

## State File (`.conductor_state.json`)

```json
{
  "jobs": [
    {
      "name": "3p",
      "pod_id": "abc123def456",
      "gpu_type": "NVIDIA RTX 2000 Ada Generation",
      "gpu_cost_per_hour": 0.12,
      "ssh_host": "213.173.99.23",
      "ssh_port": 28117,
      "status": "running",
      "started_at": 1709142301.5,
      "last_sync_at": 1709149501.2,
      "idle_since": null,
      "provision_attempts": 1,
      "cost_usd": 0.27,
      "job_budget_usd": 5.00
    },
    {
      "name": "eval_4p",
      "pod_id": null,
      "status": "pending",
      "depends_on": ["4p"],
      "cost_usd": 0.0
    }
  ],
  "total_cost_usd": 0.27,
  "budget_usd": 40.00
}
```

The state file is updated after every significant event (provision, deploy, sync, status check, teardown). It is the source of truth for `conductor status`.

## Cost Log (`.conductor_cost_log.jsonl`)

Append-only JSONL file recording every cost-relevant event. Used by `conductor report` for historical analysis.

```jsonl
{"ts": 1709142301.5, "event": "pod_started", "job": "3p", "pod_id": "abc123", "gpu_type": "RTX 2000 Ada", "cost_per_hour": 0.12}
{"ts": 1709149501.2, "event": "pod_sync", "job": "3p", "elapsed_hours": 2.0, "cost_usd": 0.24}
{"ts": 1709156701.5, "event": "pod_stopped", "job": "3p", "reason": "completed", "total_hours": 4.0, "total_cost_usd": 0.48}
{"ts": 1709156705.0, "event": "pod_started", "job": "3p", "pod_id": "def456", "gpu_type": "RTX A4000", "cost_per_hour": 0.25}
```

## Architecture

```
cloud-conductor/
├── pyproject.toml          # uv project, CLI entry point
├── SPEC.md                 # this file
├── src/
│   └── conductor/
│       ├── __init__.py
│       ├── cli.py          # argparse CLI with subcommands, dispatches to commands
│       ├── config.py       # TOML config loading, merging global+job, validation
│       ├── provisioner.py  # RunPod pod creation, GPU fallback/auto-select, SSH wait
│       ├── deployer.py     # rsync code, run setup_command (or skip for prebuilt image)
│       ├── runner.py       # launch run_command via nohup, check if process alive
│       ├── syncer.py       # rsync sync_paths from pod to local
│       ├── monitor.py      # main loop: check status, sync, spot recovery, cost tracking, deps
│       ├── state.py        # state file + cost log read/write, PodState dataclass
│       ├── ssh.py          # SSH/rsync helpers (shared by all modules)
│       ├── gpu_pricing.py  # query RunPod API for GPU types/prices, auto-select cheapest
│       ├── notify.py       # notification dispatch (terminal-notifier, pushover, command)
│       └── validator.py    # pre-flight config validation (SSH keys, paths, GPU IDs, etc.)
└── tests/
    ├── test_config.py
    ├── test_provisioner.py
    ├── test_state.py
    ├── test_validator.py
    ├── test_notify.py
    └── ...
```

### Module responsibilities

**`cli.py`** — Entry point. Parses args, loads config, dispatches to the appropriate command function. Uses `argparse` with subcommands. Registers subcommands: `run`, `status`, `sync`, `teardown`, `dry-run`, `validate`, `logs`, `ssh`, `report`.

**`config.py`** — Reads TOML config file. Merges `[global]` defaults with per-`[[jobs]]` overrides. Resolves `depends_on` references. Returns a list of `JobConfig` dataclasses. Validates required fields and types.

**`provisioner.py`** — Creates RunPod pods via the `runpod` Python SDK. Two GPU selection modes:
- Manual: tries the primary `gpu_type_id`, then falls through `gpu_type_ids_fallback`
- Auto: delegates to `gpu_pricing.py` to find the cheapest available GPU meeting minimum VRAM requirements

Waits for SSH to become available (polls RunPod API for port mapping, then tests SSH connectivity). Returns `PodInfo` with host/port/pod_id/gpu_type/cost_per_hour. Supports pod reuse: if `keep_pod_alive` is set and a pod from a previous run still exists, skips provisioning and reuses it.

**`deployer.py`** — Two modes:
- `rsync`: Installs rsync on the pod (if needed), rsyncs `local_project_dir` to `remote_project_dir` with configured excludes, then runs `setup_command` via SSH.
- `image`: Skips rsync and setup (image has everything). Optionally syncs specific files (like seed checkpoints) listed in the config.

When reusing a pod (`keep_pod_alive`), rsync mode re-syncs code (fast incremental) but skips `setup_command` (deps already installed).

**`runner.py`** — Launches `run_command` on the pod via `nohup bash -c '...' > log 2>&1 </dev/null &`. Checks whether the process is still running via `ps aux | grep` over SSH. Captures the remote PID for reliable process tracking.

**`syncer.py`** — Rsyncs configured `sync_paths` from pod to local machine. Each sync path specifies a remote path (relative to `remote_project_dir`) and a local destination. Also supports push direction (local → remote) for spot recovery checkpoint restoration.

**`monitor.py`** — Main monitoring loop:
1. For each active job, check if the process is still running (`runner.is_alive()`)
2. If running and sync interval elapsed, trigger sync
3. If process not running, determine if completed or failed:
   - Check RunPod pod status (EXITED/STOPPED → spot interruption)
   - SSH unreachable → spot interruption
   - Process exited but pod alive → completed or crashed (check exit code if available)
4. On spot interruption: teardown dead pod, backoff, re-provision, re-deploy, push synced results back, re-launch with same command (should resume via `--resume` or equivalent in the user's command)
5. On completion: final sync, teardown pod (unless `keep_pod_alive`), send notification
6. On failure: sync logs, teardown pod, send notification
7. Update cost tracking based on elapsed time and GPU hourly rate
8. If per-job budget exceeded, teardown that job's pod, send notification
9. If global budget exceeded, teardown all pods, send notification, exit
10. Check `depends_on`: if a completed job unblocks pending jobs, start provisioning them
11. Print status table
12. Append cost event to cost log

**`state.py`** — Serializes/deserializes the state file. Provides `PodState` dataclass with fields for pod_id, ssh_host, ssh_port, gpu_type, gpu_cost_per_hour, status, timing, cost, provision attempts, idle_since, job_budget_usd. Also manages the append-only cost log (JSONL) for historical reporting.

**`ssh.py`** — Low-level SSH and rsync wrappers. All SSH commands use `-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null` for ephemeral pod connections. Provides `ssh_exec()` for commands and `ssh_interactive()` for `conductor ssh`. Provides `tail_remote_log()` for `conductor logs --tail` (opens persistent SSH + `tail -f`).

**`gpu_pricing.py`** — Queries `runpod.get_gpu_types()` to get current GPU availability and pricing. Filters by minimum VRAM, sorts by price ascending, returns ordered list of candidates. Caches results for 5 minutes to avoid hammering the API during parallel provisioning.

**`notify.py`** — Dispatches notifications based on configured backend:
- `terminal-notifier`: Calls `terminal-notifier` CLI (macOS native notifications, no dependencies beyond Homebrew install). Falls back to `osascript` display notification if terminal-notifier is not installed.
- `pushover`: POST to Pushover API with user key and app token. Supports priority levels (normal for completion, high for failure/budget).
- `command`: Pipes a JSON event payload to a user-specified command's stdin. The JSON includes `event` type, `job` name, `status`, `cost`, `elapsed`, etc. This is the escape hatch for Slack webhooks, email scripts, or anything else.

Events that trigger notifications: `job_complete`, `job_failed`, `spot_recovery`, `budget_threshold` (configurable percentage), `budget_exceeded`, `job_budget_exceeded`.

**`validator.py`** — Pre-flight configuration validation. Checks:
- SSH key file exists at `ssh_key_path` and has correct permissions (600/644)
- `local_project_dir` exists (for rsync deploy)
- GPU type IDs are valid (queries `runpod.get_gpu_types()` and checks names match)
- Docker image is reachable (for prebuilt image deploy — runs `docker manifest inspect`)
- `depends_on` references all resolve to valid job names
- `depends_on` graph has no cycles (topological sort)
- Required fields are present in each `[[jobs]]` entry (`name`, `run_command`)
- Budget values are positive numbers
- `notify_command` is executable (for command backend)

Returns a list of errors/warnings. `conductor validate` prints them. `conductor run` calls it automatically and aborts on errors.

## Dependencies

Minimal:
- `runpod` — RunPod Python SDK for pod CRUD
- `httpx` — HTTP client for Pushover API (only if pushover notifications enabled; stdlib `urllib` as fallback)
- Python 3.12+ stdlib only (no click, no rich — keep it lightweight)
- System: `ssh`, `rsync` (expected on macOS/Linux)
- Optional system: `terminal-notifier` (macOS, `brew install terminal-notifier`)

## Lifecycle Phases

```
┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌──────────┐
│Provision│──▶│ Deploy  │──▶│ Launch  │──▶│ Monitor │──▶│ Teardown │
└─────────┘   └─────────┘   └─────────┘   └─────────┘   └──────────┘
     ▲                                          │
     │            Spot Recovery                 │
     └──────────────────────────────────────────┘
```

1. **Provision**: Create pod, wait for SSH. Parallel across jobs.
2. **Deploy**: Rsync code + run setup (or skip for prebuilt images). Parallel across jobs.
3. **Launch**: Start `run_command` via nohup. Sequential (fast, just SSH commands).
4. **Monitor**: Loop until all jobs done. Sync results, track costs, handle interruptions.
5. **Teardown**: Terminate pod via RunPod API. Final sync first.

## Error Handling

| Scenario | Behavior |
|---|---|
| Config validation fails | Abort before provisioning, print errors |
| All GPU types unavailable | Mark job as `failed`, notify, continue other jobs |
| SSH never becomes ready (5 min timeout) | Mark job as `failed`, terminate pod, notify |
| Deploy/setup command fails | Mark job as `failed`, terminate pod, notify |
| Process exits unexpectedly | Final sync of logs, mark as `failed`, terminate pod, notify |
| Spot interruption (pod evicted) | Automatic recovery: re-provision → re-deploy → push synced results → re-launch. Notify. |
| Max provision attempts exceeded | Mark job as `failed`, stop retrying, notify |
| Per-job budget exceeded | Final sync, teardown that pod, mark as `failed`, notify |
| Global budget limit reached | Final sync all pods, teardown all pods, notify, exit |
| Dependency failed | Mark dependent jobs as `skipped`, notify |
| Ctrl+C | Graceful shutdown: final sync all pods, teardown all pods (unless `keep_pod_alive`), save state, exit |
| Conductor process dies | State file preserved. `conductor status` still works. `conductor run` resumes from saved state. |

## Cost Tracking

The conductor tracks cost per pod based on:
- `elapsed_time × cost_per_hour`
- `cost_per_hour` is either auto-fetched from the RunPod API (`runpod.get_gpu_types()`) or manually set via `cost_per_hour_override` in config
- Total cost across all jobs is summed and compared against `budget_usd`
- When total cost exceeds budget, all pods are torn down immediately

## Idle Detection

After launching a job, the monitor periodically checks if the process is still running. If the process is not found for `idle_timeout_minutes` consecutive checks:

1. Do a final sync
2. Terminate the pod
3. Mark the job as `completed` (assumption: process exited cleanly)

This prevents paying for idle pods after training completes — the single most common source of wasted spend in the current system.

## Resume Behavior

When `conductor run` is invoked and a state file exists:

- Jobs with status `completed` or `failed` are skipped
- Jobs with status `running` are validated: check if pod still exists and process is alive. If yes, resume monitoring. If no, treat as interrupted and attempt recovery.
- Jobs with status `pending` are started from scratch
- Jobs with status `skipped` (dependency failed) are skipped

This means you can safely kill and restart the conductor without losing track of pods.

## Job Dependencies

Jobs can declare dependencies via `depends_on = ["job_a", "job_b"]`. The conductor respects these constraints:

1. **Startup ordering**: Jobs with unmet dependencies remain in `pending` state. They are only provisioned after all dependencies reach `completed` status.
2. **Failure propagation**: If a dependency fails, all jobs that depend on it (transitively) are marked `skipped`.
3. **Cycle detection**: `conductor validate` checks for dependency cycles and rejects the config.
4. **Independent jobs run in parallel**: Jobs without dependencies (or whose dependencies are already met) are provisioned in parallel as usual.

Example use case: train a model, then evaluate it on a separate pod:

```toml
[[jobs]]
name = "train"
run_command = "... train ..."

[[jobs]]
name = "evaluate"
depends_on = ["train"]
run_command = "... evaluate using train's synced output ..."
```

The `evaluate` job can reference files synced from the `train` job because the conductor syncs results to local before marking `train` as completed.

## Automatic GPU Price Selection

When `auto_select_cheapest_gpu = true`:

1. At provision time, query `runpod.get_gpu_types()` for all available GPUs
2. Filter by `gpu_min_vram_gb` (default 0 = any)
3. Filter by `cloud_type` ("COMMUNITY", "SECURE", or "ALL")
4. Sort by `communityPrice` (or `securePrice` depending on cloud_type) ascending
5. Try each GPU in price order until one succeeds

This replaces the manually-maintained `gpu_type_ids_fallback` list and always picks the cheapest option at the moment of provisioning. GPU pricing is cached for 5 minutes to avoid redundant API calls when provisioning multiple pods in parallel.

When `auto_select_cheapest_gpu = false` (default), the manual `gpu_type_id` + `gpu_type_ids_fallback` list is used as before.

## Pod Reuse

When `keep_pod_alive = true`:

1. On job completion, the pod is NOT terminated. It remains running (and billing).
2. On the next `conductor run`, the conductor detects the existing pod in the state file, verifies it is still alive via the RunPod API, and reuses it.
3. For rsync deploy: only re-syncs code (fast incremental rsync), skips `setup_command` (deps already installed from the previous run).
4. For image deploy: skips entirely (nothing to do).
5. The job command is re-launched on the existing pod.

This is useful for iterative development workflows where you're tweaking configs and re-running. Saves 3-5 minutes of provisioning + setup per iteration.

To release kept-alive pods: `conductor teardown` terminates all tracked pods regardless of `keep_pod_alive`.

## Notifications

Notifications are optional and disabled by default (omit the `[global.notifications]` section). When enabled, the conductor sends alerts on significant events:

| Event | Trigger |
|---|---|
| `job_complete` | A job finishes successfully |
| `job_failed` | A job fails (crash, timeout, budget) |
| `spot_recovery` | A spot interruption is detected and recovery begins |
| `budget_threshold` | Total spend crosses the configured percentage of budget |
| `budget_exceeded` | Total spend exceeds budget (all pods being torn down) |
| `job_budget_exceeded` | A single job exceeds its per-job budget |

### Notification backends

**`terminal-notifier`** (macOS, zero-config):
Sends macOS native notifications via the `terminal-notifier` CLI (`brew install terminal-notifier`). Falls back to `osascript -e 'display notification ...'` if terminal-notifier is not installed. No account setup needed.

**`pushover`** (cross-platform push notifications):
Sends push notifications to your phone via the Pushover API. Requires a Pushover account ($5 one-time), user key, and app token. Supports priority levels: normal for completions, high for failures and budget alerts.

**`command`** (most flexible):
Pipes a JSON payload to a user-specified command's stdin. The command can do anything: post to Slack via webhook, send email, write to a database, etc.

Event JSON format:
```json
{
  "event": "job_complete",
  "job": "3p",
  "status": "completed",
  "elapsed_hours": 4.2,
  "cost_usd": 0.50,
  "gpu_type": "NVIDIA RTX 2000 Ada Generation",
  "timestamp": "2026-02-28T22:15:00Z"
}
```

## Per-Job Budget Limits

In addition to the global `budget_usd`, each job can set `job_budget_usd`:

```toml
[[jobs]]
name = "8p"
job_budget_usd = 5.00    # don't spend more than $5 on this job
run_command = "..."
```

When a job's accumulated cost exceeds its per-job budget:
1. Final sync is performed
2. The pod is terminated
3. The job is marked as `failed` (reason: budget exceeded)
4. A notification is sent
5. Other jobs continue running (only the global budget stops everything)

This prevents a single slow-converging or misbehaving job from consuming the entire budget.

## Differences from poker-ninja `runpod_pipeline.py`

| Feature | runpod_pipeline.py | cloud-conductor |
|---|---|---|
| Project coupling | Poker-specific (CloudJobConfig has player_count, entropy_coef, etc.) | Project-agnostic (opaque run_command) |
| Config merging | Manual field-by-field extraction | Generic global+job merge |
| Training command | Built by `build_training_command()` with poker params | User-supplied `run_command` string |
| Log parsing | Regex for poker training output | None — conductor doesn't parse job output |
| MLflow integration | Built-in | None — out of scope |
| Sync paths | Hardcoded checkpoint + log paths | Configurable `sync_paths` list |
| Cost controls | None | Global budget + per-job budget + idle detection |
| GPU selection | Manual fallback list, hardcoded order | Manual fallback OR auto-cheapest with VRAM filter |
| Notifications | None | terminal-notifier, pushover, or custom command |
| Job dependencies | None (all jobs run in parallel) | `depends_on` with cycle detection |
| Pod reuse | None (always provision fresh) | `keep_pod_alive` for iterative dev |
| Config validation | None (crashes at runtime) | `conductor validate` pre-flight checks |
| Cost history | None | Append-only cost log + `conductor report` |
| CLI | Single-file argparse | Subcommand-based CLI |
| Resume | Partial (state file, but no validation) | Full resume with pod validation |
| Interactive SSH | Not supported | `conductor ssh <job>` |
| Log viewing | Not supported | `conductor logs <job>` + `--tail` streaming |

## Implementation Notes

### From operational experience with RunPod

These lessons come from running the poker-ninja pipeline and should be carried forward:

1. **SSH needs rsync installed first**: Most RunPod images don't have rsync. The deploy step must `apt-get install -y rsync` before rsyncing code. For prebuilt images, include rsync in the Dockerfile.

2. **Use `</dev/null &` for nohup over SSH**: Without stdin redirect, SSH hangs waiting for the nohup process.

3. **`PYTHONUNBUFFERED=1` for real-time logs**: Without this, Python buffers stdout and log files appear empty for minutes.

4. **Cheap GPUs for CPU-bound work**: Many ML workloads (especially RL environment simulation) are CPU-bound. The cheapest available GPU ($0.12-0.25/hr) is often the right choice. The GPU fallback list should be ordered by price.

5. **Prebuilt Docker images save 3+ minutes per deploy**: `uv sync` + `pip install` takes 3-5 minutes on every pod. For multi-pod deployments, this adds up. Prebuilt images skip this entirely.

6. **`--delete` flag in rsync is important**: Without it, removed files persist on the pod, causing stale code to run.

7. **Pod SSH port is dynamic**: RunPod assigns a random public port mapped to container port 22. Must query the API to discover it.

8. **Check pod status AND process status**: A pod can be running (not evicted) but the training process may have crashed. Check both.

9. **Sync before teardown**: Always do a final rsync before terminating a pod. Once terminated, all data on the pod is lost (unless using persistent volumes).

10. **Cost estimation is approximate**: RunPod bills by the second, but the API doesn't expose accumulated cost directly. Track elapsed time × hourly rate as an estimate.
