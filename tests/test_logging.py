import logging
import os

from conductor.cli import LOG_FORMAT, add_log_file, configure_logging, log_file_for, main


SDK_FMT = "%(filename)-20s:%(lineno)-4d %(asctime)s %(message)s"


def test_configure_logging_overrides_sdk_basic_config():
    # What `import runpod` does behind our back
    logging.basicConfig(level=logging.INFO, format=SDK_FMT, handlers=[logging.StreamHandler()], force=True)
    configure_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.handlers[0].formatter._fmt == LOG_FORMAT
    record = logging.LogRecord("conductor.monitor", logging.INFO, "monitor.py", 435, "hello", None, None)
    line = root.handlers[0].format(record)
    assert line.endswith("INFO hello") and "monitor.py" not in line


def test_add_log_file_writes_timestamped_lines(tmp_dir):
    configure_logging()
    path = log_file_for(os.path.join(tmp_dir, ".conductor_state.json"))
    assert path == os.path.join(tmp_dir, "conductor.log")
    handler = add_log_file(path)
    try:
        logging.getLogger("conductor.monitor").warning("[train] something happened")
        handler.flush()
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()
    with open(path) as f:
        line = f.read().strip()
    assert line.endswith("WARNING conductor.monitor [train] something happened")
    assert line[:4].isdigit() and line[4] == "-"   # full date, not just %H:%M:%S


def test_add_log_file_survives_unwritable_dir(tmp_dir):
    configure_logging()
    assert add_log_file(os.path.join(tmp_dir, "no", "such", "dir", "conductor.log")) is None


def test_run_logs_to_project_log_file(tmp_dir, monkeypatch):
    from unittest.mock import MagicMock
    from tests.test_cli import _write_run_fixture
    from conductor.state import PodState
    config_path, state_path = _write_run_fixture(tmp_dir, [PodState(name="train"), PodState(name="eval")])
    monkeypatch.setattr("conductor.cli._configure_provider", lambda name: MagicMock())
    monkeypatch.setattr("conductor.cli.run_lifecycle", lambda configs, state, *a, **k: state)
    main(["run", "--config", config_path])
    log_path = os.path.join(tmp_dir, "conductor.log")
    assert os.path.exists(log_path)
    with open(log_path) as f:
        content = f.read()
    assert "run abc123 started" in content
    # Every main() call owns the root logger afresh: no duplicate handlers accumulate
    main(["run", "--config", config_path])
    assert len(logging.getLogger().handlers) == 2  # stderr + this run's file
