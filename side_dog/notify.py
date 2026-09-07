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
import threading
from queue import Full, Queue
from typing import Any, Callable

NotificationRule = Callable[[dict[str, Any]], "tuple[str, str] | None"]

PERSISTENT_NOTIFICATION_SECONDS = 30


def _applescript_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_desktop_notification(
    title: str,
    message: str,
    subtitle: str = "",
    *,
    persistent: bool = False,
) -> None:
    """Show one notification, or do nothing if that is not possible here."""
    try:
        if sys.platform == "darwin":
            if persistent:
                body = f"{subtitle}\n\n{message}" if subtitle else message
                script = (
                    f'display dialog "{_applescript_string(body)}"'
                    f' with title "{_applescript_string(title)}"'
                    ' buttons {"Dismiss"} default button "Dismiss"'
                    f" with icon caution giving up after {PERSISTENT_NOTIFICATION_SECONDS}"
                )
            else:
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
                timeout=PERSISTENT_NOTIFICATION_SECONDS + 5 if persistent else 5,
                check=False,
            )
        elif shutil.which("notify-send"):
            body = f"{subtitle}\n{message}" if subtitle else message
            options = (
                [
                    "--urgency=critical",
                    f"--expire-time={PERSISTENT_NOTIFICATION_SECONDS * 1000}",
                ]
                if persistent
                else []
            )
            subprocess.run(
                ["notify-send", *options, "--", title, body],
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

# The subtitle every board message carries, so the desktop shows where it
# came from without the message itself naming a folder.
BOARD_SUBTITLE = "Side Dog board"

# Desktop adapters are external conveniences and must never become feed
# backpressure. Ordinary notifications and serialized conflict dialogs have
# separate bounded workers. Failed-test dialogs use bounded independent workers,
# so a 30-second dialog cannot delay another failure or a conflict warning.
_NOTIFICATION_QUEUE: Queue[tuple[str, str, str, bool]] = Queue(maxsize=16)
_PERSISTENT_NOTIFICATION_QUEUE: Queue[tuple[str, str, str, bool]] = Queue(maxsize=4)
_PARALLEL_PERSISTENT_NOTIFICATION_SLOTS = threading.BoundedSemaphore(4)
_NOTIFICATION_WORKER_LOCK = threading.Lock()
_NOTIFICATION_WORKER: threading.Thread | None = None
_PERSISTENT_NOTIFICATION_WORKER: threading.Thread | None = None


def _notification_worker(
    notifications: Queue[tuple[str, str, str, bool]],
) -> None:
    while True:
        title, message, subtitle, persistent = notifications.get()
        try:
            send_desktop_notification(title, message, subtitle, persistent=persistent)
        except Exception:
            # Rules and adapters are third-party extension points. Preserve the
            # module's promise even when one raises an unexpected exception.
            pass
        finally:
            notifications.task_done()


def _ensure_notification_worker() -> bool:
    global _NOTIFICATION_WORKER
    with _NOTIFICATION_WORKER_LOCK:
        if _NOTIFICATION_WORKER is not None and _NOTIFICATION_WORKER.is_alive():
            return True
        _NOTIFICATION_WORKER = threading.Thread(
            target=_notification_worker,
            name="side-dog-notifications",
            args=(_NOTIFICATION_QUEUE,),
            daemon=True,
        )
        try:
            _NOTIFICATION_WORKER.start()
        except (OSError, RuntimeError):
            _NOTIFICATION_WORKER = None
            return False
        return True


def _ensure_persistent_notification_worker() -> bool:
    global _PERSISTENT_NOTIFICATION_WORKER
    with _NOTIFICATION_WORKER_LOCK:
        if (
            _PERSISTENT_NOTIFICATION_WORKER is not None
            and _PERSISTENT_NOTIFICATION_WORKER.is_alive()
        ):
            return True
        _PERSISTENT_NOTIFICATION_WORKER = threading.Thread(
            target=_notification_worker,
            name="side-dog-persistent-notifications",
            args=(_PERSISTENT_NOTIFICATION_QUEUE,),
            daemon=True,
        )
        try:
            _PERSISTENT_NOTIFICATION_WORKER.start()
        except (OSError, RuntimeError):
            _PERSISTENT_NOTIFICATION_WORKER = None
            return False
        return True


def _send_parallel_persistent_notification(
    title: str,
    message: str,
    subtitle: str,
) -> None:
    """Run one persistent alert independently, bounded by the acquired slot."""
    try:
        send_desktop_notification(title, message, subtitle, persistent=True)
    except Exception:
        pass
    finally:
        _PARALLEL_PERSISTENT_NOTIFICATION_SLOTS.release()


def _dispatch_parallel_persistent_notification(
    title: str,
    message: str,
    subtitle: str,
) -> None:
    """Start one independent persistent alert, dropping excess bursts."""
    if not _PARALLEL_PERSISTENT_NOTIFICATION_SLOTS.acquire(blocking=False):
        return
    worker = threading.Thread(
        target=_send_parallel_persistent_notification,
        name="side-dog-persistent-notification",
        args=(title, message, subtitle),
        daemon=True,
    )
    try:
        worker.start()
    except (OSError, RuntimeError):
        _PARALLEL_PERSISTENT_NOTIFICATION_SLOTS.release()


def dispatch_desktop_notification(
    title: str,
    message: str,
    subtitle: str = "",
    *,
    persistent: bool = False,
    parallel: bool = False,
) -> None:
    """Queue one best-effort notification without delaying event polling."""
    if persistent and parallel:
        _dispatch_parallel_persistent_notification(title, message, subtitle)
        return
    queue = (
        _PERSISTENT_NOTIFICATION_QUEUE if persistent else _NOTIFICATION_QUEUE
    )
    ready = (
        _ensure_persistent_notification_worker()
        if persistent
        else _ensure_notification_worker()
    )
    if not ready:
        return
    try:
        queue.put_nowait((title, message, subtitle, persistent))
    except Full:
        # A notification burst is less important than a responsive live feed.
        pass


def notify_for_event(root_label: str, event: dict[str, Any]) -> None:
    """Send a notification for one new event, if any rule wants to."""
    for rule in NOTIFICATION_RULES:
        found = rule(event)
        if found is not None:
            title, message = found
            dispatch_desktop_notification(
                title,
                message,
                subtitle=root_label,
                persistent=True,
                parallel=True,
            )
            return


def notify_for_board(
    title: str, message: str, *, persistent: bool = False
) -> None:
    """Send one board transition through the same desktop path as feed events.

    ``side-dog board`` works out what changed between two frames; this is the
    only door it uses, so the platform adapters, the bounded queue, and the
    never-raise promise above apply to it exactly as they do to test failures.
    """
    if persistent:
        dispatch_desktop_notification(
            title, message, subtitle=BOARD_SUBTITLE, persistent=True
        )
    else:
        dispatch_desktop_notification(title, message, subtitle=BOARD_SUBTITLE)
