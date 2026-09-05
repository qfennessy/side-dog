from __future__ import annotations

import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    ANSI_ESCAPE,
    STARTUP_PROGRESS_DELAY_SECONDS,
    STATE_ENV,
    StartupCancelled,
    StartupExecutor,
    StartupProgress,
    StartupStage,
    run_startup_stage,
    startup_display_width,
    watch,
)


class StartupProgressTest(TestCase):
    def make_progress(
        self, *, width: int = 80, color: bool = False
    ) -> tuple[StartupProgress, io.StringIO, list[float]]:
        output = io.StringIO()
        clock = [0.0]
        progress = StartupProgress(
            enabled=True,
            width=width,
            color=color,
            clock=lambda: clock[0],
            writer=output.write,
            flusher=lambda: None,
        )
        return progress, output, clock

    def test_fast_start_only_has_the_immediate_line(self) -> None:
        progress, output, clock = self.make_progress()

        progress.begin()
        progress.advance(StartupStage.FINDING_PROJECTS)
        clock[0] = STARTUP_PROGRESS_DELAY_SECONDS - 0.01
        progress.complete(StartupStage.FINDING_PROJECTS)
        progress.ready()

        self.assertEqual(output.getvalue().count("\x1b[2K"), 1)
        self.assertIn("Starting Side Dog...", output.getvalue())
        self.assertNotIn("Finding projects", output.getvalue())
        self.assertNotIn("Ready in", output.getvalue())

    def test_slow_required_stage_shows_elapsed_time_and_ready_duration(self) -> None:
        progress, output, clock = self.make_progress()

        progress.begin()
        progress.advance(StartupStage.LOADING_ACTIVITY)
        clock[0] = 0.31
        progress.pump()
        clock[0] = 0.48
        progress.complete(StartupStage.LOADING_ACTIVITY)
        progress.ready()

        rendered = ANSI_ESCAPE.sub("", output.getvalue())
        self.assertIn("Loading recent activity... 0.3s", rendered)
        self.assertIn("Loading recent activity... 0.5s", rendered)
        self.assertIn("Ready in 0.5s", rendered)

    def test_optional_failure_is_explicit_but_continues_with_fallback(self) -> None:
        progress, output, _clock = self.make_progress()

        result = run_startup_stage(
            progress,
            None,
            StartupStage.REFRESHING_OPTIONAL,
            lambda: (_ for _ in ()).throw(RuntimeError("private detail")),
            optional=True,
            fallback="fallback",
        )

        self.assertEqual(result, "fallback")
        self.assertIn(
            "Refreshing optional context... unavailable; continuing",
            output.getvalue(),
        )
        self.assertNotIn("private detail", output.getvalue())

    def test_slow_optional_stage_is_named_while_it_runs(self) -> None:
        progress, output, _clock = self.make_progress()

        progress.begin()
        progress.advance(StartupStage.REFRESHING_OPTIONAL)
        progress.stage_started_at = 0.0
        progress.clock = lambda: 0.35
        progress.pump()

        self.assertIn("Refreshing optional context...", output.getvalue())
        self.assertIn("0.3s", output.getvalue())

    def test_quit_confirmation_survives_stage_completion(self) -> None:
        progress, output, clock = self.make_progress()

        progress.begin()
        progress.advance(StartupStage.FINDING_PROJECTS)
        progress.show_confirmation()
        clock[0] = STARTUP_PROGRESS_DELAY_SECONDS
        progress.complete(StartupStage.FINDING_PROJECTS)

        self.assertTrue(progress.confirmation_visible)
        self.assertIn(
            "Startup in progress; press Ctrl-C again to quit.",
            output.getvalue(),
        )
        self.assertNotIn("Finding projects... 0.3s", output.getvalue())

    def test_stage_runner_paints_a_slow_required_operation(self) -> None:
        output = io.StringIO()
        progress = StartupProgress(
            enabled=True,
            width=80,
            color=False,
            writer=output.write,
            flusher=lambda: None,
        )
        progress.begin()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = run_startup_stage(
                progress,
                executor,
                StartupStage.LOADING_ACTIVITY,
                lambda: (time.sleep(STARTUP_PROGRESS_DELAY_SECONDS * 2), "done")[1],
            )

        self.assertEqual(result, "done")
        self.assertIn("Loading recent activity...", output.getvalue())

    def test_stage_runner_stops_waiting_when_startup_is_cancelled(self) -> None:
        progress, _output, _clock = self.make_progress()
        started = threading.Event()
        release = threading.Event()

        def operation() -> str:
            started.set()
            release.wait(2.0)
            return "done"

        def cancel_after_start() -> None:
            if started.is_set():
                progress.request_cancel()

        progress.input_pump = cancel_after_start
        executor = StartupExecutor("test-startup")
        try:
            with self.assertRaises(StartupCancelled):
                run_startup_stage(
                    progress,
                    executor,
                    StartupStage.LOADING_ACTIVITY,
                    operation,
                )
        finally:
            release.set()
            executor.shutdown(wait=False, cancel_futures=True)

    def test_cancellation_requested_before_begin_is_preserved(self) -> None:
        progress, _output, _clock = self.make_progress()
        progress.request_cancel()
        progress.begin()

        with self.assertRaises(StartupCancelled):
            run_startup_stage(
                progress,
                None,
                StartupStage.FINDING_PROJECTS,
                lambda: "unreachable",
            )

    def test_default_startup_width_uses_the_detected_terminal_width(self) -> None:
        with patch(
            "side_dog.cli.shutil.get_terminal_size",
            return_value=os.terminal_size((23, 30)),
        ):
            self.assertEqual(startup_display_width(0), 23)

        self.assertEqual(startup_display_width(41), 41)

    def test_late_completion_cannot_move_progress_backwards(self) -> None:
        progress, output, clock = self.make_progress()

        progress.begin()
        progress.advance(StartupStage.FINDING_PROJECTS)
        progress.advance(StartupStage.FINDING_AGENTS)
        progress.complete(StartupStage.FINDING_PROJECTS)
        clock[0] = STARTUP_PROGRESS_DELAY_SECONDS
        progress.pump()

        self.assertEqual(progress.current_stage, StartupStage.FINDING_AGENTS)
        self.assertIn("Finding coding agents", output.getvalue())
        self.assertNotIn("Finding projects...", output.getvalue())

    def test_sequential_work_in_the_same_stage_can_report_if_it_is_slow(self) -> None:
        progress, output, clock = self.make_progress()

        progress.begin()
        progress.advance(StartupStage.FINDING_PROJECTS)
        progress.complete(StartupStage.FINDING_PROJECTS)
        progress.start(StartupStage.FINDING_PROJECTS)
        clock[0] = 0.31
        progress.pump()

        self.assertIn("Finding projects... 0.3s", output.getvalue())

    def test_narrow_no_color_output_is_one_line_and_plain(self) -> None:
        progress, output, clock = self.make_progress(width=20, color=False)

        progress.begin()
        progress.advance(StartupStage.FINDING_WORKTREES)
        clock[0] = STARTUP_PROGRESS_DELAY_SECONDS
        progress.pump()

        rendered = ANSI_ESCAPE.sub("", output.getvalue()).split("\x1b[2K")[-1]
        self.assertNotIn("\x1b[2m", output.getvalue())
        self.assertLessEqual(len(rendered), 20)
        self.assertIn("Finding", rendered)

    def test_noninteractive_once_mode_never_writes_progress(self) -> None:
        output = io.StringIO()
        progress = StartupProgress(
            enabled=False,
            width=80,
            color=False,
            writer=output.write,
            flusher=lambda: None,
        )

        progress.begin()
        progress.advance(StartupStage.FINDING_PROJECTS)
        progress.complete(StartupStage.FINDING_PROJECTS)
        progress.ready()

        self.assertEqual(output.getvalue(), "")

    def test_startup_error_is_handled_without_private_exception_text(self) -> None:
        class TerminalOutput(io.StringIO):
            def isatty(self) -> bool:
                return True

        output = TerminalOutput()
        error = io.StringIO()
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(root / "state")}),
                patch("side_dog.cli.sys.stdout", output),
                patch("side_dog.cli.sys.stdin", io.StringIO()),
                patch("side_dog.cli.sys.stderr", error),
                patch("side_dog.cli.signal.signal"),
                patch(
                    "side_dog.cli.prepare_watch_startup",
                    side_effect=RuntimeError("/private/session/path"),
                ),
                redirect_stderr(error),
            ):
                result = watch(
                    os.fspath(root),
                    width=40,
                    poll=0.0,
                    no_color=True,
                    github_poll=0.0,
                    no_notify=True,
                )

        self.assertEqual(result, 1)
        self.assertIn("Startup stopped", output.getvalue())
        self.assertIn("startup could not finish; try again", error.getvalue())
        self.assertNotIn("private/session/path", output.getvalue())
        self.assertNotIn("private/session/path", error.getvalue())
