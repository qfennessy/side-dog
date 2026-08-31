import os
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    ANSI,
    STATE_ENV,
    actor_label,
    classify_commands,
    coalesce_operations,
    display_detail,
    display_title,
    emit_tool_event,
    events_path,
    github_event,
    is_definitive_no_pr,
    is_side_dog_hook_command,
    latest_events,
    render,
    render_github_banner,
    render_milestone_card,
    render_timeline_activity,
    shell_command_is_compound,
)


class RenderHelpTest(TestCase):
    def test_help_shows_controls_and_current_commit(self) -> None:
        screen = render(
            [],
            Path("/tmp/example-project"),
            width=80,
            height=24,
            color=False,
            identities={
                "codex-session": {
                    "agent": "codex",
                    "pane_id": "w1:p1",
                    "label": "Codex",
                    "model": "gpt-example",
                    "effort": "high",
                    "status": "working",
                }
            },
            git_status={
                "branch": "feature/sidebar",
                "oid": "1234567890abcdef",
                "short_oid": "1234567",
            },
            show_help=True,
        )

        self.assertIn("┌ Help", screen)
        self.assertIn("?       toggle this help", screen)
        self.assertIn("e       toggle compact / expanded detail", screen)
        self.assertIn("Codex · gpt-example · high · working", screen)
        self.assertIn("feature/sidebar @ 1234567", screen)

    def test_removed_file_label_is_compact(self) -> None:
        self.assertEqual(display_title({"title": "File removed"}), "removed")

    def test_verified_github_event_is_not_misattributed(self) -> None:
        event = github_event(
            {
                "number": 3,
                "title": "Add useful activity names",
                "state": "OPEN",
                "ci": "CI none",
            },
            None,
            {},
        )

        self.assertEqual(event["agent"], "github")
        self.assertEqual(actor_label(event, {}), "")
        self.assertIn("Add useful activity names", event["detail"])
        event["detail"] = "stale cached detail"
        self.assertIn("Add useful activity names", display_detail(event))

    def test_command_titles_are_helpful_without_recording_bodies(self) -> None:
        issue = classify_commands(
            "gh issue create --title 'Improve timeline layout' "
            "--body 'private implementation detail'"
        )
        pull_request = classify_commands(
            "gh pr create --title='Add activity names' --body-file notes.md"
        )

        self.assertEqual(issue, [("issue", "Opening issue", "Improve timeline layout")])
        self.assertEqual(
            pull_request,
            [("pr", "Opening pull request", "Add activity names")],
        )
        self.assertNotIn("private implementation detail", repr(issue))


def event(
    epoch_ms: int,
    kind: str,
    title: str,
    detail: str,
    *,
    agent: str = "filesystem",
    status: str = "success",
    **extra: object,
) -> dict[str, object]:
    timestamp = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).isoformat()
    return {
        "epoch_ms": epoch_ms,
        "timestamp": timestamp,
        "kind": kind,
        "title": title,
        "detail": detail,
        "agent": agent,
        "status": status,
        **extra,
    }


class TimelineTest(TestCase):
    def render_lines(
        self,
        events: list[dict[str, object]],
        *,
        expanded: bool = False,
        event_filter: str = "all",
    ) -> str:
        lines, _ = render_timeline_activity(
            events,
            line_budget=30,
            width=100,
            color=False,
            now_ms=10_000,
            identities={},
            expanded_history=expanded,
            event_filter=event_filter,
        )
        return "\n".join(lines)

    def test_newest_activity_is_rendered_first(self) -> None:
        screen = self.render_lines(
            [
                event(1_000, "file", "File changed", "old.py"),
                event(
                    2_000,
                    "commit",
                    "Commit created",
                    "abc1234 newest commit",
                    agent="git",
                ),
            ],
            expanded=True,
        )

        self.assertLess(screen.index("newest commit"), screen.index("old.py"))

    def test_compact_view_uses_height_for_older_activity(self) -> None:
        now = int(time.time() * 1000)
        screen = render(
            [
                event(now - 86_400_000, "file", "File changed", "yesterday.py"),
                event(now, "file", "File changed", "today.py"),
            ],
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
        )

        self.assertIn("today.py", screen)
        self.assertIn("yesterday.py", screen)
        self.assertLess(screen.index("today.py"), screen.index("yesterday.py"))

    def test_filesystem_burst_summarizes_changes_and_paths(self) -> None:
        screen = self.render_lines(
            [
                event(1_000, "file", "File changed", "alpha.py"),
                event(2_000, "config", "Config changed", "settings.json"),
            ]
        )

        self.assertIn("Files · 2 changed · 2 paths", screen)
        self.assertIn("alpha.py", screen)
        self.assertIn("settings.json", screen)

    def test_delivery_pipeline_connects_milestones(self) -> None:
        shared = {"turn_id": "turn-1", "agent": "codex"}
        screen = self.render_lines(
            [
                event(1_000, "file", "Wrote file", "app.py", **shared),
                event(2_000, "test", "Tests passed", "unittest", **shared),
                event(
                    3_000,
                    "commit",
                    "Commit created",
                    "abc1234 add feature",
                    **shared,
                ),
                event(4_000, "push", "Branch pushed", "origin", **shared),
                event(
                    5_000, "pr", "PR create command succeeded", "gh pr create", **shared
                ),
            ],
            expanded=True,
        )

        self.assertIn(
            "Edit ×1 → Tests ✓ → Commit abc1234 → Push ✓ → PR ✓",
            screen,
        )

    def test_milestone_filter_hides_passive_files(self) -> None:
        screen = self.render_lines(
            [
                event(1_000, "file", "File changed", "hidden.py"),
                event(2_000, "test", "Tests passed", "unittest", agent="codex"),
            ],
            expanded=True,
            event_filter="milestones",
        )

        self.assertIn("Tests passed", screen)
        self.assertNotIn("hidden.py", screen)

    def test_atomic_milestone_uses_one_line_at_narrow_and_wide_widths(self) -> None:
        milestone = event(
            2_000,
            "commit",
            "Commit created",
            "abc1234 fix production corruption",
            agent="codex",
        )

        narrow = render_milestone_card(milestone, 28, False, 2_000, {})
        wide = render_milestone_card(milestone, 120, False, 2_000, {})

        self.assertEqual(len(narrow), 1)
        self.assertEqual(len(wide), 1)
        self.assertLessEqual(len(narrow[0]), 28)
        self.assertIn("Commit", narrow[0])
        self.assertIn("abc1234", narrow[0])
        self.assertIn("Codex · Commit · abc1234 fix production corruption", wide[0])

    def test_pause_state_shows_new_event_count(self) -> None:
        screen = render(
            [event(int(time.time() * 1000), "file", "File changed", "app.py")],
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
            paused=True,
            new_event_count=3,
        )

        self.assertIn("PAUSED · 3 new", screen)
        self.assertIn("p resume", screen)


class ReviewFeedbackTest(TestCase):
    def test_hook_ownership_is_exact(self) -> None:
        self.assertTrue(
            is_side_dog_hook_command(
                "SIDE_DOG_MANAGED=1 /usr/local/bin/side-dog hook --root /tmp/repo"
            )
        )
        self.assertTrue(
            is_side_dog_hook_command(
                "/usr/bin/python3 /tmp/side_dog/cli.py hook --root /tmp/repo"
            )
        )
        self.assertFalse(
            is_side_dog_hook_command(
                "/usr/bin/python3 /tmp/custom_side_dog_backup.py --root /tmp/repo"
            )
        )
        self.assertFalse(is_side_dog_hook_command("echo side-dog status --root /tmp"))

    def test_compound_command_outcome_is_unknown(self) -> None:
        self.assertTrue(shell_command_is_compound("pytest; echo cleanup"))
        self.assertFalse(shell_command_is_compound("pytest -k 'value;other'"))

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            state = Path(temporary) / "state"
            root.mkdir()
            payload = {
                "session_id": "session-1",
                "tool_name": "Bash",
                "tool_use_id": "tool-1",
                "tool_input": {"command": "pytest; echo cleanup"},
            }
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                emit_tool_event(payload, root, status="success")
                recorded = latest_events(events_path(root))

        self.assertEqual(recorded[-1]["status"], "unknown")
        self.assertEqual(recorded[-1]["title"], "Tests finished")

    def test_github_lifecycle_events_have_distinct_operation_ids(self) -> None:
        opened = github_event(
            {"number": 3, "title": "Feature", "state": "OPEN", "ci": "CI —"},
            None,
            {},
        )
        merged = github_event(
            {"number": 3, "title": "Feature", "state": "MERGED", "ci": "CI 1/1"},
            opened["github"],
            {},
        )

        self.assertNotEqual(opened["operation_id"], merged["operation_id"])

    def test_historical_github_states_are_never_coalesced(self) -> None:
        records = [
            {
                "kind": "github",
                "operation_id": "legacy-github-pr-3",
                "github_state": "OPEN",
            },
            {
                "kind": "github",
                "operation_id": "legacy-github-pr-3",
                "github_state": "MERGED",
            },
        ]

        self.assertEqual(len(coalesce_operations(records)), 2)

    def test_definitive_no_pr_is_distinguished_from_transient_error(self) -> None:
        self.assertTrue(is_definitive_no_pr("no pull requests found for branch main"))
        self.assertFalse(is_definitive_no_pr("failed to connect to api.github.com"))

    def test_open_pr_is_not_red_without_a_failure(self) -> None:
        banner = render_github_banner(
            {
                "number": 3,
                "title": "Feature",
                "state": "OPEN",
                "ci": "CI —",
                "merge_state": "UNKNOWN",
            },
            width=100,
            color=True,
        )
        failed = render_github_banner(
            {
                "number": 3,
                "title": "Feature",
                "state": "OPEN",
                "ci": "CI 1 failed",
                "checks_failed": 1,
            },
            width=100,
            color=True,
        )

        self.assertIn(ANSI["blue"], banner)
        self.assertNotIn(ANSI["red"], banner)
        self.assertIn(ANSI["red"], failed)
