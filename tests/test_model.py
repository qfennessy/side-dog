from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import render_timeline_activity
from side_dog.model import SOURCE_KEY, build_activity_units


BASE_EPOCH_MS = int(
    datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp() * 1000
)


def activity(
    offset_ms: int,
    kind: str,
    title: str,
    detail: str,
    *,
    root: Path,
    agent: str = "filesystem",
    status: str = "success",
    **extra: object,
) -> dict[str, object]:
    epoch_ms = BASE_EPOCH_MS + offset_ms
    return {
        "epoch_ms": epoch_ms,
        "timestamp": datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).isoformat(),
        "kind": kind,
        "title": title,
        "detail": detail,
        "agent": agent,
        "status": status,
        SOURCE_KEY: str(root),
        **extra,
    }


class DisplayModelCharacterizationTest(TestCase):
    def test_current_compact_timeline_frame_is_unchanged(self) -> None:
        first_root = Path("/work/one")
        second_root = Path("/work/two")
        records = [
            activity(0, "file", "File changed", "alpha.py", root=first_root),
            activity(1_000, "file", "File changed", "alpha.py", root=first_root),
            activity(
                2_000,
                "test",
                "Running tests",
                "pytest",
                root=first_root,
                agent="codex",
                status="running",
                operation_id="op",
                turn_id="turn",
            ),
            activity(
                3_000,
                "test",
                "Tests passed",
                "pytest",
                root=first_root,
                agent="codex",
                operation_id="op",
                turn_id="turn",
            ),
            activity(
                4_000,
                "commit",
                "Commit created",
                "abc1234 · feat: deliver",
                root=second_root,
                agent="git",
            ),
        ]

        with patch("side_dog.cli.display_time", return_value="12:00"):
            lines, hidden = render_timeline_activity(
                records,
                line_budget=30,
                width=90,
                color=False,
                now_ms=BASE_EPOCH_MS + 5_000,
                identities={},
                expanded_history=False,
                event_filter="all",
                local_timezone=timezone.utc,
                newest_first=True,
            )

        self.assertEqual(hidden, 0)
        self.assertEqual(
            "\n".join(lines),
            "├─ Today · Tue Sep 1 ─────────────────────────────────────────────────────────────────────\n"
            "│ 12:00 ◆ Commit · abc1234 · deliver\n"
            "│ 12:00 ┌ Codex · Agent task · 3.0s\n"
            "│   Tests ×2 ✓\n"
            "│ 12:00 ✎ changed · alpha.py · ×2",
        )

    def test_units_are_root_owned_and_never_merge_across_roots(self) -> None:
        first_root = Path("/work/one")
        second_root = Path("/work/two")
        events = [
            activity(
                0,
                "test",
                "Tests passed",
                "one",
                root=first_root,
                agent="codex",
                turn_id="shared-turn",
            ),
            activity(
                1_000,
                "commit",
                "Commit created",
                "one commit",
                root=first_root,
                agent="git",
                turn_id="shared-turn",
            ),
            activity(
                2_000,
                "test",
                "Tests passed",
                "two",
                root=second_root,
                agent="codex",
                turn_id="shared-turn",
            ),
            activity(
                3_000,
                "commit",
                "Commit created",
                "two commit",
                root=second_root,
                agent="git",
                turn_id="shared-turn",
            ),
        ]

        units = build_activity_units(events, expanded_history=False)

        self.assertEqual([unit["root"] for unit in units], [str(first_root), str(second_root)])
        self.assertEqual([unit["type"] for unit in units], ["pipeline", "pipeline"])
        for unit in units:
            self.assertEqual(
                {event[SOURCE_KEY] for event in unit["events"]},
                {unit["root"]},
            )

    def test_latest_backfill_notice_stays_out_of_agent_task_pipeline(self) -> None:
        root = Path("/work/one")
        session_id = "codex-session"
        events = [
            activity(
                0,
                "session",
                "Transcript indexed",
                "10 native events available",
                root=root,
                agent="codex",
                session_id=session_id,
                turn_id="turn",
                source_event_id=f"codex:{session_id}:history-indexed",
            ),
            activity(
                1_000,
                "test",
                "Tests passed",
                "unittest",
                root=root,
                agent="codex",
                session_id=session_id,
                turn_id="turn",
            ),
            activity(
                2_000,
                "session",
                "Transcript backfill complete",
                "12 native events available",
                root=root,
                agent="codex",
                session_id=session_id,
                turn_id="turn",
                source_event_id=(
                    f"codex:{session_id}:history-backfill-complete-v2"
                ),
            ),
        ]

        units = build_activity_units(events, expanded_history=False)

        self.assertEqual([unit["type"] for unit in units], ["event", "event"])
        self.assertEqual(
            [unit["events"][0]["title"] for unit in units],
            ["Tests passed", "Transcript backfill complete"],
        )

    def test_model_has_no_terminal_presentation_dependencies(self) -> None:
        source = (Path(__file__).parents[1] / "side_dog" / "model.py").read_text()

        self.assertNotIn("ANSI", source)
        self.assertNotIn("terminal", source.casefold())
        self.assertNotIn("width", source.casefold())
