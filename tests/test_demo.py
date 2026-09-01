from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from side_dog.cli import STATE_ENV, build_parser, demo_tour


class DemoTourTests(unittest.TestCase):
    def test_parser_supports_browser_and_terminal_tours(self) -> None:
        parser = build_parser()

        self.assertEqual(parser.parse_args(["demo"]).view, "panel")
        self.assertEqual(parser.parse_args(["demo", "--panel"]).view, "panel")
        self.assertEqual(parser.parse_args(["demo", "--watch"]).view, "watch")

    def test_panel_tour_is_isolated_and_cleans_up_without_opening_browser(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        emitted: list[tuple[Path, dict[str, object]]] = []
        output = io.StringIO()
        with (
            patch("side_dog.cli.subprocess.Popen", return_value=process) as start,
            patch(
                "side_dog.cli.append_event",
                side_effect=lambda root, event: emitted.append((root, event)),
            ),
            redirect_stdout(output),
        ):
            code = demo_tour("panel", duration=0, open_window=False)

        self.assertEqual(code, 0)
        command = start.call_args.args[0]
        environment = start.call_args.kwargs["env"]
        self.assertIn("panel", command)
        self.assertIn("--no-open", command)
        self.assertEqual(len({root for root, _ in emitted}), 2)
        self.assertTrue(all(environment[STATE_ENV] not in str(Path.cwd()) for _ in [0]))
        temporary_root = emitted[0][0].parent
        self.assertFalse(temporary_root.exists())
        self.assertIsNone(os.environ.get(STATE_ENV))
        statuses = {str(event.get("status")) for _, event in emitted}
        self.assertTrue({"running", "success", "failed"}.issubset(statuses))
        self.assertIn("all displayed activity is synthetic", output.getvalue())
        self.assertIn("temporary activity was removed", output.getvalue())
        process.terminate.assert_called_once()

    def test_terminal_tour_uses_the_isolated_folders(self) -> None:
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            patch("side_dog.cli.subprocess.Popen", return_value=process) as start,
            patch("side_dog.cli.append_event"),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(demo_tour("watch", duration=0), 0)

        command = start.call_args.args[0]
        self.assertIn("watch", command)
        self.assertIn("--no-follow-worktrees", command)
        self.assertIn("--github-poll", command)

    def test_viewer_start_failure_still_restores_state_and_cleans_up(self) -> None:
        captured: list[list[str]] = []
        stderr = io.StringIO()
        with (
            patch(
                "side_dog.cli.subprocess.Popen",
                side_effect=lambda command, **_kwargs: (
                    captured.append(command),
                    (_ for _ in ()).throw(OSError("not available")),
                )[1],
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            code = demo_tour("panel", duration=0, open_window=False)

        self.assertEqual(code, 2)
        self.assertFalse(Path(captured[0][2]).exists())
        self.assertIsNone(os.environ.get(STATE_ENV))
        self.assertIn("could not start demo panel", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
