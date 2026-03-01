# Cloud Conductor

A lightweight CLI orchestrator for [RunPod](https://www.runpod.io/) GPU workloads. Provisions pods, deploys code via rsync, monitors jobs, syncs results, and tears down pods — all driven by a single TOML config file.

Cloud Conductor is **project-agnostic**: it runs arbitrary shell commands on GPU pods and knows nothing about the specific workloads.

## Features

- **TOML-driven** — all configuration in one file, no CLI flags for job settings
- **Multi-job orchestration** — run multiple GPU jobs in parallel with dependency ordering
- **Spot recovery** — automatic re-provision, re-deploy, and resume on spot interruptions
- **Cost controls** — global and per-job budgets, idle detection, automatic teardown
- **Result syncing** — periodic rsync of configurable paths from pod to local machine
- **Pod reuse** — skip provisioning on re-runs for fast iteration (`keep_pod_alive`)
- **Auto GPU selection** — query RunPod API for cheapest available GPU at provision time
- **Notifications** — macOS native, Pushover push, or pipe to any custom command
- **Observable** — `conductor status` works even when the main process isn't running

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/YOUR_USER/cloud-conductor.git
cd cloud-conductor
uv sync
```

You also need `ssh` and `rsync` on your system (standard on macOS/Linux), and a [RunPod API key](https://www.runpod.io/console/user/settings) set as `RUNPOD_API_KEY`.

## Quick Start

1. Create a `jobs.toml`:

```toml
[global]
gpu_type_id = "NVIDIA RTX A2000"
image_name = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
ssh_key_path = "~/.ssh/id_rsa"
remote_project_dir = "/workspace/my-project"
budget_usd = 10.00

setup_command = "pip install -r requirements.txt"
sync_paths = [
    { remote = "output/", local = "./results/" },
]

[[jobs]]
name = "train"
run_command = "python train.py --epochs 100"
```

2. Validate and run:

```bash
conductor validate --config jobs.toml
conductor run --config jobs.toml
```

3. While running, use other subcommands in a separate terminal:

```bash
conductor status                    # show job table
conductor logs train --tail         # stream remote log
conductor ssh train                 # interactive SSH to pod
conductor sync                     # force immediate result sync
```

## CLI Reference

```
conductor run [--config jobs.toml] [--jobs name1,name2] [--budget 40.00]
conductor status [--config jobs.toml]
conductor sync [--config jobs.toml] [--jobs name1,name2]
conductor teardown [--config jobs.toml] [--jobs name1,name2] [--force]
conductor dry-run [--config jobs.toml]
conductor validate [--config jobs.toml]
conductor logs <job-name> [--tail] [--config jobs.toml]
conductor ssh <job-name> [--config jobs.toml]
conductor report [--config jobs.toml]
```

## Configuration

The TOML config has a `[global]` section for defaults and `[[jobs]]` entries for each job. Any global field can be overridden per-job.

### Key settings

| Field | Default | Description |
|---|---|---|
| `gpu_type_id` | | Primary GPU to provision |
| `gpu_type_ids_fallback` | `[]` | Fallback GPUs tried in order |
| `auto_select_cheapest_gpu` | `false` | Ignore manual list, pick cheapest available |
| `gpu_min_vram_gb` | `0` | Minimum VRAM filter for auto-select |
| `image_name` | | Docker image for the pod |
| `deploy_method` | `"rsync"` | `"rsync"` or `"image"` (prebuilt Docker) |
| `setup_command` | `""` | Shell command run after rsync (install deps, etc.) |
| `sync_paths` | `[]` | `[{remote, local}]` pairs to sync periodically |
| `sync_interval_minutes` | `5` | How often to sync during monitoring |
| `budget_usd` | `0` | Global budget limit (0 = unlimited) |
| `job_budget_usd` | `0` | Per-job budget limit |
| `idle_timeout_minutes` | `10` | Teardown pod if process not running for this long |
| `stall_timeout_minutes` | `0` | Teardown if GPU util stays below threshold (0 = disabled) |
| `stall_gpu_threshold` | `5` | GPU util % below which the job is considered stalled |
| `keep_pod_alive` | `false` | Don't teardown on completion (for iterative dev) |
| `depends_on` | `[]` | Job names that must complete before this job starts |

### Notifications (optional)

```toml
[global.notifications]
backend = "terminal-notifier"  # or "pushover" or "command"
on_job_complete = true
on_job_failure = true
on_budget_threshold = 0.8
on_spot_recovery = true
```

See [SPEC.md](SPEC.md) for the complete configuration reference.

## Lifecycle

```
Provision → Deploy → Launch → Monitor → Teardown
    ↑                            │
    └──── Spot Recovery ─────────┘
```

1. **Provision** — create RunPod pod, wait for SSH
2. **Deploy** — rsync code + run setup command (or skip for prebuilt images)
3. **Launch** — start `run_command` via nohup
4. **Monitor** — poll process status, sync results, track costs, handle interruptions
5. **Teardown** — terminate pod (final sync first)

## Job Dependencies

```toml
[[jobs]]
name = "train"
run_command = "python train.py"

[[jobs]]
name = "evaluate"
depends_on = ["train"]
run_command = "python eval.py"
```

Jobs with unmet dependencies stay in `pending` until all dependencies complete. If a dependency fails, dependents are marked `skipped`.

## Development

```bash
uv sync
uv run pytest              # run all tests
uv run pytest -x -q        # stop on first failure, quiet output
uv run conductor --help    # verify CLI
```

## License

Copyright (c) 2026 Steve Flinter. [MIT License](LICENSE).
