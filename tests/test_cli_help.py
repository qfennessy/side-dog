from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog import __version__
from side_dog.cli import ANSI, COMMANDS, STATE_ENV, build_parser, main, watch


class CliHelpTest(TestCase):
    @staticmethod
    def invoke(*arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                result = main(list(arguments))
            except SystemExit as error:
                result = int(error.code or 0)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_help_alias_matches_top_level_help(self) -> None:
        alias = self.invoke("help")
        standard = self.invoke("--help")

        self.assertEqual(alias, standard)
        self.assertEqual(alias[0], 0)
        for command in COMMANDS:
            self.assertIn(command, alias[1])

        command_action = next(
            action
            for action in build_parser()._actions
            if isinstance(action.choices, dict)
        )
        self.assertEqual(tuple(command_action.choices), COMMANDS)

    def test_version_reports_the_installed_package_version(self) -> None:
        code, stdout, stderr = self.invoke("--version")

        self.assertEqual(code, 0)
        self.assertEqual(stdout, f"side-dog {__version__}\n")
        self.assertEqual(stderr, "")

    def test_help_alias_accepts_a_command(self) -> None:
        code, stdout, stderr = self.invoke("help", "watch")

        self.assertEqual(code, 0)
        self.assertIn("usage: side-dog watch", stdout)
        self.assertIn("--github-poll", stdout)
        self.assertEqual(stderr, "")

    def test_standard_help_remains_available_for_every_command(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                code, stdout, stderr = self.invoke(command, "--help")
                self.assertEqual(code, 0)
                self.assertIn(f"usage: side-dog {command}", stdout)
                self.assertEqual(stderr, "")

    def test_watch_help_distinguishes_bare_and_explicit_current_folder(self) -> None:
        code, stdout, stderr = self.invoke("watch", "--help")

        self.assertEqual(code, 0)
        self.assertIn("Bare `side-dog watch` discovers active agent", stdout)
        self.assertIn("`side-dog watch .` explicitly watches", stdout)
        self.assertEqual(stderr, "")

    def test_help_alias_rejects_an_unknown_command_concisely(self) -> None:
        code, stdout, stderr = self.invoke("help", "unknown")

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("side-dog: unknown command 'unknown'", stderr)
        self.assertIn(f"Available commands: {', '.join(COMMANDS)}", stderr)
        self.assertIn("side-dog help <command>", stderr)
        self.assertNotIn("invalid choice", stderr)

    def test_missing_and_invalid_commands_are_actionable(self) -> None:
        for arguments, message in (((), "a command is required"), (("wat",), "unknown command 'wat'")):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertIn(message, stderr)
                self.assertIn("Available commands:", stderr)
                self.assertIn("side-dog help", stderr)

    def test_help_import_does_not_load_terminal_width_tables(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import side_dog.cli; print('wcwidth' in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(completed.stdout.strip(), "False")

    @patch("side_dog.cli.watch", return_value=0)
    @patch("side_dog.cli.terminal_cell_width", return_value=0)
    def test_watch_checks_width_support_before_entering_session(
        self, width_support: object, watch: object
    ) -> None:
        self.assertEqual(main(["watch", "--no-color"]), 0)
        width_support.assert_called_once_with("")  # type: ignore[attr-defined]
        watch.assert_called_once()  # type: ignore[attr-defined]


class TtyStream(io.StringIO):
    """A stdout stand-in that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class InteractiveTtyStream(TtyStream):
    def fileno(self) -> int:
        return 7


class WatchOnceTest(TestCase):
    def render_once(self, **overrides: object) -> str:
        stream = TtyStream()
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            arguments = {
                "width": 80,
                "poll": 0.0,
                "no_color": False,
                "github_poll": 0.0,
                "once": True,
            }
            arguments.update(overrides)
            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(root / "state")}),
                patch("side_dog.cli.load_herdr_identities", return_value={}),
                patch("side_dog.cli.sys.stdout", stream),
                patch("side_dog.cli.sys.stdin", stream),
            ):
                self.assertEqual(watch(os.fspath(root), **arguments), 0)
        return stream.getvalue()

    def test_once_prints_one_frame_and_leaves_the_terminal_alone(self) -> None:
        output = self.render_once()

        self.assertIn("SIDE DOG", output)
        self.assertEqual(output.count("SIDE DOG"), 1)
        self.assertNotIn("\x1b[?1049h", output)
        self.assertNotIn("\x1b[?1049l", output)

    def test_once_keeps_color_on_a_terminal_and_honors_no_color(self) -> None:
        self.assertIn(ANSI["bold"], self.render_once())
        self.assertNotIn(ANSI["bold"], self.render_once(no_color=True))

    def test_plain_watch_keeps_automatic_live_worktree_detection(self) -> None:
        with patch("side_dog.cli.busy_worktrees", return_value=[]) as busy:
            self.render_once(follow_worktrees=True)

        self.assertTrue(busy.called)
        self.assertIsNone(busy.call_args_list[0].kwargs["live"])

    def test_watch_accepts_once_from_the_command_line(self) -> None:
        parsed = build_parser().parse_args(["watch", ".", "--once"])

        self.assertTrue(parsed.once)
        self.assertFalse(build_parser().parse_args(["watch", "."]).once)

    def test_no_color_terminal_still_confirms_before_quitting(self) -> None:
        output = InteractiveTtyStream()
        terminal_input = InteractiveTtyStream()
        ready = ([7], [], [])
        idle = ([], [], [])
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(root / "state")}),
                patch("side_dog.cli.sys.stdout", output),
                patch("side_dog.cli.sys.stdin", terminal_input),
                patch("side_dog.cli.signal.signal"),
                patch("side_dog.cli.termios.tcgetattr", return_value=[]),
                patch("side_dog.cli.termios.tcsetattr"),
                patch("side_dog.cli.tty.setcbreak"),
                patch("side_dog.cli.snapshot", return_value=set()),
                patch("side_dog.cli.load_git_state", return_value=None),
                patch("side_dog.cli.poll_watch_root", return_value=0),
                patch("side_dog.cli.os.read", side_effect=[b"q", b"y"]),
                patch(
                    "side_dog.cli.select.select",
                    side_effect=[ready, ready, idle],
                ),
                patch("side_dog.cli.create_poll_coordinator"),
                patch("side_dog.cli.UsageMonitor") as usage_monitor,
            ):
                usage_monitor.return_value.report = None
                self.assertEqual(
                    watch(
                        os.fspath(root),
                        width=80,
                        poll=0.0,
                        no_color=True,
                        github_poll=0.0,
                        follow_worktrees=False,
                        no_notify=True,
                    ),
                    0,
                )

        rendered = output.getvalue()
        self.assertIn("Are you sure you want to quit?", rendered)
        self.assertIn("> No <", rendered)
        self.assertNotIn(ANSI["blue"], rendered)

    def test_ctrl_c_twice_opens_the_dialog_then_quits(self) -> None:
        output = InteractiveTtyStream()
        terminal_input = InteractiveTtyStream()
        handlers: dict[int, object] = {}

        def remember_handler(signal_number: int, handler: object) -> None:
            handlers[signal_number] = handler

        def interrupt_then_idle(*_arguments: object) -> tuple[list[int], list, list]:
            handler = handlers[signal.SIGINT]
            assert callable(handler)
            handler(signal.SIGINT, None)
            return ([], [], [])

        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(root / "state")}),
                patch("side_dog.cli.sys.stdout", output),
                patch("side_dog.cli.sys.stdin", terminal_input),
                patch("side_dog.cli.signal.signal", side_effect=remember_handler),
                patch("side_dog.cli.termios.tcgetattr", return_value=[]),
                patch("side_dog.cli.termios.tcsetattr"),
                patch("side_dog.cli.tty.setcbreak"),
                patch("side_dog.cli.snapshot", return_value=set()),
                patch("side_dog.cli.load_git_state", return_value=None),
                patch("side_dog.cli.poll_watch_root", return_value=0),
                patch(
                    "side_dog.cli.select.select",
                    side_effect=interrupt_then_idle,
                ),
                patch("side_dog.cli.create_poll_coordinator"),
                patch("side_dog.cli.UsageMonitor") as usage_monitor,
            ):
                usage_monitor.return_value.report = None
                self.assertEqual(
                    watch(
                        os.fspath(root),
                        width=80,
                        poll=0.0,
                        no_color=True,
                        github_poll=0.0,
                        follow_worktrees=False,
                        no_notify=True,
                    ),
                    0,
                )

        self.assertIn("Are you sure you want to quit?", output.getvalue())
        self.assertIn(signal.SIGTERM, handlers)

    def test_zero_argument_views_follow_the_inherited_herdr_session(self) -> None:
        with (
            patch.dict(os.environ, {"HERDR_ENV": "1"}, clear=True),
            patch("side_dog.cli.terminal_cell_width"),
            patch("side_dog.cli.watch", return_value=0) as watch_view,
            patch("side_dog.panel.panel", return_value=0) as panel_view,
        ):
            self.assertEqual(main(["watch", "--once"]), 0)
            self.assertEqual(main(["panel", "--no-open"]), 0)

        self.assertEqual(watch_view.call_args.args[0], [])
        self.assertTrue(watch_view.call_args.kwargs["follow_herdr"])
        self.assertFalse(watch_view.call_args.kwargs["require_herdr"])
        self.assertEqual(panel_view.call_args.args[0], [])
        self.assertTrue(panel_view.call_args.kwargs["follow_herdr"])
        self.assertFalse(panel_view.call_args.kwargs["require_herdr"])

    def test_an_explicit_folder_keeps_the_existing_behavior_inside_herdr(self) -> None:
        with (
            patch.dict(os.environ, {"HERDR_ENV": "1"}, clear=True),
            patch("side_dog.cli.terminal_cell_width"),
            patch("side_dog.cli.watch", return_value=0) as watch_view,
        ):
            self.assertEqual(main(["watch", ".", "--once"]), 0)

        self.assertFalse(watch_view.call_args.kwargs["follow_herdr"])
