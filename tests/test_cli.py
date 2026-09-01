import json
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
    root_color,
    classify_commands,
    command_program,
    display_detail,
    display_root,
    display_title,
    emit_tool_event,
    events_path,
    format_duration,
    github_event,
    github_progress_title,
    is_definitive_no_pr,
    is_side_dog_hook_command,
    latest_events,
    normalized_tool_events,
    render,
    SOURCE_COLOR_INDEX,
    WebPanel,
    activity_count,
    claude_identities,
    claude_session_registry,
    load_agent_identities,
    crop,
    crop_to_match,
    activity_meter,
    append_search_byte,
    event_matches_search,
    display_settings_path,
    load_display_settings,
    save_display_settings,
    search_notice,
    launch_web_panel,
    panel_url_from_output,
    render_github_banner,
    side_dog_command,
    render_help,
    render_milestone_card,
    render_timeline_activity,
    shell_command_is_compound,
)
from side_dog.model import (
    SOURCE_KEY,
    SOURCE_LABEL,
    actor_label,
    carry_forward_merge_state,
    coalesce_operations,
    display_conventional_subject,
    github_fingerprint,
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
        self.assertNotIn("Folder colors", screen)
        self.assertIn("PR/CI text: blue open · yellow pending", screen)
        self.assertIn("? could not tell", screen)
        self.assertIn("A task card links one agent turn", screen)
        self.assertIn("Codex · gpt-example · high · working", screen)
        self.assertIn("feature/sidebar @ 1234567", screen)

    def test_help_explains_root_colors_only_when_roots_are_shared(self) -> None:
        one_root = "\n".join(render_help(80, False, True, root_count=1))
        many_roots = "\n".join(render_help(80, False, True, root_count=3))

        self.assertNotIn("Folder colors", one_root)
        self.assertIn("Folder colors", many_roots)
        self.assertIn("all share one color", many_roots)

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

    def test_coalesced_completion_uses_final_append_order_for_epoch_ties(self) -> None:
        records = [
            event(
                1_000,
                "test",
                "Running tests",
                "coalesced operation",
                agent="codex",
                status="running",
                operation_id="test-op",
            ),
            event(
                2_000,
                "commit",
                "Commit created",
                "commit appended between",
                agent="git",
            ),
            event(
                2_000,
                "test",
                "Tests passed",
                "coalesced operation",
                agent="codex",
                operation_id="test-op",
            ),
        ]

        newest = render(
            records,
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
            expanded_history=True,
        )
        oldest = render(
            records,
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
            expanded_history=True,
            newest_first=False,
        )

        self.assertLess(
            newest.index("coalesced operation"),
            newest.index("commit appended between"),
        )
        self.assertLess(
            oldest.index("commit appended between"),
            oldest.index("coalesced operation"),
        )

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
        self.assertIn("16h40m", lines[0])

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


class FailedCommandTest(TestCase):
    @staticmethod
    def events(command: str, status: str) -> list[dict[str, object]]:
        return normalized_tool_events(
            {
                "tool_name": "Bash",
                "tool_use_id": "call-1",
                "session_id": "session",
                "agent": "codex",
                "tool_input": {"command": command},
            },
            Path("/tmp"),
            status=status,
        )

    def test_a_failed_command_is_reported_even_when_its_work_is_not(self) -> None:
        events = self.events("./scripts/deploy", "failed")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "command")
        self.assertEqual(events[0]["title"], "Command failed")
        self.assertEqual(events[0]["detail"], "deploy")
        self.assertEqual(events[0]["status"], "failed")

    def test_the_same_command_is_silent_while_running_and_on_success(self) -> None:
        self.assertEqual(self.events("./scripts/deploy", "running"), [])
        self.assertEqual(self.events("./scripts/deploy", "success"), [])
        self.assertEqual(self.events("./scripts/deploy", "unknown"), [])

    def test_a_compound_failure_stays_out_because_the_cause_is_ambiguous(self) -> None:
        self.assertEqual(self.events("pwd && rg pattern src/", "failed"), [])
        self.assertEqual(self.events("./build || true", "failed"), [])

    def test_a_search_that_finds_nothing_is_not_a_failure(self) -> None:
        for command in ("rg pattern src/", "grep -n needle file", "find . -name x"):
            with self.subTest(command=command):
                self.assertEqual(self.events(command, "failed"), [])

    def test_a_classified_command_keeps_its_own_failure_title(self) -> None:
        events = self.events("python -m unittest discover", "failed")

        self.assertEqual([event["kind"] for event in events], ["test"])
        self.assertEqual(events[0]["title"], "Tests failed")

    def test_the_reported_program_never_repeats_arguments_or_paths(self) -> None:
        events = self.events("TOKEN=s3cret /Users/someone/bin/publish --now", "failed")

        self.assertEqual(events[0]["detail"], "publish")
        self.assertNotIn("s3cret", json.dumps(events))
        self.assertNotIn("someone", json.dumps(events))

    def test_command_program_skips_wrappers_and_falls_back(self) -> None:
        self.assertEqual(command_program("sudo /usr/local/bin/wipe --all"), "wipe")
        self.assertEqual(command_program("env FOO=1 make"), "make")
        self.assertEqual(command_program(""), "command")
        self.assertEqual(command_program("'unbalanced"), "unbalanced")


class DisplayDensityTest(TestCase):
    @staticmethod
    def sourced(
        epoch_ms: int,
        kind: str,
        title: str,
        detail: str,
        label: str,
        **extra: object,
    ) -> dict[str, object]:
        return {
            **event(epoch_ms, kind, title, detail, agent="codex", **extra),
            SOURCE_KEY: f"/tmp/{label}",
            SOURCE_LABEL: label,
        }

    def render(
        self, events: list[dict[str, object]], budget: int = 20, color: bool = False
    ) -> list[str]:
        lines, _ = render_timeline_activity(
            events, budget, 90, color, 10_000_000, {}, False, "all"
        )
        return lines

    @staticmethod
    def run_of_roots() -> list[dict[str, object]]:
        return [
            {
                **DisplayDensityTest.sourced(
                    1_000 * step, "commit", "Commit created", f"change {step}", label
                ),
                SOURCE_COLOR_INDEX: "0" if label == "main" else "1",
            }
            for step, label in enumerate(("main", "main", "review", "main"), start=1)
        ]

    def test_the_root_badge_is_printed_only_when_the_root_changes(self) -> None:
        lines = self.render(self.run_of_roots(), color=True)

        self.assertEqual(sum(line.count("[main]") for line in lines), 2)
        self.assertEqual(sum(line.count("[review]") for line in lines), 1)

    def test_every_line_keeps_its_root_color_on_the_left_edge(self) -> None:
        lines = self.render(self.run_of_roots(), color=True)

        body = [line for line in lines if "Commit" in line]
        self.assertEqual(len(body), 4)
        for line in body:
            with self.subTest(line=line):
                self.assertTrue(
                    line.startswith(f"{root_color(0)}  {ANSI['reset']}")
                    or line.startswith(f"{root_color(1)}  {ANSI['reset']}"),
                    line,
                )

    def test_without_color_every_line_keeps_its_badge(self) -> None:
        lines = self.render(self.run_of_roots())

        self.assertEqual(sum(line.count("[main]") for line in lines), 3)
        self.assertEqual(sum(line.count("[review]") for line in lines), 1)

    def test_the_topmost_line_always_carries_its_root(self) -> None:
        events = [
            self.sourced(1_000, "commit", "Commit created", "first", "main"),
            self.sourced(2_000, "commit", "Commit created", "second", "main"),
        ]

        newest_first = self.render(events, color=True)
        oldest_first, _ = render_timeline_activity(
            events, 20, 90, True, 10_000_000, {}, False, "all", newest_first=False
        )

        self.assertIn("[main]", newest_first[1])
        self.assertIn("[main]", oldest_first[1])

    def test_a_sweep_of_pull_request_reads_collapses_to_one_line(self) -> None:
        events = [
            self.sourced(
                1_000 + number,
                "github",
                f"PR #{number} confirmed",
                "a pull request",
                f"PR #{number}",
                github={"number": number, "state": "MERGED"},
                github_state="MERGED",
            )
            for number in (17, 18, 19)
        ]

        lines = self.render(events)

        body = [line for line in lines if "PR" in line]
        self.assertEqual(len(body), 1)
        self.assertIn("PRs · 3 confirmed · #17 #18 #19", body[0])

    def test_a_single_confirmation_keeps_its_own_line(self) -> None:
        events = [
            self.sourced(
                1_000,
                "github",
                "PR #17 confirmed",
                "a pull request",
                "PR #17",
                github={"number": 17, "state": "OPEN"},
                github_state="OPEN",
            )
        ]

        lines = self.render(events)

        self.assertTrue(any("PR #17 confirmed" in line for line in lines), lines)
        self.assertFalse(any("confirmed · #" in line for line in lines), lines)

    def test_a_real_pull_request_change_is_never_collapsed(self) -> None:
        events = [
            self.sourced(
                1_000,
                "github",
                "PR #17 confirmed",
                "a pull request",
                "PR #17",
                github={"number": 17, "state": "OPEN"},
            ),
            self.sourced(
                2_000,
                "github",
                "PR #18 merged",
                "another pull request",
                "PR #18",
                github={"number": 18, "state": "MERGED"},
            ),
        ]

        lines = self.render(events)

        self.assertTrue(any("PR #18 merged" in line for line in lines), lines)


class GithubChangeDetectionTest(TestCase):
    @staticmethod
    def status(**overrides: object) -> dict[str, object]:
        return {
            "number": 31,
            "title": "See agent model and effort",
            "state": "OPEN",
            "ci": "CI 2/2",
            "review": "APPROVED",
            "merge_state": "CLEAN",
            "mergeable": "MERGEABLE",
            **overrides,
        }

    def test_invisible_churn_does_not_look_like_a_change(self) -> None:
        before = self.status()
        after = self.status(mergeable="UNKNOWN")

        self.assertEqual(github_fingerprint(before), github_fingerprint(after))

    def test_an_unknown_merge_state_does_not_look_like_a_change(self) -> None:
        before = self.status(merge_state="")
        after = self.status(merge_state="UNKNOWN")

        self.assertEqual(github_fingerprint(before), github_fingerprint(after))

    def test_a_visible_change_still_registers(self) -> None:
        before = self.status()

        for field, value in (
            ("state", "MERGED"),
            ("checks_failed", 1),
            ("review", "CHANGES_REQUESTED"),
            ("merge_state", "BLOCKED"),
            ("title", "Something else"),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(
                    github_fingerprint(before),
                    github_fingerprint(self.status(**{field: value})),
                )

    def test_a_ticking_checks_counter_is_not_news(self) -> None:
        running = self.status(
            ci="CI 0/2", checks_total=2, checks_passed=0, checks_pending=2
        )
        halfway = self.status(
            ci="CI 1/2", checks_total=2, checks_passed=1, checks_pending=1
        )
        finished = self.status(
            ci="CI 2/2", checks_total=2, checks_passed=2, checks_pending=0
        )

        self.assertEqual(github_fingerprint(running), github_fingerprint(halfway))
        self.assertNotEqual(github_fingerprint(halfway), github_fingerprint(finished))

    def test_a_progress_line_says_what_moved(self) -> None:
        running = self.status(checks_total=2, checks_pending=2)
        finished = self.status(checks_total=2, checks_pending=0, checks_passed=2)
        failed = self.status(checks_total=2, checks_pending=0, checks_failed=1)

        self.assertEqual(
            github_progress_title(9, finished, running), "PR #9 checks passed"
        )
        self.assertEqual(
            github_progress_title(9, failed, finished), "PR #9 checks failed"
        )
        self.assertEqual(
            github_progress_title(9, running, finished), "PR #9 checks started"
        )
        self.assertEqual(
            github_progress_title(
                9, self.status(review="APPROVED"), self.status(review="")
            ),
            "PR #9 approved",
        )
        self.assertIsNone(github_progress_title(9, running, running))


class MergeStateCarryForwardTest(TestCase):
    @staticmethod
    def status(**overrides: object) -> dict[str, object]:
        return {"number": 31, "state": "OPEN", "merge_state": "CLEAN", **overrides}

    def test_a_transient_unknown_keeps_the_last_known_state(self) -> None:
        carried = carry_forward_merge_state(
            self.status(merge_state="UNKNOWN"), self.status()
        )

        self.assertEqual(carried["merge_state"], "CLEAN")

    def test_a_real_change_is_not_overwritten(self) -> None:
        carried = carry_forward_merge_state(
            self.status(merge_state="BLOCKED"), self.status()
        )

        self.assertEqual(carried["merge_state"], "BLOCKED")

    def test_a_finished_pull_request_carries_nothing_forward(self) -> None:
        carried = carry_forward_merge_state(
            self.status(state="MERGED", merge_state="UNKNOWN"), self.status()
        )

        self.assertEqual(carried["merge_state"], "UNKNOWN")

    def test_the_first_reading_carries_nothing_forward(self) -> None:
        status = self.status(merge_state="UNKNOWN")

        self.assertEqual(carry_forward_merge_state(status, None), status)


class DurationTest(TestCase):
    @staticmethod
    def duration(seconds: float) -> str:
        return format_duration(
            {"started_epoch_ms": 0, "epoch_ms": int(seconds * 1000)}, 0
        )

    def test_long_runs_are_reported_in_hours(self) -> None:
        self.assertEqual(self.duration(9.4), "9.4s")
        self.assertEqual(self.duration(45), "45s")
        self.assertEqual(self.duration(62), "1m02s")
        self.assertEqual(self.duration(59 * 60 + 59), "59m59s")
        self.assertEqual(self.duration(60 * 60), "1h00m")
        self.assertEqual(self.duration(111 * 60 + 28), "1h51m")


class WebPanelKeyTest(TestCase):
    def test_the_panel_address_is_read_from_its_own_output(self) -> None:
        line = "Side Dog panel: http://127.0.0.1:8123/s3cr3t-path/\n"

        self.assertEqual(
            panel_url_from_output(line), "http://127.0.0.1:8123/s3cr3t-path/"
        )
        self.assertEqual(panel_url_from_output("connecting…"), "")

    def test_launching_asks_this_side_dog_to_serve_the_watched_folders(self) -> None:
        with patch("side_dog.cli.subprocess.Popen") as popen:
            popen.return_value.stdout = None
            popen.return_value.poll.return_value = None
            panel = launch_web_panel([Path("/tmp/one"), Path("/tmp/two")])

        command = popen.call_args.args[0]
        self.assertEqual(command[-3:], ["panel", "/tmp/one", "/tmp/two"])
        self.assertEqual(command[: len(side_dog_command())], side_dog_command())
        self.assertTrue(panel.alive())

    def test_a_panel_that_will_not_start_is_reported_as_dead(self) -> None:
        with patch("side_dog.cli.subprocess.Popen", side_effect=OSError):
            panel = launch_web_panel([Path("/tmp/one")])

        self.assertFalse(panel.alive())
        self.assertEqual(panel.url, "")
        panel.stop()

    def test_the_key_is_advertised_in_the_help_and_the_footer(self) -> None:
        help_lines = "\n".join(render_help(80, False, True, root_count=1))
        screen = render(
            [], Path("/tmp/example-project"), width=80, height=12, color=False
        )

        self.assertIn("C       open the browser panel", help_lines)
        self.assertIn("C web", screen)


class BusyMeterTest(TestCase):
    @staticmethod
    def minutes_ago(now_ms: int, minutes: float) -> dict[str, object]:
        return {"epoch_ms": int(now_ms - minutes * 60_000)}

    def test_only_recent_events_count(self) -> None:
        now = 1_000 * 60 * 60
        records = [
            self.minutes_ago(now, 0.5),
            self.minutes_ago(now, 9.5),
            self.minutes_ago(now, 11),
            self.minutes_ago(now, 90),
        ]

        self.assertEqual(activity_count(records, now), 2)
        self.assertEqual(activity_count([], now), 0)

    def test_one_cell_grows_with_activity_and_is_blank_when_quiet(self) -> None:
        self.assertEqual(activity_meter(0, 40), " ")
        self.assertEqual(activity_meter(1, 40), "▁")
        self.assertEqual(activity_meter(40, 40), "█")
        self.assertEqual(activity_meter(20, 40), "▄")

    def test_every_folder_is_measured_against_the_same_busiest_count(self) -> None:
        # The same folder reads differently beside a busier neighbour, which is
        # the point: the meters are comparable with each other.
        self.assertEqual(activity_meter(10, 10), "█")
        self.assertEqual(activity_meter(10, 100), "▁")

    def test_a_folder_alone_is_measured_against_itself(self) -> None:
        self.assertEqual(activity_meter(3, 0), " ")
        self.assertEqual(activity_meter(0, 0), " ")


class LiveSearchTest(TestCase):
    @staticmethod
    def commit(epoch_ms: int, detail: str) -> dict[str, object]:
        return event(epoch_ms, "commit", "Commit created", detail, agent="codex")

    def render(self, search: str) -> list[str]:
        events = [
            self.commit(1_000, "harden RSS XML parsing"),
            self.commit(2_000, "clarify installation"),
            self.commit(3_000, "reject invalid rss feed responses"),
        ]
        lines, _ = render_timeline_activity(
            events, 20, 80, False, 10_000, {}, False, "all", search=search
        )
        return [line for line in lines if "Commit" in line]

    def test_only_matching_lines_survive_and_case_does_not_matter(self) -> None:
        self.assertEqual(len(self.render("rss")), 2)
        self.assertEqual(len(self.render("RSS")), 2)
        self.assertEqual(len(self.render("installation")), 1)
        self.assertEqual(self.render("nothing here"), [])

    def test_an_empty_search_shows_everything(self) -> None:
        self.assertEqual(len(self.render("")), 3)

    def test_a_match_hidden_inside_a_group_is_shown_on_its_own(self) -> None:
        turn = {"turn_id": "turn-1"}
        events = [
            {**event(1_000, "file", "Wrote file", "app.py", agent="codex"), **turn},
            {**event(2_000, "commit", "Commit created", "rss cleanup", agent="codex"),
             **turn},
        ]

        grouped, _ = render_timeline_activity(
            events, 20, 80, False, 10_000, {}, False, "all"
        )
        found, _ = render_timeline_activity(
            events, 20, 80, False, 10_000, {}, False, "all", search="rss"
        )

        # Without a search the two events are one task card.
        self.assertTrue(any("Edit" in line for line in grouped), grouped)
        # With one, only the matching event is shown, and it says why it is here.
        self.assertFalse(any("Edit" in line for line in found), found)
        self.assertTrue(any("rss cleanup" in line for line in found), found)

    def test_every_line_a_search_shows_contains_the_match(self) -> None:
        events = [
            event(1_000 + index, "file", "Wrote file", path, agent="filesystem")
            for index, path in enumerate(
                ["a.py", "b.py", "c.py", "d.py", "e.py", "README.md"]
            )
        ]

        lines, _ = render_timeline_activity(
            events, 20, 80, False, 10_000, {}, False, "all", search="README"
        )

        body = [line for line in lines if "Wrote" in line or "wrote" in line]
        self.assertEqual(len(body), 1)
        self.assertIn("README.md", body[0])

    def test_the_notice_says_what_is_being_searched_for(self) -> None:
        self.assertIn("rss", search_notice("rss"))
        self.assertIn("cleared", search_notice(""))


class RememberedSettingsTest(TestCase):
    def test_the_toggles_survive_a_restart(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {STATE_ENV: directory}):
                self.assertEqual(load_display_settings(), {})

                save_display_settings(
                    newest_first=False, expanded_history=True, event_filter="files"
                )

                self.assertEqual(
                    load_display_settings(),
                    {
                        "newest_first": False,
                        "expanded_history": True,
                        "event_filter": "files",
                    },
                )

    def test_an_unreadable_settings_file_is_ignored(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {STATE_ENV: directory}):
                display_settings_path().parent.mkdir(parents=True, exist_ok=True)
                display_settings_path().write_text("not json at all")

                self.assertEqual(load_display_settings(), {})


class AliveAndQuitTest(TestCase):
    def test_the_header_carries_a_ticking_clock(self) -> None:
        screen = render([], Path("/tmp/example-project"), width=80, height=8, color=False)

        self.assertRegex(screen.splitlines()[0], r"\d\d:\d\d:\d\d")

    def test_quitting_with_q_is_advertised(self) -> None:
        screen = render([], Path("/tmp/example-project"), width=80, height=8, color=False)
        help_lines = "\n".join(render_help(80, False, True, root_count=1))

        self.assertIn("q quit", screen)
        self.assertIn("q       quit Side Dog", help_lines)

    def test_a_folder_name_is_searchable_in_every_view(self) -> None:
        record = {
            **event(1_000, "commit", "Commit created", "unrelated text", agent="codex"),
            SOURCE_KEY: "/Users/someone/src/note-highway",
        }

        self.assertTrue(event_matches_search(record, "note-highway"))
        self.assertFalse(event_matches_search(record, "src"))

    def test_typed_text_that_is_not_ascii_still_reaches_the_search(self) -> None:
        search, pending = "", b""
        for byte in "café".encode():
            search, pending = append_search_byte(search, pending, bytes([byte]))

        self.assertEqual(search, "café")
        self.assertEqual(pending, b"")

    def test_a_half_typed_character_waits_for_the_rest(self) -> None:
        search, pending = append_search_byte("", b"", b"\xc3")

        self.assertEqual(search, "")
        self.assertEqual(pending, b"\xc3")

    def test_bytes_that_never_decode_are_dropped(self) -> None:
        search, pending = "", b""
        for _ in range(4):
            search, pending = append_search_byte(search, pending, b"\xff")

        self.assertEqual(search, "")
        self.assertEqual(pending, b"")

    def test_a_long_search_cannot_widen_the_header(self) -> None:
        screen = render(
            [event(1_000, "commit", "Commit created", "x", agent="codex")],
            Path("/tmp/example-project"),
            width=60,
            height=10,
            color=False,
            search="z" * 200,
        )

        self.assertTrue(all(len(line) <= 60 for line in screen.splitlines()), screen)

    def test_a_cropped_line_still_shows_what_was_searched_for(self) -> None:
        long = "changed · lead_monitor/web/deeply/nested/path/to/README.md"

        self.assertIn("README", crop_to_match(long, 30, "README"))
        self.assertEqual(crop_to_match("changed · README.md", 30, "README"),
                         "changed · README.md")
        self.assertEqual(crop_to_match(long, 30, ""), crop(long, 30))
        self.assertLessEqual(len(crop_to_match(long, 30, "README")), 30)


class ClaudeSessionRegistryTest(TestCase):
    @staticmethod
    def session(directory: Path, pid: int, **overrides: object) -> None:
        sessions = directory / ".claude" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        record = {
            "pid": pid,
            "sessionId": f"11111111-2222-3333-4444-{pid:012d}",
            "cwd": overrides.pop("cwd", "/tmp"),
            "entrypoint": "claude-desktop",
            "kind": "interactive",
            **overrides,
        }
        (sessions / f"{pid}.json").write_text(json.dumps(record))

    def test_a_session_that_died_without_tidying_up_is_ignored(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            self.session(home, os.getpid())
            self.session(home, 999_999_999)
            with patch.dict(os.environ, {"HOME": os.fspath(home)}):
                live = claude_session_registry()

        self.assertEqual([record["pid"] for record in live], [os.getpid()])

    def test_only_sessions_working_in_this_folder_become_identities(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            watched = home / "project"
            watched.mkdir()
            other = home / "elsewhere"
            other.mkdir()
            self.session(home, os.getpid(), cwd=os.fspath(watched))
            self.session(home, os.getpid() + 1, cwd=os.fspath(other))
            with patch.dict(os.environ, {"HOME": os.fspath(home)}):
                with patch("side_dog.cli.process_is_alive", return_value=True):
                    identities = claude_identities(watched.resolve())

        self.assertEqual(len(identities), 1)
        identity = next(iter(identities.values()))
        self.assertEqual(identity["agent"], "claude-code")
        self.assertEqual(identity["working_root"], os.fspath(watched.resolve()))

    def test_the_label_names_the_surface_the_session_runs_in(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            watched = home / "project"
            watched.mkdir()
            for offset, entrypoint in enumerate(("cli", "claude-desktop", "claude-vscode")):
                self.session(
                    home,
                    os.getpid() + offset,
                    cwd=os.fspath(watched),
                    entrypoint=entrypoint,
                )
            with patch.dict(os.environ, {"HOME": os.fspath(home)}):
                with patch("side_dog.cli.process_is_alive", return_value=True):
                    labels = {
                        identity["label"]
                        for identity in claude_identities(watched.resolve()).values()
                    }

        self.assertEqual(labels, {"terminal", "desktop", "VS Code"})

    def test_herdr_wins_where_both_sources_see_one_session(self) -> None:
        shared = {"sid": {"agent": "claude-code", "label": "from herdr"}}
        with patch("side_dog.cli.claude_identities", return_value={
            "sid": {"agent": "claude-code", "label": "from the registry"}
        }):
            with patch("side_dog.cli.load_herdr_identities", return_value=shared):
                merged = load_agent_identities(Path("/tmp"))

        self.assertEqual(merged["sid"]["label"], "from herdr")
