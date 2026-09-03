"""Best-effort desktop notifications for events in the activity feed.

A notification is a courtesy, never a requirement: a machine with no notifier
installed, or a call that fails for any reason, must leave the watch and panel
loops running exactly as they would without this module. Nothing here raises.

New triggers join ``NOTIFICATION_RULES`` - the loops themselves never change.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any, Callable

NotificationRule = Callable[[dict[str, Any]], "tuple[str, str] | None"]


def _applescript_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_desktop_notification(title: str, message: str, subtitle: str = "") -> None:
    """Show one notification, or do nothing if that is not possible here."""
    try:
        if sys.platform == "darwin":
            script = (
                f'display notification "{_applescript_string(message)}"'
                f' with title "{_applescript_string(title)}"'
            )
            if subtitle:
                script += f' subtitle "{_applescript_string(subtitle)}"'
            subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        elif shutil.which("notify-send"):
            body = f"{subtitle}\n{message}" if subtitle else message
            subprocess.run(
                ["notify-send", "--", title, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        pass


def _test_failed(event: dict[str, Any]) -> tuple[str, str] | None:
    if event.get("kind") != "test" or event.get("status") != "failed":
        return None
    title = str(event.get("title") or "Tests failed")
    detail = event.get("detail")
    message = str(detail) if detail else "A test run failed."
    return title, message


NOTIFICATION_RULES: list[NotificationRule] = [_test_failed]


def notify_for_event(root_label: str, event: dict[str, Any]) -> None:
    """Send a notification for one new event, if any rule wants to."""
    for rule in NOTIFICATION_RULES:
        found = rule(event)
        if found is not None:
            title, message = found
            send_desktop_notification(title, message, subtitle=root_label)
            return
