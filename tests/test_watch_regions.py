"""Watch-region hierarchy remains useful without crowding compact panes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

from side_dog.cli import ANSI_ESCAPE, render
from side_dog.usage import LiveUsageSnapshot, UsageBlock, UsageReport, UsageSample


NOW_MS = 2_000_000_000_000


def activity(detail: str = "src/app.py") -> dict[str, object]:
    return {
        "epoch_ms": NOW_MS,
        "timestamp": datetime.fromtimestamp(NOW_MS / 1000, timezone.utc).isoformat(),
        "kind": "commit",
        "title": "Commit",
        "detail": f"abc1234 · {detail}",
        "agent": "codex",
        "status": "success",
    }


def usage_snapshot() -> LiveUsageSnapshot:
    sample = UsageSample(
        agent="claude-code",
        session_id="session-1",
        model="claude-opus-5",
        input_tokens=1_000,
        output_tokens=500,
        cost_microusd=125_000,
        cost_basis="estimated",
    )
    report = UsageReport("daily", samples=(sample,), pricing_source="online")
    return LiveUsageSnapshot(report, report, UsageBlock(status="unavailable"))


class WatchRegionHierarchyTest(TestCase):
    def render_watch(self, *, height: int, color: bool) -> str:
        return render(
            [activity()],
            Path("/tmp/side-dog"),
            width=96,
            height=height,
            color=color,
            identities={
                "codex-session": {
                    "agent": "codex",
                    "session_id": "session-1",
                    "model": "gpt-6-astra",
                    "status": "working",
                    "working_root": "/tmp/side-dog",
                }
            },
            usage_report=usage_snapshot(),
        )

    def test_tall_watch_separates_agents_usage_and_activity(self) -> None:
        screen = self.render_watch(height=40, color=False)
        lines = screen.splitlines()

        agents = next(index for index, line in enumerate(lines) if "AGENTS" in line)
        usage = next(index for index, line in enumerate(lines) if "USAGE" in line)
        activity = next(index for index, line in enumerate(lines) if "ACTIVITY" in line)

        self.assertLess(agents, usage, screen)
        self.assertLess(usage, activity, screen)
        self.assertEqual(lines[usage - 1], "", screen)
        self.assertEqual(lines[activity - 1], "", screen)
        self.assertIn("abc1234", screen)
        self.assertIn("q quit", screen)

    def test_region_separators_are_dim_only_in_color_mode(self) -> None:
        screen = self.render_watch(height=40, color=True)
        plain = ANSI_ESCAPE.sub("", screen)

        self.assertIn("─ AGENTS", plain)
        self.assertIn("─ USAGE", plain)
        self.assertIn("─ ACTIVITY", plain)

    def test_compact_watch_folds_regions_before_activity_or_footer(self) -> None:
        screen = self.render_watch(height=24, color=False)

        self.assertNotIn("AGENTS", screen)
        self.assertNotIn("USAGE", screen)
        self.assertNotIn("ACTIVITY", screen)
        self.assertIn("abc1234", screen)
        self.assertIn("q quit", screen)
        self.assertLessEqual(len(screen.splitlines()), 24)

    def test_absent_roster_and_usage_do_not_leave_empty_regions(self) -> None:
        screen = render(
            [activity()],
            Path("/tmp/side-dog"),
            width=96,
            height=40,
            color=False,
        )

        self.assertNotIn("AGENTS", screen)
        self.assertNotIn("USAGE", screen)
        self.assertIn("ACTIVITY", screen)
        self.assertIn("abc1234", screen)

    def test_narrow_tall_watch_keeps_rules_within_the_pane(self) -> None:
        screen = render(
            [activity()],
            Path("/tmp/side-dog"),
            width=28,
            height=40,
            color=False,
            identities={
                "codex-session": {
                    "agent": "codex",
                    "session_id": "session-1",
                    "status": "working",
                    "working_root": "/tmp/side-dog",
                }
            },
            usage_report=usage_snapshot(),
        )

        self.assertIn("AGENTS", screen)
        self.assertIn("USAGE", screen)
        self.assertIn("ACTIVITY", screen)
        self.assertTrue(all(len(line) <= 28 for line in screen.splitlines()), screen)
