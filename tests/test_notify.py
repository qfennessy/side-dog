import subprocess
from unittest import TestCase
from unittest.mock import patch

from side_dog.notify import notify_for_event, send_desktop_notification


class TestFailureRuleTest(TestCase):
    def test_a_failed_test_event_notifies_with_its_own_title_and_detail(self) -> None:
        event = {
            "kind": "test",
            "status": "failed",
            "title": "Tests failed",
            "detail": "pytest",
        }
        with patch("side_dog.notify.send_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_called_once_with("Tests failed", "pytest", subtitle="my-project")

    def test_a_passing_test_event_does_not_notify(self) -> None:
        event = {"kind": "test", "status": "success", "title": "Tests passed"}
        with patch("side_dog.notify.send_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_not_called()

    def test_a_running_test_event_does_not_notify(self) -> None:
        event = {"kind": "test", "status": "running", "title": "Running tests"}
        with patch("side_dog.notify.send_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_not_called()

    def test_an_unrelated_event_does_not_notify(self) -> None:
        event = {"kind": "file", "status": "success", "title": "File changed"}
        with patch("side_dog.notify.send_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_not_called()

    def test_a_missing_detail_falls_back_to_a_plain_sentence(self) -> None:
        event = {"kind": "test", "status": "failed", "title": "Tests failed"}
        with patch("side_dog.notify.send_desktop_notification") as sent:
            notify_for_event("my-project", event)
        sent.assert_called_once_with(
            "Tests failed", "A test run failed.", subtitle="my-project"
        )


class SendDesktopNotificationTest(TestCase):
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
