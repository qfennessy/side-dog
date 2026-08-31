import os
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
    display_conventional_subject,
    display_detail,
    display_root,
    display_title,
    emit_tool_event,
    events_path,
    github_event,
    github_fingerprint,
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
    def test_header_identifies_the_watched_folder(self) -> None:
        root = Path.home() / "src" / "side-dog"

        screen = render([], root, width=80, height=24, color=False)

        self.assertIn(" Watching ~/src/side-dog", screen)
        self.assertEqual(
            display_root(Path("/tmp/example-project")), "/tmp/example-project"
        )

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
        self.assertIn("r       toggle newest-first / oldest-first order", screen)
        self.assertIn("Codex · gpt-example · high · working", screen)
        self.assertIn("feature/sidebar @ 1234567", screen)

    def test_removed_file_label_is_compact(self) -> None:
        self.assertEqual(display_title({"title": "File removed"}), "removed")

    def test_help_explains_active_oldest_first_order(self) -> None:
        screen = render(
            [],
            Path("/tmp/example-project"),
            width=80,
            height=24,
            color=False,
            show_help=True,
            newest_first=False,
        )

        self.assertIn("Newest activity is at the bottom", screen)
        self.assertNotIn("Newest activity is at the top", screen)

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
        line_budget: int = 30,
        now_ms: int = 10_000,
        local_timezone: timezone | None = None,
        newest_first: bool = True,
    ) -> str:
        lines, _ = render_timeline_activity(
            events,
            line_budget=line_budget,
            width=100,
            color=False,
            now_ms=now_ms,
            identities={},
            expanded_history=expanded,
            event_filter=event_filter,
            local_timezone=local_timezone,
            newest_first=newest_first,
        )
        return "\n".join(lines)

    def test_each_displayed_local_date_has_one_separator(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        today = datetime(2026, 9, 1, 12, tzinfo=eastern)
        yesterday = datetime(2026, 8, 31, 12, tzinfo=eastern)
        two_days_ago = datetime(2026, 8, 30, 12, tzinfo=eastern)
        screen = self.render_lines(
            [
                event(
                    int(two_days_ago.timestamp() * 1000),
                    "file",
                    "File changed",
                    "old.py",
                ),
                event(
                    int(yesterday.timestamp() * 1000),
                    "test",
                    "Tests passed",
                    "unit",
                ),
                event(
                    int(today.timestamp() * 1000),
                    "commit",
                    "Commit created",
                    "abc1234 current",
                    agent="git",
                ),
            ],
            expanded=True,
            now_ms=int(today.timestamp() * 1000),
            local_timezone=eastern,
        )

        self.assertEqual(screen.count("Today · Tue Sep 1"), 1)
        self.assertEqual(screen.count("Mon Aug 31, 2026"), 1)
        self.assertEqual(screen.count("Sun Aug 30, 2026"), 1)
        self.assertLess(screen.index("Today · Tue Sep 1"), screen.index("current"))
        self.assertLess(screen.index("current"), screen.index("Mon Aug 31, 2026"))
        self.assertLess(screen.index("unit"), screen.index("Sun Aug 30, 2026"))

    def test_same_day_events_share_one_date_separator(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        morning = datetime(2026, 9, 1, 9, tzinfo=eastern)
        afternoon = datetime(2026, 9, 1, 15, tzinfo=eastern)
        screen = self.render_lines(
            [
                event(
                    int(morning.timestamp() * 1000),
                    "file",
                    "File changed",
                    "am.py",
                ),
                event(
                    int(afternoon.timestamp() * 1000),
                    "file",
                    "File changed",
                    "pm.py",
                ),
            ],
            expanded=True,
            now_ms=int(afternoon.timestamp() * 1000),
            local_timezone=eastern,
        )

        self.assertEqual(screen.count("Today · Tue Sep 1"), 1)

    def test_filter_does_not_leave_or_duplicate_date_separators(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        yesterday = datetime(2026, 8, 31, 12, tzinfo=eastern)
        today = datetime(2026, 9, 1, 12, tzinfo=eastern)
        screen = self.render_lines(
            [
                event(
                    int(yesterday.timestamp() * 1000),
                    "file",
                    "File changed",
                    "hidden.py",
                ),
                event(
                    int(today.timestamp() * 1000),
                    "test",
                    "Tests passed",
                    "unit",
                    agent="codex",
                ),
            ],
            expanded=True,
            event_filter="milestones",
            now_ms=int(today.timestamp() * 1000),
            local_timezone=eastern,
        )

        self.assertEqual(screen.count("Today · Tue Sep 1"), 1)
        self.assertNotIn("Mon Aug 31", screen)
        self.assertNotIn("hidden.py", screen)

    def test_cross_midnight_file_events_do_not_collapse_across_dates(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        before = datetime(2026, 8, 31, 23, 59, 30, tzinfo=eastern)
        after = datetime(2026, 9, 1, 0, 0, 30, tzinfo=eastern)
        screen = self.render_lines(
            [
                event(
                    int(before.timestamp() * 1000),
                    "file",
                    "File changed",
                    "same.py",
                ),
                event(
                    int(after.timestamp() * 1000),
                    "file",
                    "File changed",
                    "same.py",
                ),
            ],
            now_ms=int(after.timestamp() * 1000),
            local_timezone=eastern,
        )

        self.assertEqual(screen.count("Today · Tue Sep 1"), 1)
        self.assertEqual(screen.count("Mon Aug 31, 2026"), 1)
        self.assertEqual(screen.count("same.py"), 2)
        self.assertNotIn("×2", screen)

    def test_timezone_controls_which_side_of_midnight_events_use(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        before = datetime(2026, 9, 1, 3, 30, tzinfo=timezone.utc)
        after = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)
        screen = self.render_lines(
            [
                event(
                    int(before.timestamp() * 1000),
                    "file",
                    "File changed",
                    "before.py",
                ),
                event(
                    int(after.timestamp() * 1000),
                    "file",
                    "File changed",
                    "after.py",
                ),
            ],
            expanded=True,
            now_ms=int(after.timestamp() * 1000),
            local_timezone=eastern,
        )

        self.assertIn("Today · Tue Sep 1", screen)
        self.assertIn("Mon Aug 31, 2026", screen)

    def test_date_separator_and_event_are_not_split_at_viewport_edge(self) -> None:
        eastern = timezone(timedelta(hours=-4))
        yesterday = datetime(2026, 8, 31, 12, tzinfo=eastern)
        today = datetime(2026, 9, 1, 12, tzinfo=eastern)
        lines, hidden = render_timeline_activity(
            [
                event(
                    int(yesterday.timestamp() * 1000),
                    "file",
                    "File changed",
                    "old.py",
                ),
                event(
                    int(today.timestamp() * 1000),
                    "file",
                    "File changed",
                    "new.py",
                ),
            ],
            line_budget=2,
            width=100,
            color=False,
            now_ms=int(today.timestamp() * 1000),
            identities={},
            expanded_history=True,
            event_filter="all",
            local_timezone=eastern,
        )

        self.assertEqual(len(lines), 2)
        self.assertIn("Today · Tue Sep 1", lines[0])
        self.assertIn("new.py", lines[1])
        self.assertEqual(hidden, 1)

        one_line, one_line_hidden = render_timeline_activity(
            [
                event(
                    int(yesterday.timestamp() * 1000),
                    "file",
                    "File changed",
                    "old.py",
                ),
                event(
                    int(today.timestamp() * 1000),
                    "file",
                    "File changed",
                    "new.py",
                ),
            ],
            line_budget=1,
            width=100,
            color=False,
            now_ms=int(today.timestamp() * 1000),
            identities={},
            expanded_history=True,
            event_filter="all",
            local_timezone=eastern,
        )

        self.assertEqual(one_line, [])
        self.assertEqual(one_line_hidden, 2)

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

    def test_reversed_order_places_newest_activity_at_bottom_and_toggles_back(
        self,
    ) -> None:
        events = [
            event(1_000, "commit", "Commit created", "abc1234 oldest", agent="git"),
            event(2_000, "test", "Tests passed", "newest", agent="codex"),
        ]

        reversed_screen = self.render_lines(events, expanded=True, newest_first=False)
        restored_screen = self.render_lines(events, expanded=True, newest_first=True)

        self.assertLess(
            reversed_screen.index("abc1234 oldest"), reversed_screen.index("newest")
        )
        self.assertLess(
            restored_screen.index("newest"), restored_screen.index("abc1234 oldest")
        )

    def test_reversed_order_preserves_append_order_for_equal_timestamps(self) -> None:
        events = [
            event(2_000, "commit", "Commit created", "first appended", agent="git"),
            event(2_000, "test", "Tests passed", "second appended", agent="codex"),
        ]

        screen = self.render_lines(events, expanded=True, newest_first=False)

        self.assertLess(screen.index("first appended"), screen.index("second appended"))

    def test_reversed_order_keeps_markers_before_multiple_local_date_groups(
        self,
    ) -> None:
        eastern = timezone(timedelta(hours=-4))
        yesterday_morning = datetime(2026, 8, 31, 9, tzinfo=eastern)
        yesterday_afternoon = datetime(2026, 8, 31, 15, tzinfo=eastern)
        today_morning = datetime(2026, 9, 1, 9, tzinfo=eastern)
        today_afternoon = datetime(2026, 9, 1, 15, tzinfo=eastern)
        screen = self.render_lines(
            [
                event(
                    int(yesterday_morning.timestamp() * 1000),
                    "commit",
                    "Commit created",
                    "old-day morning",
                    agent="git",
                ),
                event(
                    int(yesterday_afternoon.timestamp() * 1000),
                    "test",
                    "Tests passed",
                    "old-day afternoon",
                    agent="codex",
                ),
                event(
                    int(today_morning.timestamp() * 1000),
                    "commit",
                    "Commit created",
                    "today morning",
                    agent="git",
                ),
                event(
                    int(today_afternoon.timestamp() * 1000),
                    "test",
                    "Tests passed",
                    "today newest",
                    agent="codex",
                ),
            ],
            expanded=True,
            now_ms=int(today_afternoon.timestamp() * 1000),
            local_timezone=eastern,
            newest_first=False,
        )

        positions = [
            screen.index("Mon Aug 31, 2026"),
            screen.index("old-day morning"),
            screen.index("old-day afternoon"),
            screen.index("Today · Tue Sep 1"),
            screen.index("today morning"),
            screen.index("today newest"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_reversed_tight_view_keeps_newest_date_marker_and_event_atomic(
        self,
    ) -> None:
        now = int(time.time() * 1000)
        screen = render(
            [
                event(now - 172_800_000, "file", "File changed", "older.py"),
                event(now, "file", "File changed", "newest.py"),
            ],
            Path("/tmp/project"),
            width=100,
            height=6,
            color=False,
            expanded_history=True,
            newest_first=False,
        )

        self.assertIn("· 1 above", screen)
        self.assertIn("├─ Today ·", screen)
        self.assertIn("newest.py", screen)
        self.assertNotIn("older.py", screen)
        self.assertLess(screen.index("├─ Today ·"), screen.index("newest.py"))

    def test_reversed_compact_filter_keeps_latest_unit_visible_at_bottom(self) -> None:
        events = [
            event(1_000, "test", "Tests passed", "older tests", agent="codex"),
            event(2_000, "file", "File changed", "hidden.py"),
            event(3_000, "test", "Tests failed", "newer tests", agent="codex"),
        ]

        screen = self.render_lines(
            events,
            event_filter="milestones",
            newest_first=False,
        )

        self.assertNotIn("hidden.py", screen)
        self.assertLess(screen.index("older tests"), screen.index("newer tests"))

    def test_reversed_render_preserves_input_event_order_and_content(self) -> None:
        events = [
            event(1_000, "file", "File changed", "alpha.py"),
            event(2_000, "file", "File changed", "beta.py"),
            event(3_000, "commit", "Commit created", "abc1234 latest", agent="git"),
        ]
        original = deepcopy(events)

        screen = render(
            events,
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
            paused=True,
            new_event_count=2,
            newest_first=False,
        )

        self.assertEqual(events, original)
        self.assertIn("┌ oldest first · compact · all · PAUSED · 2 new", screen)
        self.assertIn("r newest", screen)
        self.assertLess(screen.index("Files · 2 changed"), screen.index("abc1234"))

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

    def test_atomic_milestone_reserves_duration_before_cropping_detail(self) -> None:
        milestone = event(
            14_000,
            "commit",
            "Commit created",
            "abc1234 fix a conventional length production commit subject",
            agent="codex",
            started_epoch_ms=2_000,
        )

        lines = render_milestone_card(milestone, 80, False, 14_000, {})

        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 80)
        self.assertIn("abc1234", lines[0])
        self.assertTrue(lines[0].endswith(" · 12s"))

    def test_conventional_prefixes_are_removed_only_for_display(self) -> None:
        self.assertEqual(
            display_conventional_subject("fix(sidebar)!: preserve date signal"),
            "preserve date signal",
        )
        self.assertEqual(
            display_conventional_subject("abc1234 · chore(ui): align rows"),
            "abc1234 · align rows",
        )
        self.assertEqual(
            display_conventional_subject("Update docs: explain the rationale"),
            "Update docs: explain the rationale",
        )

        milestone = event(
            2_000,
            "commit",
            "Commit created",
            "abc1234 · fix(sidebar): keep rows short",
            agent="git",
        )
        original = deepcopy(milestone)
        rendered = render_milestone_card(milestone, 100, False, 2_000, {})[0]

        self.assertIn("abc1234 · keep rows short", rendered)
        self.assertNotIn("fix(sidebar):", rendered)
        self.assertEqual(milestone, original)

    def test_pr_titles_are_normalized_only_in_timeline_and_banner(self) -> None:
        status = {
            "number": 4,
            "title": "feat(sidebar)!: show day boundaries",
            "state": "OPEN",
            "ci": "CI —",
            "merge_state": "CLEAN",
        }
        source_status = deepcopy(status)
        source_fingerprint = github_fingerprint(status)
        github = github_event(status, None, {})
        source_event = deepcopy(github)

        banner = render_github_banner(status, 100, False)
        milestone = render_milestone_card(github, 100, False, 2_000, {})[0]

        self.assertIn("show day boundaries", banner)
        self.assertIn("show day boundaries", milestone)
        self.assertNotIn("feat(sidebar)!:", banner)
        self.assertNotIn("feat(sidebar)!:", milestone)
        self.assertEqual(status, source_status)
        self.assertEqual(github, source_event)
        self.assertIn("feat(sidebar)!:", github["detail"])
        self.assertEqual(github_fingerprint(status), source_fingerprint)

        command_event = event(
            3_000,
            "pr",
            "PR create command succeeded",
            "feat: show date markers",
            agent="codex",
        )
        command_source = deepcopy(command_event)
        command_line = render_milestone_card(command_event, 100, False, 3_000, {})[0]
        self.assertIn("show date markers", command_line)
        self.assertNotIn("feat:", command_line)
        self.assertEqual(command_event, command_source)

    def test_extreme_duration_cannot_wrap_minimum_width_milestone(self) -> None:
        milestone = event(
            60_000_000,
            "test",
            "Tests passed",
            "a long-running integration test target",
            agent="codex",
            started_epoch_ms=0,
        )

        lines = render_milestone_card(milestone, 28, False, 60_000_000, {})

        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), 28)
        self.assertIn("1000m00s", lines[0])

    def test_unchanged_pr_status_is_not_repeated(self) -> None:
        open_status = {
            "number": 3,
            "title": "Feature",
            "state": "OPEN",
            "ci": "CI —",
            "merge_state": "CLEAN",
            "updated_at": "2026-08-31T18:00:00Z",
        }
        refreshed_status = {
            **open_status,
            "updated_at": "2026-08-31T18:05:00Z",
        }
        merged_status = {
            **refreshed_status,
            "state": "MERGED",
            "merge_state": "UNKNOWN",
        }
        screen = self.render_lines(
            [
                event(
                    1_000,
                    "github",
                    "PR #3 confirmed",
                    "",
                    agent="github",
                    github=open_status,
                    github_state="OPEN",
                ),
                event(
                    2_000,
                    "github",
                    "PR #3 status updated",
                    "",
                    agent="github",
                    github=refreshed_status,
                    github_state="OPEN",
                ),
                event(
                    3_000,
                    "github",
                    "PR #3 merged",
                    "",
                    agent="github",
                    github=merged_status,
                    github_state="MERGED",
                ),
            ],
            expanded=True,
        )

        self.assertEqual(
            github_fingerprint(open_status), github_fingerprint(refreshed_status)
        )
        self.assertEqual(screen.count("Feature · OPEN"), 1)
        self.assertEqual(screen.count("Feature · MERGED"), 1)

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
