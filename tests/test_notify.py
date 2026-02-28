from unittest.mock import patch, MagicMock

from conductor.config import NotificationConfig
from conductor.notify import send_notification, _should_notify, _format_message


def test_disabled_when_no_config():
    assert send_notification(None, "job_complete", job="a") is False


def test_should_notify_filters():
    cfg = NotificationConfig(on_job_complete=True, on_job_failure=False, on_spot_recovery=True)
    assert _should_notify(cfg, "job_complete") is True
    assert _should_notify(cfg, "job_failed") is False
    assert _should_notify(cfg, "spot_recovery") is True


def test_format_message():
    msg = _format_message("job_complete", {"job": "train", "cost_usd": 1.23, "gpu_type": "RTX A4000"})
    assert "train" in msg
    assert "$1.23" in msg
    assert "RTX A4000" in msg


@patch("conductor.notify.subprocess.run")
@patch("conductor.notify.shutil.which", return_value="/usr/local/bin/terminal-notifier")
def test_terminal_notifier(mock_which, mock_run):
    cfg = NotificationConfig(backend="terminal-notifier")
    result = send_notification(cfg, "job_complete", job="train")
    assert result is True
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "terminal-notifier"


@patch("conductor.notify.subprocess.run")
@patch("conductor.notify.shutil.which", return_value=None)
def test_terminal_notifier_fallback_osascript(mock_which, mock_run):
    cfg = NotificationConfig(backend="terminal-notifier")
    result = send_notification(cfg, "job_complete", job="train")
    assert result is True
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "osascript"


@patch("conductor.notify.subprocess.run")
def test_command_backend(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    cfg = NotificationConfig(backend="command", notify_command="python notify.py")
    result = send_notification(cfg, "job_complete", job="train")
    assert result is True
    assert mock_run.call_args[1]["input"]  # JSON payload on stdin
