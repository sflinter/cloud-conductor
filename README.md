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
- **Resilient** — every SSH, rsync and provider call is retried with backoff; a transient error never kills the orchestrator or triggers a false spot recovery; Ctrl-C detaches and `conductor run` resumes

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

`validate` checks, among other things, that the SSH key can be used without a prompt: a passphrase-protected key must be loaded in `ssh-agent` (`ssh-add --apple-use-keychain ~/.ssh/id_rsa` on macOS), otherwise both `validate` and `run` stop before creating a pod.

3. While running, use other subcommands in a separate terminal:

```bash
conductor status                    # show job table
conductor logs train --tail         # stream remote log
conductor ssh train                 # interactive SSH to pod
conductor sync                     # force immediate result sync
```

## CLI Reference

```
conductor run [--config jobs.toml] [--jobs name1,name2] [--budget 40.00] [--retry-failed] [--retry-completed] [--force] [--teardown-on-interrupt]
conductor status [--config jobs.toml]
conductor sync [--config jobs.toml] [--jobs name1,name2] [--force]
conductor teardown [--config jobs.toml] [--jobs name1,name2] [--force]   # --jobs: only those jobs' pods
conductor dry-run [--config jobs.toml]
conductor validate [--config jobs.toml]
conductor logs <job-name> [--tail] [--config jobs.toml]
conductor ssh <job-name> [--config jobs.toml]
conductor report [--config jobs.toml]
```

## Configuration

The TOML config has a `[global]` section for defaults and `[[jobs]]` entries for each job. Any global field can be overridden per-job. Relative local paths (`state_file`, `cost_log_file`, `local_project_dir`, `ssh_key_path`, the `local` side of `sync_paths`/`upload_paths`) are resolved against the directory containing the config file, so running `conductor` from another directory keeps the state files and results with the project. Absolute paths and `~` are used as given.

### Key settings

| Field | Default | Description |
|---|---|---|
| `gpu_type_id` | | Primary GPU to provision |
| `gpu_type_ids_fallback` | `[]` | Fallback GPUs tried in order |
| `auto_select_cheapest_gpu` | `false` | Ignore manual list, pick cheapest available |
| `gpu_min_vram_gb` | `0` | Minimum VRAM filter for auto-select |
| `image_name` | | Docker image for the pod |
| `provision_timeout_seconds` | `900` | Max wait for a new pod's SSH to come up before it is terminated and the next GPU tried |
| `deploy_method` | `"rsync"` | `"rsync"` or `"image"` (prebuilt Docker) |
| `setup_command` | `""` | Shell command run after rsync (install deps, etc.) |
| `setup_timeout_seconds` | `1800` | Max time for `setup_command` before the job is marked failed |
| `deploy_timeout_seconds` | `600` | Max time for each deploy rsync |
| `sync_timeout_seconds` | `600` | Max time for each result-sync rsync |
| `sync_paths` | `[]` | `[{remote, local, optional?}]` pairs to sync periodically. A remote path missing on the pod is nothing to sync (not a failure); a transfer failure fails the sync unless `optional = true` |
| `sync_interval_minutes` | `5` | How often to sync during monitoring |
| `budget_usd` | `0` | Global budget limit (0 = unlimited) |
| `job_budget_usd` | `0` | Per-job budget limit |
| `idle_timeout_minutes` | `5` | Fallback only: teardown if the process vanished without writing an exit file and stayed gone this long |
| `stall_timeout_minutes` | `0` | Teardown if GPU util stays below threshold (0 = disabled) |
| `stall_gpu_threshold` | `5` | GPU util % below which the job is considered stalled |
| `recovery_confirmations` | `3` | Consecutive ticks the provider must report a pod gone before spot recovery starts |
| `provision_retries` | `3` | Passes over the GPU candidate list when none has capacity right now, with jittered backoff between passes |
| `keep_pod_alive` | `false` | Don't teardown on completion (for iterative dev) |
| `keep_pod_on_failure` | `false` | Keep the pod of a *failed* job whose final sync also failed (default: tear it down and record the sync failure in `job.error`) |
| `teardown_on_crash` | `false` | If the orchestrator process itself dies, tear down all live pods (default: leave them and print their IDs) |
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

## Resuming a run

Every `conductor run` gets a short run ID (printed at start, stored in `.conductor_state.json`) and names its pods `conductor-<run_id>-<job>`. Pod IDs are written to the state file and to `~/.conductor/pods.json` the moment the provider returns them, so a crash can never leave a pod recorded nowhere.

Running `conductor run` again with an existing state file:

- lists the provider's pods, adopts any `conductor-<run_id>-*` pod missing from state, and terminates (loudly) any such pod that no job tracks
- resumes jobs persisted as `provisioning`/`deploying` on their existing pod (re-deploy, launch) and relaunches a `running` job that has no recorded pid
- skips `completed`/`failed`/`skipped` jobs unless you pass `--retry-failed` (also un-skips their dependents) or `--retry-completed`

**Ctrl-C detaches.** The orchestrator stops after its current step (within one poll interval, even while it is still waiting for a new pod's SSH), prints the live pod IDs and exits; nothing is marked failed and no pod is terminated. Run `conductor run` again to resume monitoring, or `conductor teardown` to terminate. Use `conductor run --teardown-on-interrupt` for the old behaviour (final sync, then terminate everything). A second Ctrl-C hard-exits after printing the pod IDs.

**One orchestrator per state file.** A live run writes a heartbeat to the state file every 15 s from a background thread, so it stays live throughout provisioning. `conductor run` refuses to start while another run is live, and `conductor teardown` / `conductor sync` refuse too, so two processes never fight over the same pods; pass `--force` if the other process is really dead. `conductor status` shows whether the orchestrator is live. `run`, `teardown` and `sync` also write a rotating `conductor.log` next to the state file.

**`conductor teardown`** records what it did: the registry, a `pod_stopped` cost event (reason `manual_teardown`, with the cost accrued since the pod was created) and the job's status (`failed`, "torn down manually"), so `conductor status` and `conductor report` agree with the provider afterwards.

## Lifecycle

```
Provision → Deploy → Launch → Monitor → Teardown
    ↑                            │
    └──── Spot Recovery ─────────┘
```

1. **Provision** — create RunPod pod, wait for SSH
2. **Deploy** — rsync code + run setup command (or skip for prebuilt images)
3. **Launch** — start `run_command` under `setsid nohup`; the exit code is written to `.conductor_exit` when it ends
4. **Monitor** — poll the exit file and process, sync results, track costs, handle interruptions. Exit 0 completes the job at once; a non-zero exit fails it with the last 50 log lines in the error
5. **Teardown** — final sync (retried; if it keeps failing after a successful job the pod is kept alive and the job marked `sync_failed`; a failed job is torn down regardless unless `keep_pod_on_failure = true`), then terminate and verify with the provider that the pod is gone

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
