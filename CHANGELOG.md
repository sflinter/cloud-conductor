# Changelog

## 0.6.1

Fixes from the first live RunPod trial of 0.6.0.

- The heartbeat is now refreshed by a daemon thread every `POLL_INTERVAL/2` for the whole lifecycle, not once per monitor tick. Previously `conductor status` reported "orchestrator not running" and the live-run guards in `run`/`teardown`/`sync` stopped refusing during exactly the window in which pods were being created, because `_start_unblocked_jobs` blocks for up to `provision_timeout_seconds`. `save_state` snapshots and writes under a process-wide lock so the heartbeat can never overwrite a newer save
- A single Ctrl-C now detaches during provisioning. The first Ctrl-C sets a process-wide shutdown flag that `wait_ssh`, both providers' `wait_for_ssh` loops, the ssh/rsync and provider retry backoffs, the spot-recovery backoff and the new provisioning backoff all check between polls; the pipeline stops at the next check, keeps the job's persisted status and pod ID, and the run detaches printing the live pod IDs. Deploy and launch are not started once the flag is set. The second Ctrl-C hard-exit is unchanged
- An SSH key that sshd rejects fails the job fast: three consecutive probes answered with `Permission denied`, `Too many authentication failures` or `no such identity` raise `SshAuthError`; the pod is terminated and no further GPU candidate is tried with the same key (previously every candidate was billed for the whole `provision_timeout_seconds`). `conductor validate` and `conductor run` now also check that the key is usable non-interactively (`ssh-keygen -y -P '' -f <key>` succeeds, or `ssh-add -l` lists its fingerprint) and otherwise stop with "SSH key is passphrase-protected and not loaded in ssh-agent: run `ssh-add --apple-use-keychain <key>` or use an unencrypted key"
- `conductor teardown` records what it did: the registry record becomes `terminated` (or stays `running` when the terminate could not be verified), a `pod_stopped` cost event with reason `manual_teardown` and the accrued cost is written, and the job is marked `failed` with `error = "torn down manually"`. Cost is accrued from pod creation (`started_at`) for every job in `provisioning`/`deploying`/`running`/`sync_failed` by `conductor status`, `conductor teardown` and the monitor (which previously only counted `running` jobs), so a pod that has been billing for minutes no longer shows `$0.00`. `conductor report` pairs start/stop events by pod ID and records `gpu_type` on stop events
- GPU availability: a create failure that is an availability error (RunPod's "no longer any instances available" and similar; no matching Vast.ai offer) raises `NoCapacityError`, and `provision_pod` walks the whole candidate list again after a jittered `backoff_base_seconds * 2^(pass-1)` wait, up to `provision_retries` passes (new config key, default 3). A pass, not a candidate, counts towards `max_provision_attempts`. Other create failures are not retried
- Logging: the `runpod` SDK calls `logging.basicConfig()` with its own format at import, which made the conductor's own configuration a no-op (hence `monitor.py :435 <time>` lines in the run log). The root logger is now configured explicitly with `force=True` after importing the SDK. `run`, `teardown` and `sync` also log to a rotating `conductor.log` (5 MB x 3, full timestamps and logger names) next to the state file
- New config field: `provision_retries` (default 3)

## 0.6.0

Reliability release — see `RELIABILITY_REVIEW.md` for the findings that drove it.

- Crash-proof the lifecycle: SSH/rsync timeouts and OS errors are returned as failed results, provider/SDK errors are converted to `ConductorError`, and any exception in a job's provision/deploy/launch or monitor tick marks that job `failed` with `job.error` set instead of killing the orchestrator. If the orchestrator does die, `run_lifecycle` saves state, prints every live pod ID with its job, sends an `orchestrator_crashed` notification and (only if `teardown_on_crash = true`) tears the pods down
- Supervise the remote job: `run_command` is launched with `setsid nohup` from a generated run script that writes the exit code to `.conductor_exit` the moment it ends; the pid and its start time (`/proc/<pid>/stat`, so a reused pid is not mistaken for the job) are recorded in state. `is_alive()` now returns a tri-state `ProcessStatus` (running / exited with code / unknown on SSH failure). Exit 0 completes the job immediately; a non-zero exit fails it immediately with the last 50 log lines in `job.error`. `idle_timeout_minutes` only applies when a process vanishes without an exit file. `conductor status` and the run table show the exit code
- Tri-state pod existence: `check_exists()` returns `True` / `False` / `None` (`None` on any exception or non-JSON response) for both RunPod and Vast.ai, and the registry reconciliation, `conductor status` and pod reuse all treat `None` as "could not tell". The monitor only treats a pod as reclaimed when the provider reports it exited or missing, without exception, on `recovery_confirmations` consecutive ticks (default 3). When SSH is unreachable but the pod exists, `ssh_host`/`ssh_port` are refreshed from the provider (`get_ssh_info()`, new on the provider protocol) before the next tick; `keep_pod_alive` reuse also re-reads SSH info
- Persist pod IDs first and reconcile by run ID: every `conductor run` has a short `run_id` (stored in the state file) and names its pods `conductor-<run_id>-<job>`. The pod ID is written to the state file and the global registry the instant the provider returns it, before any SSH wait. On startup `run` lists the account's pods (`list_instances()`, new on the provider protocol), adopts pods of this run that are missing from state, terminates (loudly) pods of this run that no job tracks, and resumes jobs persisted as `provisioning`/`deploying` (re-check pod, re-deploy, launch) or `running` without a pid instead of looping forever. `--retry-failed` and `--retry-completed` re-run finished jobs without deleting the state file
- Run ownership, detach and resume: the state file carries `orchestrator_pid` and a `heartbeat_at` updated every tick. `conductor run` refuses to start on a state file whose heartbeat is fresher than two poll intervals unless `--force`; `conductor teardown` and `conductor sync` refuse while a run is live unless `--force`. **Ctrl-C now detaches by default**: it prints the live pod IDs and how to resume, marks nothing failed and terminates nothing; `conductor run --teardown-on-interrupt` restores the old sync-and-terminate behaviour (which now also covers jobs still provisioning/deploying). A second Ctrl-C hard-exits after printing the live pod IDs. `conductor status` shows whether the orchestrator is live
- Retries with jitter and SSH hardening: a `retry()` helper (4 attempts, exponential backoff with jitter, capped at 30 s; 429/5xx and transport errors retried, other 4xx and authentication errors not) wraps every idempotent provider call on both providers; `ssh_exec` retries connection failures (rc 255) and `rsync` retries socket/protocol/timeout codes, both with backoff. SSH now uses `ConnectTimeout=15 ServerAliveInterval=15 ServerAliveCountMax=3 BatchMode=yes`; rsync adds `--partial --timeout=120`. `provision_timeout_seconds` (default 900, was a hard-coded 300 + 300) bounds the wait for a new pod's SSH. `teardown_pod()` retries the terminate call and then polls the provider until the pod is reported gone (bounded); an unverified teardown is recorded as such (registry keeps the pod as running, `job.error` says so) instead of being assumed. The final sync in `_finish_job` is retried up to 3 times; if it still fails the pod is kept alive, the job is marked `sync_failed` and a `sync_failed` notification is sent instead of terminating (budget stops still force the teardown). `conductor sync` also syncs `sync_failed` jobs
- The state file and the global pod registry are guarded by a cross-process `flock` on a hidden `.lock` sibling for every read-modify-write, including from the pool threads (fixes lost registry updates)
- New config fields: `setup_timeout_seconds` (default 1800, was a hard-coded 600), `deploy_timeout_seconds` (600), `sync_timeout_seconds` (600), `teardown_on_crash` (false), `recovery_confirmations` (3), `provision_timeout_seconds` (900)
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
