# Contributing to Cloud Conductor

Thanks for your interest in contributing! This document covers the basics.

## Getting Started

```bash
git clone https://github.com/YOUR_USER/cloud-conductor.git
cd cloud-conductor
uv sync
uv run pytest
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Development Workflow

1. Create a branch from `main`
2. Make your changes
3. Run tests: `uv run pytest`
4. Commit with a clear message describing *why*, not just *what*
5. Open a pull request

## Running Tests

```bash
uv run pytest              # all tests
uv run pytest -x -q        # stop on first failure
uv run pytest tests/test_config.py  # single file
```

All tests use mocks for external dependencies (RunPod API, SSH, rsync) so no API keys or pods are needed.

## Project Structure

```
src/conductor/
├── cli.py          # argparse entry point
├── config.py       # TOML config loading
├── state.py        # state file + cost log I/O
├── ssh.py          # SSH/rsync helpers
├── gpu_pricing.py  # RunPod GPU price queries
├── provisioner.py  # pod creation + teardown
├── deployer.py     # rsync code + setup
├── runner.py       # job launch + process checks
├── syncer.py       # result sync (pull/push)
├── monitor.py      # main lifecycle loop
├── notify.py       # notification dispatch
└── validator.py    # pre-flight validation
```

## Guidelines

- **Keep it simple** — this project intentionally has minimal dependencies (no click, no rich). Stdlib where possible.
- **Test your changes** — every module has a corresponding test file. Add tests for new behaviour.
- **No unnecessary abstractions** — prefer clear, direct code over clever patterns.
- **Mock external calls** — tests should not hit the RunPod API or require SSH. Use `unittest.mock`.

## Design Decisions

- `run_command` is opaque — the conductor never parses or constructs it
- Per-job overrides — any `[global]` config field can be overridden in a `[[jobs]]` entry
- State file is the source of truth — `conductor status` reads it directly
- Cost tracking is approximate — elapsed time × hourly rate

See [SPEC.md](SPEC.md) for the full specification.

## Reporting Issues

Open an issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your Python version and OS

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE). Copyright (c) 2026 Steve Flinter.
