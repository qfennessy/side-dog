"""The expanded header stays small so the timeline keeps the pane."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    ANSI_ESCAPE,
    HEADER_SHARE,
    SOURCE_COLOR_INDEX,
    SOURCE_KEY,
    SOURCE_LABEL,
    expanded_watch_location_lines,
    render,
    render_external_refresh_details,
    render_usage_banner,
    terminal_cell_width,
)
from side_dog.usage import LiveUsageSnapshot, UsageBlock, UsageReport, UsageSample

HOME = os.path.expanduser("~")
PATHS = [
    f"{HOME}/src/pr-agent",
    f"{HOME}/src/cocos-story",
    f"{HOME}/src/side-dog",
    f"{HOME}/src/tony-the-tiger",
    f"{HOME}/src/pr-agent-shadow-recorder-55",
    f"{HOME}/.codex/worktrees/59c8/cocos-story",
    f"{HOME}/src/side-dog/.claude/worktrees/upbeat-napier-95ec36",
    f"{HOME}/.codex/worktrees/427d/cocos-story",
]


class FolderLineTest(TestCase):
    def test_siblings_share_one_line_named_by_their_parent(self) -> None:
        lines = expanded_watch_location_lines(PATHS, 120)

        self.assertEqual(
            lines[0],
            " Folders ~/src: pr-agent, cocos-story, side-dog, tony-the-tiger, "
            "pr-agent-shadow-recorder-55",
        )
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(all(terminal_cell_width(line) <= 120 for line in lines))
        self.assertIn("~/.codex/worktrees/59c8/cocos-story", "\n".join(lines))

    def test_one_line_budget_keeps_the_names_and_counts_the_rest(self) -> None:
        line = expanded_watch_location_lines(PATHS, 96, max_lines=1)

        self.assertEqual(len(line), 1)
        self.assertLessEqual(terminal_cell_width(line[0]), 96)
        # The suffix needs room, so one more name folds rather than being
        # cropped into something unreadable; the count says so.
        self.assertEqual(
            line[0],
            " Folders ~/src: pr-agent, cocos-story, side-dog, tony-the-tiger · +4 folded",
        )

    def test_home_and_root_stay_whole(self) -> None:
        home = os.path.expanduser("~")

        self.assertEqual(expanded_watch_location_lines([home], 80), [" Folder  ~"])
        self.assertEqual(
            expanded_watch_location_lines([home, f"{home}/src/a", "/"], 80),
            [" Folders ~ · ~/src/a · /"],
        )

    def test_a_group_too_wide_for_the_pane_falls_back_to_one_folder_per_line(
        self,
    ) -> None:
        lines = expanded_watch_location_lines(PATHS[:2], 24)

        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("/pr-agent"), lines[0])
        self.assertTrue(lines[1].endswith("/cocos-story"), lines[1])


class RefreshWarningTest(TestCase):
    def test_folders_with_the_same_message_share_one_line(self) -> None:
        roots = [
            {
                "key": f"/tmp/wt-{index}",
                "name": f"wt-{index}",
                "identity_refresh_status": "ready",
                "github_refresh_status": "unavailable",
                "has_identities": True,
            }
            for index in range(3)
        ]

        lines = render_external_refresh_details(roots, 100, False)

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("│ ? 3 folders: "), lines[0])
        self.assertIn("GitHub context unknown", lines[0])


def usage_fixture(count: int) -> tuple[LiveUsageSnapshot, tuple, tuple]:
    samples = tuple(
        UsageSample(
            agent="claude-code",
            period=f"session-{index}",
            session_id=f"session-{index}",
            model="claude-sonnet-4",
            input_tokens=1_000,
            output_tokens=500,
            cost_microusd=1_250_000,
            cost_basis="estimated",
            last_activity=f"2033-05-18T03:{index:02d}:00+00:00",
        )
        for index in range(count)
    )
    report = LiveUsageSnapshot(
        UsageReport("session", samples=samples),
        UsageReport("session", samples=samples),
        UsageBlock(
            status="available",
            cost_microusd=2_550_000,
            burn_rate_microusd_per_hour=102_000_000,
            remaining_minutes=239,
        ),
    )
    sessions = tuple(("claude-code", f"session-{index}") for index in range(count))
    contexts = tuple(
        {
            "agent": "claude-code",
            "session_id": f"session-{index}",
            "label": f"Session {index}",
            "status": "working",
        }
        for index in range(count)
    )
    return report, sessions, contexts


class UsageSessionsTest(TestCase):
    def test_session_rows_stay_folded_behind_u(self) -> None:
        report, sessions, contexts = usage_fixture(14)

        folded = render_usage_banner(
            report, (), {}, 160, False, sessions, expanded=True,
            contexts=contexts, list_sessions=False,
        )
        listed = render_usage_banner(
            report, (), {}, 160, False, sessions, expanded=True,
            contexts=contexts, list_sessions=True,
        )

        self.assertNotIn("Session 3", folded)
        self.assertNotIn("subscription bill", folded)
        self.assertIn("14 sessions · u lists them", folded)
        self.assertLessEqual(len(folded.splitlines()), 3)
        self.assertIn("Session 3", listed)
        self.assertIn("subscription bill", listed)


class HeaderShareTest(TestCase):
    def test_expanded_header_leaves_most_of_the_pane_to_the_timeline(self) -> None:
        now_ms = 2_000_000_000_000
        height = 40
        roots = [
            {"key": path, "name": Path(path).name, "label": Path(path).name, "color_index": index}
            for index, path in enumerate(PATHS)
        ]
        identities = {
            f"agent-{index}": {
                "agent": "claude-code",
                "pane_id": f"p{index}",
                "label": f"Task {index}",
                "working_root": path,
                "status": "working",
                SOURCE_KEY: path,
            }
            for index, path in enumerate(PATHS)
        }
        records = [
            {
                "kind": "commit",
                "status": "success",
                "title": "Commit",
                "detail": f"abc{index:02d} · change {index}",
                "agent": "claude-code",
                "timestamp": "2033-05-18T03:33:20+00:00",
                "epoch_ms": now_ms - index * 60_000,
                SOURCE_LABEL: "main",
                SOURCE_COLOR_INDEX: "0",
            }
            for index in range(60)
        ]
        report, sessions, contexts = usage_fixture(14)

        with patch("side_dog.cli.time.time", return_value=now_ms / 1000):
            screen = render(
                records,
                Path(PATHS[0]),
                width=120,
                height=height,
                color=False,
                identities=identities,
                root_count=8,
                roster_roots=roots,
                expanded_header=True,
                show_idle_agents=True,
                usage_report=report,
                usage_sessions=sessions,
                usage_contexts=contexts,
            )

        lines = [ANSI_ESCAPE.sub("", line) for line in screen.splitlines()]
        divider = next(index for index, line in enumerate(lines) if "Today" in line)
        self.assertLessEqual(len(lines), height)
        self.assertLessEqual(divider, int(height * HEADER_SHARE) + 1, "\n".join(lines))
        events = [line for line in lines if "· change " in line]
        self.assertGreaterEqual(len(events), height // 2 - 2, "\n".join(lines))
        self.assertIn("Folders ~/src: pr-agent", screen)
        self.assertNotIn("claude-code · Session", screen)


class ColumnHeaderShareTest(TestCase):
    def test_column_rosters_share_the_header_budget(self) -> None:
        from side_dog.cli import render_root_columns, watch_root_labels  # noqa: PLC0415
        from tests.test_multi_root import root_state  # noqa: PLC0415

        now_ms = 2_000_000_000_000
        height = 30
        states = [
            root_state(Path("/tmp/one"), [], branch="main"),
            root_state(Path("/tmp/two"), [], branch="review"),
        ]
        for state in states:
            state.identities = {
                f"agent-{index}": {
                    "agent": "claude-code",
                    "pane_id": f"{state.root.name}-{index}",
                    "label": f"Task {index}",
                    "working_root": os.fspath(state.root),
                    "status": "idle" if index else "working",
                }
                for index in range(16)
            }
            state.records.extend(
                {
                    "kind": "commit",
                    "status": "success",
                    "title": "Commit",
                    "detail": f"abc{index:02d} · change {index}",
                    "agent": "claude-code",
                    "timestamp": "2033-05-18T03:33:20+00:00",
                    "epoch_ms": now_ms - index * 60_000,
                    SOURCE_KEY: os.fspath(state.root),
                }
                for index in range(40)
            )

        with patch("side_dog.cli.time.time", return_value=now_ms / 1000):
            screen = render_root_columns(
                states,
                watch_root_labels(states),
                None,
                width=120,
                height=height,
                color=False,
                session_filter=None,
                expanded_history=False,
                event_filter="all",
                paused=False,
                new_event_counts=None,
                newest_first=True,
                expanded_header=True,
                show_idle_agents=True,
            )

        lines = screen.splitlines()
        divider = next(index for index, line in enumerate(lines) if "Today" in line)
        self.assertLessEqual(len(lines), height)
        self.assertLessEqual(divider, int(height * HEADER_SHARE) + 1, screen)
        self.assertIn("header rows folded", screen)
        events = [line for line in lines if "· change " in line]
        self.assertGreaterEqual(len(events), height // 2 - 3, screen)
