# Changelog

## 0.5.1

- Fix `keep_pod_alive` registry bug: pods kept alive on completion were incorrectly marked as `terminated` in the global registry, making them invisible to `conductor status` and preventing reuse

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
