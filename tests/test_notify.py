import subprocess
import threading
import time
from unittest import TestCase
from unittest.mock import patch

from side_dog.notify import (
    BOARD_SUBTITLE,
    PERSISTENT_NOTIFICATION_SECONDS,
    dispatch_desktop_notification,
    notify_for_board,
    notify_for_event,
    send_desktop_notification,
)


class TestFailureRuleTest(TestCase):
    def test_a_failed_test_event_notifies_with_its_own_title_and_detail(self) -> None:
        event = {
            "kind": "test",
            "status": "failed",
            "title": "Tests failed",
            "detail": "pytest",
        }
        with patch("side_dog.notify.dispatch_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_called_once_with(
            "Tests failed", "pytest", subtitle="my-project", persistent=True
        )

    def test_a_passing_test_event_does_not_notify(self) -> None:
        event = {"kind": "test", "status": "success", "title": "Tests passed"}
        with patch("side_dog.notify.dispatch_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_not_called()

    def test_a_running_test_event_does_not_notify(self) -> None:
        event = {"kind": "test", "status": "running", "title": "Running tests"}
        with patch("side_dog.notify.dispatch_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_not_called()

    def test_an_unrelated_event_does_not_notify(self) -> None:
        event = {"kind": "file", "status": "success", "title": "File changed"}
        with patch("side_dog.notify.dispatch_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_not_called()

    def test_a_missing_detail_falls_back_to_a_plain_sentence(self) -> None:
        event = {"kind": "test", "status": "failed", "title": "Tests failed"}
        with patch("side_dog.notify.dispatch_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_called_once_with(
            "Tests failed",
            "A test run failed.",
            subtitle="my-project",
            persistent=True,
        )


class SendDesktopNotificationTest(TestCase):
    def test_dispatch_swallows_a_worker_start_failure(self) -> None:
        with patch(
            "side_dog.notify._ensure_notification_worker", return_value=False
        ):
            dispatch_desktop_notification("Tests failed", "pytest")

    def test_dispatch_does_not_wait_for_a_slow_desktop_adapter(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow_sender(
            _title: str,
            _message: str,
            _subtitle: str = "",
            *,
            persistent: bool = False,
        ) -> None:
            started.set()
            release.wait(2)
            finished.set()

        with patch("side_dog.notify.send_desktop_notification", slow_sender):
            before = time.monotonic()
            dispatch_desktop_notification("Tests failed", "pytest", "my-project")
            elapsed = time.monotonic() - before
            self.assertTrue(started.wait(1))
            self.assertLess(elapsed, 0.25)
            release.set()
            self.assertTrue(finished.wait(1))

    def test_persistent_dialogs_use_a_worker_separate_from_ordinary_alerts(
        self,
    ) -> None:
        with (
            patch(
                "side_dog.notify._ensure_persistent_notification_worker",
                return_value=True,
            ) as persistent_worker,
            patch(
                "side_dog.notify._ensure_notification_worker", return_value=True
            ) as ordinary_worker,
            patch(
                "side_dog.notify._PERSISTENT_NOTIFICATION_QUEUE.put_nowait"
            ) as persistent_queue,
            patch(
                "side_dog.notify._NOTIFICATION_QUEUE.put_nowait"
            ) as ordinary_queue,
        ):
            dispatch_desktop_notification(
                "Possible coding-agent conflict", "same folder", persistent=True
            )
            dispatch_desktop_notification("Tests failed", "unittest")

        persistent_worker.assert_called_once_with()
        ordinary_worker.assert_called_once_with()
        persistent_queue.assert_called_once_with(
            ("Possible coding-agent conflict", "same folder", "", True)
        )
        ordinary_queue.assert_called_once_with(
            ("Tests failed", "unittest", "", False)
        )

    def test_macos_shells_out_to_osascript(self) -> None:
        with (
            patch("side_dog.notify.sys.platform", "darwin"),
            patch("side_dog.notify.subprocess.run") as run,
        ):
            send_desktop_notification("Tests failed", "pytest", subtitle="my-project")
        self.assertTrue(run.called)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "osascript")
        script = command[2]
        self.assertIn("pytest", script)
        self.assertIn("Tests failed", script)
        self.assertIn("my-project", script)

    def test_a_quote_in_the_message_cannot_break_out_of_the_applescript_string(
        self,
    ) -> None:
        with (
            patch("side_dog.notify.sys.platform", "darwin"),
            patch("side_dog.notify.subprocess.run") as run,
        ):
            send_desktop_notification("Tests failed", 'say "hi" then quit')
        script = run.call_args.args[0][2]
        self.assertIn('\\"hi\\"', script)

    def test_a_persistent_macos_warning_stays_until_dismissed_or_thirty_seconds(
        self,
    ) -> None:
        with (
            patch("side_dog.notify.sys.platform", "darwin"),
            patch("side_dog.notify.subprocess.run") as run,
        ):
            send_desktop_notification(
                "Possible coding-agent conflict",
                "Two coding agents are working in the same folder.",
                subtitle="Side Dog board",
                persistent=True,
            )
        script = run.call_args.args[0][2]
        self.assertIn("display dialog", script)
        self.assertIn("Side Dog board", script)
        self.assertIn('buttons {"Dismiss"}', script)
        self.assertIn(
            f"giving up after {PERSISTENT_NOTIFICATION_SECONDS}", script
        )
        self.assertEqual(
            run.call_args.kwargs["timeout"], PERSISTENT_NOTIFICATION_SECONDS + 5
        )

    def test_linux_shells_out_to_notify_send_when_present(self) -> None:
        with (
            patch("side_dog.notify.sys.platform", "linux"),
            patch("side_dog.notify.shutil.which", return_value="/usr/bin/notify-send"),
            patch("side_dog.notify.subprocess.run") as run,
        ):
            send_desktop_notification("Tests failed", "pytest", subtitle="my-project")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "notify-send")

    def test_linux_without_notify_send_does_nothing(self) -> None:
        with (
            patch("side_dog.notify.sys.platform", "linux"),
            patch("side_dog.notify.shutil.which", return_value=None),
            patch("side_dog.notify.subprocess.run") as run,
        ):
            send_desktop_notification("Tests failed", "pytest")
        run.assert_not_called()

    def test_a_failure_to_notify_is_swallowed_rather_than_raised(self) -> None:
        with (
            patch("side_dog.notify.sys.platform", "darwin"),
            patch(
                "side_dog.notify.subprocess.run",
                side_effect=subprocess.SubprocessError("no notifier"),
            ),
        ):
            send_desktop_notification("Tests failed", "pytest")


class BoardNotificationTest(TestCase):
    """Board messages take the same door as test failures."""

    def test_a_board_message_is_queued_with_the_board_subtitle(self) -> None:
        with patch("side_dog.notify.dispatch_desktop_notification") as sent:
            notify_for_board("PR #151 checks passed", "Claude Code · kitty · side-dog fix/x is idle")
        sent.assert_called_once_with(
            "PR #151 checks passed",
            "Claude Code · kitty · side-dog fix/x is idle",
            subtitle=BOARD_SUBTITLE,
        )

    def test_macos_board_messages_shell_out_to_osascript_like_test_failures(self) -> None:
        with (
            patch("side_dog.notify.sys.platform", "darwin"),
            patch("side_dog.notify.subprocess.run") as run,
            patch("side_dog.notify._ensure_notification_worker", return_value=True),
            patch(
                "side_dog.notify._NOTIFICATION_QUEUE.put_nowait",
                side_effect=lambda item: send_desktop_notification(
                    *item[:3], persistent=item[3]
                ),
            ),
        ):
            notify_for_board("Codex is blocked", "Codex · Codex Desktop · side-dog fix/y")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "osascript")
        self.assertIn("Codex is blocked", command[2])
        self.assertIn("Codex Desktop", command[2])
        self.assertIn(BOARD_SUBTITLE, command[2])

    def test_linux_board_messages_shell_out_to_notify_send_like_test_failures(self) -> None:
        with (
            patch("side_dog.notify.sys.platform", "linux"),
            patch("side_dog.notify.shutil.which", return_value="/usr/bin/notify-send"),
            patch("side_dog.notify.subprocess.run") as run,
            patch(
                "side_dog.notify._ensure_persistent_notification_worker",
                return_value=True,
            ),
            patch(
                "side_dog.notify._PERSISTENT_NOTIFICATION_QUEUE.put_nowait",
                side_effect=lambda item: send_desktop_notification(
                    *item[:3], persistent=item[3]
                ),
            ),
        ):
            notify_for_board(
                "Possible coding-agent conflict",
                "Possible coding-agent conflict — same folder "
                "(side-dog): kitty and VS Code",
                persistent=True,
            )
        command = run.call_args.args[0]
        self.assertEqual(command[0], "notify-send")
        self.assertIn("--urgency=critical", command)
        self.assertIn(
            f"--expire-time={PERSISTENT_NOTIFICATION_SECONDS * 1000}", command
        )
        self.assertEqual(command[-2], "Possible coding-agent conflict")
        self.assertIn("kitty and VS Code", command[-1])
        self.assertIn(BOARD_SUBTITLE, command[-1])
