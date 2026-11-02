# Reliability review (2 November 2026, v0.5.1)

Scope: full read of `src/conductor/`, the poker-ninja configs that drive
it, and the only surviving log of a real run
(`poker-ninja/logs/conductor.log`, wave 2b, four pods). The headline
findings were verified line by line against the source. Nothing was
modified; the 115 unit tests pass.

## Why runs have felt flaky

The tool is sound in the happy path and brittle everywhere else: most
error paths either crash the orchestrator (leaving pods billing) or
misclassify a transient fault as a spot interruption (destroying a healthy
pod). The wave 2b log is consistent with the first of these: four pods sat
in `deploying` for nine to ten minutes, cost showed $0.00 throughout, and
the log ends abruptly at about the 600-second mark with no error line.

## Findings, by impact

1. **Uncaught exceptions kill the orchestrator and orphan every pod.**
   `ssh_exec` is `subprocess.run(..., timeout=...)` (`ssh.py:21`), which
   raises `TimeoutExpired`. Nothing catches it in `_run_setup`
   (`deployer.py:120-124`, timeout hard-coded to 600 s), `_install_rsync`,
   `rsync`, `rsync_pull`, or `launch_direct`. The exception propagates
   through the thread pool to `main()`, which prints and exits; the
   lifecycle's `finally` (`monitor.py:80-82`) only saves state, no
   teardown. poker-ninja's `setup_command` does `uv sync` plus a 2.5 GB
   torch install on a cold pod, so exceeding 600 s is the expected case.

2. **A transient API or SSH error is treated as a spot interruption, and
   the response is to terminate the healthy pod.** `check_exists` returns
   `False` on any exception (`providers/runpod.py:85-92`). In
   `_monitor_tick`, SSH unreachable plus `check_exists() == False` means
   "pod gone" (`monitor.py:166-171`) and `_spot_recover` tears the pod
   down and re-provisions from scratch. A laptop wake before DNS is up, a
   RunPod 429/502 (the SDK has no retry and calls `.json()` on the body), or
   a 15-second SSH hiccup loses everything since the last sync.

3. **Restart and Ctrl-C semantics are wrong in both directions.** After a
   crash, jobs persisted as `provisioning`/`deploying` are handled by
   neither `_start_unblocked_jobs` (pending only) nor `_monitor_tick`
   (running only), so `conductor run` loops every 30 s doing nothing while
   pods bill. Completed or failed jobs cannot be retried without deleting
   the state file, which also discards live pod IDs. Ctrl-C terminates
   pods; there is no detach-and-resume. `conductor teardown`/`sync` mutate
   the same state file as a live `run` with no lock or owner PID, and a
   live `run` will then see the pods you terminated as "gone" and
   re-provision them. The pod ID is persisted only after create plus up to
   ten minutes of SSH waits (`monitor.py:251-267`); a death in that window
   leaves a pod recorded nowhere, and nothing ever lists the account's pods
   to find orphans.

4. **Job completion is "process disappeared, then wait idle_timeout";
   exit codes are never read.** The launch script backgrounds the command
   with no `nohup`/`setsid` and no exit file (`runner.py:25-29`);
   `is_alive` is `kill -0`. A job that crashes is reported `completed` and
   its dependents start. Both wave 2b crashes (critic `load_state_dict`
   size mismatch, exit 1) would have read as success. The idle wait is pure
   billing: 10 minutes per pod in `conductor.toml`, and 180 minutes in the
   adaptive and enhanced configs, after a 30-minute job.

5. **Cost is a wall-clock estimate that ignores provisioning, and the
   budget stop is unverified.** `_update_cost` only runs for `running` jobs
   (`monitor.py:95-104, 405-412`), so deploy time and orphaned pods never
   count (hence $0.00). After recovery onto a different GPU the new rate is
   applied over the original `started_at`. On budget breach the return
   value of `teardown_pod` is ignored in six places; a failed terminate
   leaves a pod billing while the registry says terminated. The 80%
   notification fires every tick with no latch.

6. **Spot-recovery accounting.** `provision_attempts` counts GPU
   candidates, not recoveries, so with a 14-entry fallback list one
   provision can exhaust `max_provision_attempts=5` and the first real
   interruption fails the job. Backoff `time.sleep` blocks the whole loop.
   `sync_push` runs after `launch`, so a resumed job starts before its
   checkpoints arrive (the old in-repo pipeline did this in the right
   order). If SSH is down but the pod exists, nothing refreshes host/port
   from the API, so a port remap leaves a job unmonitored until the budget
   trips.

7. **A failed final sync still tears the pod down.** `_finish_job`
   ignores `sync_pull`'s result and terminates (`monitor.py:374-379`).
   rsync has no `--partial`/`--timeout`; SSH has no `ConnectTimeout`,
   `ServerAliveInterval` or `BatchMode`, so half-open connections hang
   until the Python timeout and then hit finding 1.

8. **Provisioning timeouts are hard-coded at 300 s + 300 s** and the
   pytorch devel image routinely takes longer to pull on community hosts,
   so pods are created, billed and terminated (result ignored) up to 14
   times. `get_gpu_catalog` is uncaught; `create_instance` swallows
   errors, so a 429 on the cheap GPU silently escalates to a pricier one.

Smaller bugs: the rsync install command is mis-parenthesised so install
always runs and fails on non-apt images (`deployer.py:95`); `is_alive`
returns `False` rather than `None` without a pid, so a pid-less running job
is "completed" after the idle timeout; `keep_pod_alive` reuse never
re-reads SSH host/port; the registry is read-modify-write with no lock and
is called from pool threads (last writer wins, which matches the "pods
invisible to status" symptom patched in 0.5.1); `_graceful_shutdown`
ignores jobs in `provisioning`/`deploying`.

## Observability

Logging goes to stderr, the status table to stdout, and the tool writes no
log file of its own; the wave 2b log therefore contains status tables and
nothing else. `job.error` is only set on handled paths. `conductor status`
cannot tell whether the orchestrator is alive, needs an API key even for
the offline registry path, and shows no exit code.

## Testing

115 fast mock-based unit tests cover config, validation, state round
trips, the deploy/launch/sync happy paths and a few branches of
`_monitor_tick`. Not covered: the lifecycle end to end, spot recovery,
finish/teardown, shutdown, restart from saved state, any timeout or API
error path, registry concurrency, cost after recovery, the budget stop.
`CloudProvider` is a Protocol and easy to fake; SSH/rsync are bare
functions imported by name, so there is no transport seam to fake a pod.

## Recommended changes, in order

Small means hours; medium about a day; large several days.

1. **Crash-proof the lifecycle (small).** Map `TimeoutExpired`, `OSError`,
   `RunPodError` and JSON errors to a job `failed` with `error` set, at
   every ssh/rsync/provider call. Add a `BaseException` handler in
   `run_lifecycle` that saves state, prints every live pod ID, and
   optionally tears down. Make the setup timeout configurable, or run
   setup detached and poll it like a job.
2. **Supervise the remote job with a PID and exit file (small).**
   `setsid nohup bash -c '(cmd); echo $? > .conductor_exit'`; record the
   pid and its start time; `is_alive` reads the exit file so completion
   and failure are immediate and exit codes are real. Keep `idle_timeout`
   only as a safety net. This also removes the 10 to 180 minute idle burn.
3. **Tri-state existence and confirmation before recovery (small to
   medium).** `check_exists` returns True/False/None; recover only when the
   API reports the pod exited or missing without an exception, on several
   consecutive polls; when SSH is down but the pod exists, refresh host and
   port from the API.
4. **Persist pod IDs first and reconcile by run ID (medium).** Write the
   pod ID the instant `create_pod` returns, name pods
   `conductor-<run_id>-<job>`, and on startup list the account's pods:
   adopt matches into state, terminate matches not in state, and resume
   `provisioning`/`deploying` jobs instead of looping.
5. **Run ownership, detach and resume (medium).** State carries the
   orchestrator PID, a heartbeat and a run ID; `run` refuses to start on a
   fresh heartbeat; `teardown`/`sync` refuse or signal when a run is live;
   Ctrl-C detaches by default and `conductor run` resumes; `--teardown`
   for the destructive version; `--retry-failed`.
6. **Retries with jitter and SSH hardening (small).** Three to five
   attempts with exponential backoff around every provider and ssh/rsync
   call; `-o ConnectTimeout=15 -o ServerAliveInterval=15
   -o ServerAliveCountMax=3 -o BatchMode=yes`; `rsync --partial
   --timeout=120`; configurable SSH wait timeouts (default 15 min or more);
   verify terminate by polling; never terminate after a failed final sync.
7. **Real cost accounting and a verified hard stop (small to medium).**
   Accrue from pod creation in every state, per pod segment with its own
   start and rate, re-poll the rate, latch the threshold notification, and
   on breach terminate, verify, then notify.
8. **Fix recovery accounting (small).** Count recoveries not candidates;
   push before launch; make backoff non-blocking via a `next_attempt_at`
   in state.
9. **Observability (small).** Rotating log file with timestamps in the
   project dir, status table only on change, orchestrator liveness and
   last error in `status`, an `atexit` notification on orchestrator death,
   `status` working without an API key.
10. **Offline end-to-end tests (large).** A `Transport` protocol injected
    into deployer/runner/syncer, a fake provider and fake pod with a process
    table and scripted faults, a `--simulate` mode with a fake clock, and
    lifecycle tests for crash-and-restart, spot recovery, budget stop and
    concurrent registry writes.

## Immediate config advice for poker-ninja

Until items 1 and 2 land: expect the current `setup_command` to exceed the
600-second limit on a cold pod (pre-bake the image or split the install),
and set `idle_timeout_minutes` to a few minutes in the adaptive and
enhanced configs rather than 180.
