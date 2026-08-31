from __future__ import annotations

import io
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import COMMANDS, build_parser, main


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
