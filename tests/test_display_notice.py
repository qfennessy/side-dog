from collections import deque
from pathlib import Path
from unittest import TestCase

from side_dog.cli import (
    ANSI,
    DisplayNotice,
    WatchRootState,
    event_filter_notice,
    expanded_history_notice,
    ordering_notice,
    pause_notice,
    render,
    render_display_notice,
    render_root_columns,
    root_focus_notice,
    terminal_cell_width,
)
from side_dog.usage import LiveUsageSnapshot, UsageBlock, UsageReport


def root_state(path: str, branch: str) -> WatchRootState:
    root = Path(path)
    return WatchRootState(
        root=root,
        path=root / "events.jsonl",
        records=deque(maxlen=500),
        position=0,
        known_files={},
        git_status={
            "branch": branch,
            "oid": "1234567890abcdef",
            "short_oid": "1234567",
            "repository": root.name,
        },
        last_hook_writes={},
        identities={},
        github_status=None,
        last_github_fingerprint=None,
        last_scan=0.0,
        last_git_refresh=0.0,
        last_herdr_refresh=0.0,
        last_github_refresh=0.0,
    )


class DisplayNoticeStateTest(TestCase):
    def test_latest_message_replaces_prior_message_and_restarts_timeout(self) -> None:
        notice = DisplayNotice()

        notice.show("Expanded history", now=10.0)
        self.assertEqual(notice.current(11.9), "Expanded history")

        notice.show("Files only", now=11.9)
        self.assertEqual(notice.current(12.1), "Files only")
        self.assertEqual(notice.current(13.89), "Files only")
        self.assertIsNone(notice.current(13.9))

    def test_explanations_describe_each_resulting_view(self) -> None:
        self.assertIn("Expanded", expanded_history_notice(True))
        self.assertIn("Compact", expanded_history_notice(False))
        self.assertIn("Milestones only", event_filter_notice("milestones"))
        self.assertIn("File writes only", event_filter_notice("files"))
        self.assertIn("Everything", event_filter_notice("all"))
        self.assertIn("Newest first", ordering_notice(True))
        self.assertIn("Oldest first", ordering_notice(False))
        self.assertIn("Paused", pause_notice(True))
        self.assertIn("collection continues", pause_notice(True))
        self.assertIn("Live", pause_notice(False))

    def test_root_explanations_cover_focus_and_each_all_roots_layout(self) -> None:
        labels = ["main", "PR #21"]

        self.assertEqual(
            root_focus_notice(1, labels, "auto"),
            "Showing only PR #21.",
        )
        self.assertIn("wide panes use columns", root_focus_notice(None, labels, "auto"))
        self.assertIn("one column each", root_focus_notice(None, labels, "columns"))
        self.assertIn("one shared list", root_focus_notice(None, labels, "timeline"))


class DisplayNoticeRenderTest(TestCase):
    def test_settling_frame_hides_provisional_dashboard_sections(self) -> None:
        usage = LiveUsageSnapshot(
            UsageReport("daily"),
            UsageReport("monthly"),
            UsageBlock(status="available", cost_microusd=59_860_000),
        )
        screen = render(
            [],
            Path("/tmp/side-dog"),
            width=120,
            height=30,
            color=False,
            identities={
                "codex:session": {
                    "agent": "codex",
                    "session_id": "session",
                    "status": "working",
                    "model": "gpt-5.6-sol",
                    "effort": "high",
                    "working_root": "/tmp/side-dog",
                }
            },
            root_count=2,
            available_root_count=17,
            expanded_header=True,
            discovery_pending=True,
            display_notice="Following 4 Herdr agent folders.",
            usage_report=usage,
            roster_roots=(
                {
                    "key": "/tmp/side-dog",
                    "name": "side-dog",
                    "label": "main",
                    "identity_refresh_status": "pending",
                    "github_refresh_status": "pending",
                },
            ),
        )

        self.assertEqual(
            screen.count("Starting Side Dog · finding folders and agents…"),
            1,
        )
        for provisional_text in (
            "settling",
            "gpt-5.6-sol",
            "working",
            "identity pending",
            "GitHub context pending",
            "View changed",
            "Following 4 Herdr",
            "API est",
            "$59.86",
        ):
            with self.subTest(provisional_text=provisional_text):
                self.assertNotIn(provisional_text, screen)

    def test_column_layout_uses_the_same_quiet_settling_frame(self) -> None:
        first = root_state("/tmp/main", "main")
        second = root_state("/tmp/review", "review")
        first.identities = {
            "codex:session": {
                "agent": "codex",
                "session_id": "session",
                "status": "working",
                "model": "gpt-5.6-sol",
                "working_root": "/tmp/main",
            }
        }

        screen = render_root_columns(
            [first, second],
            ["main", "review"],
            None,
            width=120,
            height=20,
            color=False,
            session_filter=None,
            expanded_history=False,
            event_filter="all",
            paused=False,
            new_event_counts=None,
            newest_first=True,
            discovery_pending=True,
            display_notice="Following another folder.",
        )

        self.assertEqual(
            screen.count("Starting Side Dog · finding folders and agents…"),
            1,
        )
        self.assertNotIn("gpt-5.6-sol", screen)
        self.assertNotIn("View changed", screen)

    def test_notice_is_width_safe_and_readable_without_color(self) -> None:
        lines = render_display_notice(
            "Showing only an-extremely-long-root-name.",
            width=28,
            color=False,
        )

        self.assertEqual(len(lines), 3)
        self.assertIn("View changed", lines[0])
        self.assertIn("Showing only", lines[1])
        self.assertNotIn("\x1b[", "\n".join(lines))
        self.assertTrue(all(terminal_cell_width(line) <= 28 for line in lines))

    def test_colored_notice_does_not_reuse_semantic_status_colors(self) -> None:
        notice = "\n".join(
            render_display_notice("Files only — milestones are hidden.", 50, True)
        )

        self.assertNotIn(ANSI["blue"], notice)
        self.assertNotIn(ANSI["green"], notice)
        self.assertNotIn(ANSI["yellow"], notice)
        self.assertNotIn(ANSI["red"], notice)

    def test_consolidated_notice_appears_above_timeline_only_while_supplied(
        self,
    ) -> None:
        with_notice = render(
            [],
            Path("/tmp/example"),
            width=80,
            height=20,
            color=False,
            display_notice="Newest first — new events appear at the top.",
        )
        without_notice = render(
            [],
            Path("/tmp/example"),
            width=80,
            height=20,
            color=False,
        )

        self.assertIn("View changed", with_notice)
        self.assertIn("Newest first", with_notice)
        self.assertNotIn("View changed", without_notice)

    def test_column_notice_uses_one_full_width_window_without_blocking_columns(
        self,
    ) -> None:
        states = [root_state("/tmp/main", "main"), root_state("/tmp/review", "review")]

        screen = render_root_columns(
            states,
            ["main", "review"],
            None,
            width=100,
            height=20,
            color=False,
            session_filter=None,
            expanded_history=False,
            event_filter="all",
            paused=False,
            new_event_counts=None,
            newest_first=True,
            display_notice="All folders — one column each.",
        )

        self.assertEqual(screen.count("View changed"), 1)
        self.assertIn("All folders", screen)
        self.assertIn("main", screen)
        self.assertIn("review", screen)
        self.assertLessEqual(len(screen.splitlines()), 20)
