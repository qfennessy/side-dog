"""The timeline tints text instead of filling blocks (issue #167)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest import TestCase

from side_dog.model import pipeline_stage
from side_dog.cli import (
    ANSI,
    ANSI_ESCAPE,
    ROOT_GUTTER,
    SOURCE_COLOR_INDEX,
    SOURCE_KEY,
    SOURCE_LABEL,
    display_github_detail,
    event_style,
    label_summary,
    render,
    render_github_banner,
    root_color,
    starts_with_label,
    style_source_label,
)

NOW_MS = 1_800_000_000_000
ROOT = Path("/tmp/side-dog")


def event(offset_ms: int, **fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "agent": "claude-code",
        "epoch_ms": NOW_MS - offset_ms,
        "timestamp": "2027-01-15T10:00:00+00:00",
        SOURCE_LABEL: "PR #162",
        SOURCE_COLOR_INDEX: "0",
    }
    base.update(fields)
    return base


def task_events() -> list[dict[str, object]]:
    shared = {"group_id": "g1", "turn_id": "t1"}
    return [
        event(
            40_000,
            kind="test",
            status="unknown",
            title="Tests finished",
            detail="unittest",
            operation_id="o1",
            **shared,
        ),
        event(
            30_000,
            kind="commit",
            status="success",
            title="Commit",
            detail="a428942",
            operation_id="o2",
            **shared,
        ),
    ]


def render_screen(
    records: list[dict[str, object]],
    *,
    color: bool,
    expanded_history: bool = True,
) -> str:
    return render(
        records,
        ROOT,
        96,
        24,
        color,
        root_count=2,
        expanded_history=expanded_history,
    )


class TintNotFillTest(TestCase):
    def test_root_color_is_a_foreground_tint(self) -> None:
        self.assertTrue(root_color(0).startswith("\x1b[38;5;"))
        self.assertNotIn("48;5;", root_color(3))

    def test_colored_screen_paints_no_background_anywhere(self) -> None:
        screen = render_screen(task_events(), color=True)

        self.assertNotIn("\x1b[48;5;", screen)
        self.assertIn(f"{root_color(0)}{ROOT_GUTTER}{ANSI['reset']} ", screen)

    def test_badge_is_tinted_text_without_ink_or_bold(self) -> None:
        line = style_source_label("[main] Commit", {SOURCE_LABEL: "main", SOURCE_COLOR_INDEX: 1}, True)

        self.assertEqual(line, f"{root_color(1)}[main]{ANSI['reset']} Commit")


class SayTheFolderOnceTest(TestCase):
    def test_title_that_starts_with_the_badge_drops_it(self) -> None:
        record = {SOURCE_LABEL: "PR #162", SOURCE_COLOR_INDEX: 0}

        self.assertEqual(label_summary(record, "PR #162 merged"), "PR #162 merged")
        self.assertEqual(label_summary(record, "Commit"), "[PR #162] Commit")

    def test_badge_survives_when_the_title_only_shares_a_prefix(self) -> None:
        # A folder labeled "PR #1" showing an older "PR #10 merged" milestone
        # still needs its badge: the two numbers are different pull requests.
        record = {SOURCE_LABEL: "PR #1", SOURCE_COLOR_INDEX: 0}

        self.assertEqual(label_summary(record, "PR #10 merged"), "[PR #1] PR #10 merged")
        self.assertTrue(starts_with_label("PR #1 merged", "PR #1"))
        self.assertTrue(starts_with_label("pr #1", "PR #1"))
        self.assertFalse(starts_with_label("PR #10 merged", "PR #1"))
        self.assertFalse(starts_with_label("PRs · 2 confirmed", "PR"))

    def test_task_card_children_never_repeat_the_badge(self) -> None:
        screen = render_screen(task_events(), color=False)
        lines = [line for line in screen.splitlines() if "Claude" in line]

        heading = next(line for line in lines if "Agent task" in line)
        children = [line for line in lines if "├─" in line or "└─" in line]
        self.assertIn("[PR #162]", heading)
        self.assertEqual(len(children), 2)
        for child in children:
            self.assertNotIn("[PR #162]", child)

    def test_github_milestone_does_not_repeat_the_pr_number(self) -> None:
        status = {"number": 162, "title": "Masthead", "state": "MERGED", "ci": "CI 6/6"}
        records = [
            event(
                10_000,
                kind="github",
                status="success",
                title="PR #162 merged",
                detail="x",
                github=status,
                github_state="MERGED",
                agent="github",
            )
        ]

        screen = render_screen(records, color=False)

        self.assertIn("⇉ PR #162 merged · Masthead · merged · CI 6/6", screen)
        self.assertNotIn("[PR #162] PR #162", screen)


class QuietGlyphsTest(TestCase):
    def test_unknown_status_shows_a_quiet_mark_not_a_question(self) -> None:
        self.assertEqual(event_style({"kind": "test", "status": "unknown"})[0], "·")

    def test_compact_stage_uses_the_same_quiet_mark(self) -> None:
        stage = pipeline_stage([{"kind": "test", "status": "unknown"}])

        self.assertEqual(stage, "Tests ·")
        self.assertEqual(pipeline_stage([{"kind": "push", "status": "odd"}]), "Push ·")
        screen = render_screen(task_events(), color=False, expanded_history=False)
        stage_line = next(line for line in screen.splitlines() if "└─ Tests" in line)
        self.assertIn("└─ Tests · → Commit a428942", stage_line)
        self.assertNotIn("?", stage_line)

    def test_task_card_with_unknown_state_omits_the_status_word(self) -> None:
        screen = render_screen(task_events(), color=False)
        heading = next(line for line in screen.splitlines() if "Agent task" in line)

        self.assertNotIn("unknown", heading)
        self.assertIn("· 2 events", heading)

    def test_milestone_line_bolds_only_its_title(self) -> None:
        records = [
            event(
                10_000,
                kind="commit",
                status="success",
                title="Commit",
                detail="a428942 · Color the dot",
            )
        ]

        screen = render_screen(records, color=True)
        line = next(line for line in screen.splitlines() if "Commit" in line)

        self.assertIn(f"{ANSI['bold']}Claude · Commit{ANSI['reset']}", line)
        self.assertNotIn(f"{ANSI['bold']}a428942", line)
        self.assertIn("a428942 · Color the dot", ANSI_ESCAPE.sub("", line))

    def test_pull_request_states_read_as_words(self) -> None:
        status = {
            "number": 12,
            "title": "Feature",
            "state": "OPEN",
            "draft": True,
            "ci": "CI 3/4",
            "review": "CHANGES_REQUESTED",
            "merge_state": "BLOCKED",
        }

        self.assertEqual(
            display_github_detail(status),
            "Feature · draft · open · CI 3/4 · changes requested · blocked",
        )
        self.assertIn("changes requested", render_github_banner(status, 100, False))
        # Titles are never rewritten, even when they are a state word.
        self.assertIn(
            "OPEN sesame",
            display_github_detail({**status, "title": "OPEN sesame"}),
        )
        self.assertTrue(
            display_github_detail({**status, "title": "OPEN"}).startswith("OPEN · draft")
        )
        self.assertTrue(
            display_github_detail({**status, "title": "Release · DRAFT"}).startswith(
                "Release · DRAFT · draft · open"
            )
        )
