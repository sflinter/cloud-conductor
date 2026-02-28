from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone

from conductor.config import NotificationConfig

log = logging.getLogger(__name__)


def send_notification(config: NotificationConfig | None, event: str, **kwargs) -> bool:
    if not config:
        return False
    if not _should_notify(config, event):
        return False

    payload = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **kwargs,
    }

    title = f"Conductor: {event.replace('_', ' ')}"
    message = _format_message(event, kwargs)

    try:
        if config.backend == "terminal-notifier":
            return _notify_terminal(title, message)
        elif config.backend == "pushover":
            return _notify_pushover(config, title, message, event)
        elif config.backend == "command":
            return _notify_command(config, payload)
        else:
            log.warning(f"Unknown notification backend: {config.backend}")
            return False
    except Exception as e:
        log.warning(f"Notification failed: {e}")
        return False


def _should_notify(config: NotificationConfig, event: str) -> bool:
    mapping = {
        "job_complete": config.on_job_complete,
        "job_failed": config.on_job_failure,
        "spot_recovery": config.on_spot_recovery,
        "budget_threshold": config.on_budget_threshold > 0,
        "budget_exceeded": config.on_job_failure,
        "job_budget_exceeded": config.on_job_failure,
    }
    return mapping.get(event, True)


def _format_message(event: str, kwargs: dict) -> str:
    job = kwargs.get("job", "")
    parts = [f"Job: {job}"] if job else []

    if "cost_usd" in kwargs:
        parts.append(f"Cost: ${kwargs['cost_usd']:.2f}")
    if "elapsed_hours" in kwargs:
        h = kwargs["elapsed_hours"]
        parts.append(f"Duration: {int(h)}h {int((h % 1) * 60)}m")
    if "gpu_type" in kwargs:
        parts.append(f"GPU: {kwargs['gpu_type']}")
    if "error" in kwargs:
        parts.append(f"Error: {kwargs['error']}")

    return " | ".join(parts) if parts else event.replace("_", " ")


def _notify_terminal(title: str, message: str) -> bool:
    if shutil.which("terminal-notifier"):
        subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message,
             "-group", "conductor"],
            capture_output=True,
        )
        return True
    else:
        # Fallback to osascript
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], capture_output=True)
        return True


def _notify_pushover(config: NotificationConfig, title: str, message: str, event: str) -> bool:
    import os
    user_key = config.pushover_user_key or os.environ.get("PUSHOVER_USER_KEY", "")
    app_token = config.pushover_app_token or os.environ.get("PUSHOVER_APP_TOKEN", "")
    if not user_key or not app_token:
        log.warning("Pushover credentials not configured")
        return False

    priority = 1 if event in ("job_failed", "budget_exceeded", "job_budget_exceeded") else 0
    data = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": priority,
    }

    try:
        import httpx
        resp = httpx.post("https://api.pushover.net/1/messages.json", data=data)
        return resp.status_code == 200
    except ImportError:
        import urllib.request
        import urllib.parse
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json",
            data=urllib.parse.urlencode(data).encode(),
        )
        resp = urllib.request.urlopen(req)
        return resp.status == 200


def _notify_command(config: NotificationConfig, payload: dict) -> bool:
    if not config.notify_command:
        log.warning("notify_command not configured")
        return False
    result = subprocess.run(
        config.notify_command, shell=True,
        input=json.dumps(payload), text=True,
        capture_output=True, timeout=30,
    )
    return result.returncode == 0
