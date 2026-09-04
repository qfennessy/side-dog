from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import render_timeline_activity
from side_dog.model import (
    REPOSITORY_KEY,
    SOURCE_KEY,
    SOURCE_LABEL,
    agent_label,
    build_activity_units,
    display_model,
    github_detail,
    identity_for_event,
    lane_key,
    lane_label,
    latest_delivery_context,
    normalize_agent,
)


BASE_EPOCH_MS = int(
    datetime(2026, 9, 1, 12, tzinfo=timezone.utc).timestamp() * 1000
)


class DeliveryContextTest(TestCase):
    def test_branch_switch_invalidates_the_previous_task_context(self) -> None:
        records = [
            {
                "kind": "push",
                "turn_id": "old-turn",
                "agent": "codex",
            },
            {
                "kind": "branch",
                "title": "Branch switched",
                "detail": "new-branch",
            },
        ]

        self.assertEqual(latest_delivery_context(records), {})

    def test_delivery_after_a_branch_switch_supplies_the_new_context(self) -> None:
        records = [
            {
                "kind": "branch",
                "title": "Branch switched",
                "detail": "new-branch",
            },
            {
                "kind": "push",
                "turn_id": "new-turn",
                "agent": "codex",
            },
        ]

        self.assertEqual(
            latest_delivery_context(records),
            {"turn_id": "new-turn", "agent": "codex"},
        )

    def test_github_confirmation_preserves_post_switch_delivery_context(self) -> None:
        records = [
            {"kind": "push", "turn_id": "new-turn", "agent": "codex"},
            {
                "kind": "branch",
                "title": "Branch switched",
                "detail": "new-branch",
            },
            {"kind": "github", "turn_id": "new-turn", "agent": "codex"},
        ]

        self.assertEqual(
            latest_delivery_context(records),
            {"turn_id": "new-turn", "agent": "codex"},
        )

    def test_branch_boundary_can_carry_same_poll_delivery_context(self) -> None:
        records = [
            {"kind": "push", "turn_id": "new-turn", "agent": "codex"},
            {
                "kind": "branch",
                "title": "Branch switched",
                "detail": "new-branch",
                "turn_id": "new-turn",
            },
        ]

        self.assertEqual(
            latest_delivery_context(records),
            {"turn_id": "new-turn"},
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
    def test_missing_agent_attribution_is_explicitly_unknown(self) -> None:
        for value in (None, "", "   "):
            with self.subTest(agent=value):
                self.assertEqual(normalize_agent(value), "unknown")
                self.assertEqual(agent_label(value), "Unknown")

        record = {
            "schema": "side-dog-activity-v1",
            "kind": "file",
            "title": "File changed",
            "detail": "alpha.py",
        }
        identity = identity_for_event(record, {})

        self.assertEqual(identity["agent"], "unknown")
        self.assertEqual(identity["label"], "Unknown")
        self.assertEqual(lane_key(record, {}), "unknown")
        self.assertEqual(lane_label({}), "Unknown")

    def test_explicit_claude_attribution_remains_readable(self) -> None:
        for alias in ("claude", "claude-code", " Claude-Code "):
            with self.subTest(agent=alias):
                self.assertEqual(normalize_agent(alias), "claude-code")
                self.assertEqual(agent_label(alias), "Claude")

        record = {
            "schema": "side-dog-activity-v1",
            "kind": "session",
            "title": "Session started",
            "detail": "",
            "agent": "claude-code",
            "session_id": "claude-session",
        }
        identity = identity_for_event(record, {})

        self.assertEqual(identity["agent"], "claude-code")
        self.assertEqual(identity["label"], "Claude claude-s")

    def test_deepseek_harness_aliases_share_one_display_identity(self) -> None:
        self.assertEqual(normalize_agent("dsh"), "deepseek")
        self.assertEqual(normalize_agent("deepseek-harness"), "deepseek")
        self.assertEqual(agent_label("deepseek"), "DeepSeek")

    def test_compact_timeline_frame_has_a_statusful_task_hierarchy(self) -> None:
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
                show_filesystem_activity=True,
            )

        self.assertEqual(hidden, 0)
        self.assertEqual(
            "\n".join(lines),
            "├─ Today · Tue Sep 1 ─────────────────────────────────────────────────────────────────────\n"
            "│ 12:00 ◆ Commit · abc1234 · deliver\n"
            "│ 12:00 ┌ Codex · Agent task · ✓ completed · 2 events · 3.0s\n"
            "│   └─ Tests ×2 ✓\n"
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

    def test_same_commit_message_and_author_folds_across_worktrees(self) -> None:
        first_root = Path("/work/one")
        second_root = Path("/work/two")
        repository = "/work/repository/.git"
        events = [
            activity(
                0,
                "commit",
                "Commit created",
                "d4fcc66 · polish multi-root header",
                root=first_root,
                agent="git",
                **{
                    REPOSITORY_KEY: repository,
                    SOURCE_LABEL: "feature",
                    "author": "Quentin Fennessy",
                },
            ),
            activity(
                1_000,
                "commit",
                "Commit created",
                "a1b2c3d · polish multi-root header",
                root=second_root,
                agent="git",
                **{
                    REPOSITORY_KEY: repository,
                    SOURCE_LABEL: "main",
                    "author": "Quentin Fennessy",
                },
            ),
        ]

        units = build_activity_units(
            events, expanded_history=False, local_timezone=timezone.utc
        )

        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0]["events"]), 1)
        detail = units[0]["events"][0]["detail"]
        self.assertIn("d4fcc66 · polish multi-root header", detail)
        self.assertIn("also on main", detail)
        self.assertNotIn("a1b2c3d", detail)

    def test_commit_fold_requires_repository_author_and_day_identity(self) -> None:
        first_root = Path("/work/one")
        second_root = Path("/work/two")
        base = activity(
            0,
            "commit",
            "Commit created",
            "d4fcc66 · same message",
            root=first_root,
            agent="git",
            **{
                REPOSITORY_KEY: "/work/repository/.git",
                "author": "first author",
            },
        )
        different_author = dict(base)
        different_author.update(
            {
                SOURCE_KEY: str(second_root),
                "detail": "a1b2c3d · same message",
                "author": "second author",
            }
        )
        no_repository = dict(different_author)
        no_repository.pop(REPOSITORY_KEY)

        self.assertEqual(
            len(
                build_activity_units(
                    [base, different_author],
                    expanded_history=False,
                    local_timezone=timezone.utc,
                )
            ),
            2,
        )
        self.assertEqual(
            len(
                build_activity_units(
                    [base, no_repository],
                    expanded_history=False,
                    local_timezone=timezone.utc,
                )
            ),
            2,
        )

    def test_unchanged_github_state_moves_to_the_latest_delivery_task(self) -> None:
        root = Path("/work/one")
        github = {
            "number": 7,
            "title": "Feature",
            "state": "OPEN",
            "ci": "CI 1/1",
            "merge_state": "BLOCKED",
        }
        events = [
            activity(
                0,
                "file",
                "Wrote file",
                "old.py",
                root=root,
                agent="codex",
                turn_id="old",
            ),
            activity(
                1_000,
                "push",
                "Branch pushed",
                "origin/old",
                root=root,
                agent="codex",
                turn_id="old",
            ),
            activity(
                2_000,
                "github",
                "PR #7 confirmed",
                "Feature",
                root=root,
                agent="github",
                turn_id="old",
                github=github,
            ),
            activity(
                3_000,
                "file",
                "Wrote file",
                "new.py",
                root=root,
                agent="codex",
                turn_id="new",
            ),
            activity(
                4_000,
                "push",
                "Branch pushed",
                "origin/new",
                root=root,
                agent="codex",
                turn_id="new",
            ),
            activity(
                5_000,
                "github",
                "PR #7 status updated",
                "Feature",
                root=root,
                agent="github",
                turn_id="new",
                github=github,
            ),
        ]

        pipelines = [
            unit
            for unit in build_activity_units(events, expanded_history=False)
            if unit["type"] == "pipeline"
        ]

        self.assertEqual(len(pipelines), 2)
        by_group = {unit["group"][1]: unit for unit in pipelines}
        self.assertIsNone(by_group["old"]["github"])
        self.assertEqual(by_group["new"]["github"], github)

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
                "Side Dog caught up on earlier activity",
                "12 earlier events already saved",
                root=root,
                agent="codex",
                session_id=session_id,
                turn_id="turn",
                source_event_id=(
                    f"codex:{session_id}:history-backfill-complete-v3"
                ),
            ),
        ]

        units = build_activity_units(events, expanded_history=False)

        self.assertEqual([unit["type"] for unit in units], ["event", "event"])
        self.assertEqual(
            [unit["events"][0]["title"] for unit in units],
            ["Tests passed", "Side Dog caught up on earlier activity"],
        )

    def test_lifecycle_rows_are_optional_background_units(self) -> None:
        events = [
            {
                "epoch_ms": 1_000,
                "timestamp": "2026-09-01T12:00:01+00:00",
                "kind": "lifecycle",
                "status": "success",
                "title": "Claude session active",
                "detail": "start",
                "agent": "claude-code",
            },
            {
                "epoch_ms": 2_000,
                "timestamp": "2026-09-01T12:00:02+00:00",
                "kind": "test",
                "status": "success",
                "title": "Tests passed",
                "detail": "unittest",
                "agent": "claude-code",
            },
        ]

        hidden = build_activity_units(events, expanded_history=False, show_lifecycle=False)
        shown = build_activity_units(events, expanded_history=False, show_lifecycle=True)

        self.assertEqual(
            [unit["events"][0]["title"] for unit in hidden], ["Tests passed"]
        )
        self.assertEqual(
            [unit["events"][0]["title"] for unit in shown],
            ["Claude session active", "Tests passed"],
        )

    def test_model_has_no_terminal_presentation_dependencies(self) -> None:
        source = (Path(__file__).parents[1] / "side_dog" / "model.py").read_text()

        self.assertNotIn("ANSI", source)
        self.assertNotIn("terminal", source.casefold())
        self.assertNotIn("width", source.casefold())


class DisplayModelTest(TestCase):
    def test_vendor_wrapping_is_trimmed_and_unknown_ids_are_left_alone(self) -> None:
        cases = {
            "claude-opus-5": "opus-5",
            "claude-haiku-4-5-20251001": "haiku-4-5",
            "us.anthropic.claude-sonnet-4-5-20250929-v1:0": "sonnet-4-5",
            "openai/gpt-5.6-sol": "gpt-5.6-sol",
            "gpt-5.6-sol": "gpt-5.6-sol",
            "codex-auto-review": "codex-auto-review",
            "": "",
        }
        for value, expected in cases.items():
            with self.subTest(model=value):
                self.assertEqual(display_model(value), expected)

    def test_a_bare_vendor_prefix_is_kept_rather_than_emptied(self) -> None:
        self.assertEqual(display_model("claude-"), "claude-")
        self.assertEqual(display_model(None), "")


class GithubDetailTest(TestCase):
    @staticmethod
    def status(**overrides: object) -> dict[str, object]:
        return {
            "title": "Add the note highway",
            "state": "MERGED",
            "ci": "CI 2/2",
            "merge_state": "UNKNOWN",
            **overrides,
        }

    def test_an_unknown_merge_state_is_left_out(self) -> None:
        detail = github_detail(self.status())

        self.assertEqual(detail, "Add the note highway · MERGED · CI 2/2")
        self.assertNotIn("UNKNOWN", detail)

    def test_a_real_merge_state_still_shows(self) -> None:
        detail = github_detail(self.status(state="OPEN", merge_state="BLOCKED"))

        self.assertEqual(detail, "Add the note highway · OPEN · CI 2/2 · BLOCKED")
