# Changelog

## 0.6.0 (unreleased)

Reliability release — see `RELIABILITY_REVIEW.md` for the findings that drove it.

- Crash-proof the lifecycle: SSH/rsync timeouts and OS errors are returned as failed results, provider/SDK errors are converted to `ConductorError`, and any exception in a job's provision/deploy/launch or monitor tick marks that job `failed` with `job.error` set instead of killing the orchestrator. If the orchestrator does die, `run_lifecycle` saves state, prints every live pod ID with its job, sends an `orchestrator_crashed` notification and (only if `teardown_on_crash = true`) tears the pods down
- Supervise the remote job: `run_command` is launched with `setsid nohup` from a generated run script that writes the exit code to `.conductor_exit` the moment it ends; the pid and its start time (`/proc/<pid>/stat`, so a reused pid is not mistaken for the job) are recorded in state. `is_alive()` now returns a tri-state `ProcessStatus` (running / exited with code / unknown on SSH failure). Exit 0 completes the job immediately; a non-zero exit fails it immediately with the last 50 log lines in `job.error`. `idle_timeout_minutes` only applies when a process vanishes without an exit file. `conductor status` and the run table show the exit code
- New config fields: `setup_timeout_seconds` (default 1800, was a hard-coded 600), `deploy_timeout_seconds` (600), `sync_timeout_seconds` (600), `teardown_on_crash` (false)
- Fix rsync install command parenthesisation (install always ran and failed on non-apt images)
- Fix `RunPodProvider._get_cost` crashing after pod creation when the API returned no `costPerHr`

## 0.5.1

- Fix `keep_pod_alive` registry bug: pods kept alive on completion were incorrectly marked as `terminated` in the global registry, making them invisible to `conductor status` and preventing reuse
- Fix one-way reconciliation: `reconcile_with_provider()` now resurrects pods that are marked `terminated` in the registry but still running on the provider
- Fix reconciliation using unconfigured providers: provider API keys were not being set, causing all API calls to fail silently. Extract `get_configured_provider()` for shared use

## 0.5.0

- Add multi-provider support (Vast.ai)
- Fix orphan pod bug
- Reconcile registry with provider

## 0.4.0

- Add kubectl-style imperative pod commands and aggressive cost defaults

## 0.3.0

- Fix cost tracking: get pod cost from RunPod API instead of get_gpus()

## 0.2.0

- Add GPU stall detection to catch hung processes with idle GPUs

## 0.1.0

- Make GPU/CPU utilization opt-in via conductor status --util
