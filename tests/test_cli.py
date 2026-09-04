import hashlib
import json
import os
import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    ANSI,
    ANSI_ESCAPE,
    QuitConfirmation,
    OpenCodeStream,
    WatchRootState,
    STATE_ENV,
    _managed_task_stage_key,
    _poll_opencode_part,
    root_color,
    classify_commands,
    command_program,
    display_detail,
    display_root,
    display_title,
    emit_tool_event,
    event_style,
    events_path,
    format_duration,
    folder_discovery_mode,
    github_event,
    github_progress_title,
    is_definitive_no_pr,
    is_side_dog_hook_command,
    latest_events,
    normalized_tool_events,
    render,
    SOURCE_COLOR_INDEX,
    CODEX_LISTING_CACHE,
    activity_count,
    restart_side_dog,
    active_agent_identities,
    append_event_once,
    codex_session_listing,
    claude_identities,
    claude_session_registry,
    load_agent_identities,
    native_index_path,
    crop,
    crop_to_match,
    activity_meter,
    append_search_byte,
    event_matches_search,
    display_settings_path,
    expanded_header_for_key,
    expanded_header_notice,
    expanded_watch_location_lines,
    filesystem_activity_for_key,
    filesystem_activity_notice,
    filesystem_activity_action,
    idle_agents_for_key,
    idle_agents_notice,
    load_display_settings,
    save_filesystem_activity_setting,
    save_display_settings,
    search_notice,
    launch_web_panel,
    panel_url_from_output,
    render_github_banner,
    render_agent_roster,
    render_root_column,
    render_footer,
    status_bar,
    status_scope_label,
    side_dog_command,
    terminal_cell_width,
    render_help,
    render_milestone_card,
    render_quit_confirmation,
    read_terminal_key,
    render_timeline_activity,
    timeline_view_hint,
    shell_command_is_compound,
    task_state,
    truncate_activity_unit,
)
from side_dog.model import (
    SOURCE_KEY,
    SOURCE_LABEL,
    actor_label,
    carry_forward_merge_state,
    coalesce_operations,
    display_conventional_subject,
    github_fingerprint,
    pipeline_stages,
)


class PrivacyPersistenceBoundaryTest(TestCase):
    def test_rejected_native_event_leaves_no_raw_bytes_in_local_state(self) -> None:
        canary = "SIDE_DOG_PRIVATE_CANARY_72"
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "repo").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                rejected = {
                    "agent": "codex",
                    "session_id": "session-72",
                    "source_event_id": f"codex:session-72:{canary}" + "x" * 50_000,
                    "kind": "file",
                    "status": "success",
                    "title": "Wrote file",
                    "detail": "safe.py",
                    "prompt": canary,
                    "response": canary,
                    "output": canary,
                    "diff": canary,
                    "patch": canary,
                    "command": canary,
                }
                self.assertTrue(append_event_once(root, rejected))
                self.assertFalse(append_event_once(root, rejected))
                records = latest_events(events_path(root), root=root)
                persisted = b"".join(
                    path.read_bytes() for path in state.rglob("*") if path.is_file()
                )
                native_index_exists = native_index_path(root).exists()

            self.assertNotIn(canary.encode(), persisted)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["title"], "Agent activity omitted")
            self.assertEqual(records[0]["detail"], "unexpected_field")
            self.assertTrue(native_index_exists)

    def test_valid_native_event_is_deduped_after_validation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            state = Path(directory) / "state"
            event = {
                "agent": "codex",
                "session_id": "session-72",
                "source_event_id": "codex:session-72:call-1",
                "kind": "file",
                "status": "success",
                "title": "Wrote file",
                "detail": os.fspath(root / "safe.py"),
            }
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                self.assertTrue(append_event_once(root, event))
                self.assertFalse(append_event_once(root, event))
                records = latest_events(events_path(root), root=root)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["detail"], "safe.py")

    def test_file_path_outside_the_root_becomes_a_fixed_diagnostic(self) -> None:
        canary = "outside-private-canary.py"
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            state = Path(directory) / "state"
            outside = Path(directory) / canary
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                append_event_once(
                    root,
                    {
                        "agent": "pi",
                        "source_event_id": "pi:session-72:call-2",
                        "kind": "file",
                        "status": "success",
                        "title": "Wrote file",
                        "detail": os.fspath(outside),
                    },
                )
                records = latest_events(events_path(root), root=root)
                persisted = events_path(root).read_text()

            self.assertEqual(records[0]["title"], "Agent activity omitted")
            self.assertEqual(records[0]["detail"], "outside_project")
            self.assertNotIn(canary, persisted)

    def test_claude_outside_edit_path_never_persists_its_filename(self) -> None:
        canary = "SIDE_DOG_PRIVATE_OUTSIDE_EDIT_81.py"
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "repo").resolve()
            root.mkdir()
            outside = (Path(directory) / "private" / canary).resolve()
            state = Path(directory) / "state"
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                emit_tool_event(
                    {
                        "agent": "claude-code",
                        "session_id": "session-81",
                        "tool_use_id": "outside-edit-81",
                        "tool_name": "Write",
                        "tool_input": {"file_path": os.fspath(outside)},
                        "cwd": os.fspath(root),
                    },
                    root,
                    status="success",
                )
                records = latest_events(events_path(root), root=root)
                persisted = b"".join(
                    path.read_bytes() for path in state.rglob("*") if path.is_file()
                )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["agent"], "claude-code")
        self.assertEqual(records[0]["title"], "Agent activity omitted")
        self.assertEqual(records[0]["detail"], "outside_project")
        self.assertNotIn(canary.encode(), persisted)


class StatusBarTest(TestCase):
    def test_status_bar_shows_version_scope_working_count_and_clock(self) -> None:
        line = status_bar("1.0.0", "all 8 folders", 5, 80, "10:33:58")

        self.assertTrue(line.startswith("SIDE DOG v1.0.0 · all 8 folders · 5 working"))
        self.assertTrue(line.endswith("10:33:58"))
        self.assertEqual(terminal_cell_width(line), 80)

    def test_status_bar_crops_working_then_scope_then_version(self) -> None:
        without_working = status_bar("1.0.0", "all 8 folders", 5, 42, "10:33:58")
        without_scope = status_bar("1.0.0", "all 8 folders", 5, 28, "10:33:58")
        without_version = status_bar("1.0.0", "all 8 folders", 5, 17, "10:33:58")

        self.assertIn("all 8 folders", without_working)
        self.assertNotIn("working", without_working)
        self.assertIn("v1.0.0", without_scope)
        self.assertNotIn("folders", without_scope)
        self.assertEqual(without_version, "SIDE DOG 10:33:58")

    def test_scope_wording_covers_all_subset_and_one_folder(self) -> None:
        root = Path("/tmp/side-dog")

        self.assertEqual(status_scope_label(root, 8), "all 8 folders")
        self.assertEqual(
            status_scope_label(root, 8, shown_root_count=3), "3 of 8 folders"
        )
        self.assertEqual(status_scope_label(root, 8, "PR #115"), "side-dog")
        self.assertEqual(status_scope_label(root, 1), "side-dog")


class RenderHelpTest(TestCase):
    def test_compact_header_hides_watching_and_mode_details_by_default(self) -> None:
        root = Path.cwd()
        mode = folder_discovery_mode(
            explicit_roots=True, follow_herdr=True, require_herdr=True
        )

        screen = render(
            [], root, width=80, height=24, color=False, discovery_mode=mode
        )

        self.assertNotIn("Watching ", screen)
        self.assertNotIn("Mode: ", screen)
        self.assertEqual(
            display_root(Path("/tmp/example-project")), "/tmp/example-project"
        )

    def test_expanded_header_identifies_folder_and_discovery_mode(self) -> None:
        mode = folder_discovery_mode(
            explicit_roots=True, follow_herdr=True, require_herdr=True
        )
        screen = render(
            [],
            Path("/tmp"),
            width=80,
            height=24,
            color=False,
            discovery_mode=mode,
            expanded_header=True,
        )
        narrow = render(
            [],
            Path("/tmp"),
            width=32,
            height=24,
            color=False,
            discovery_mode=mode,
            expanded_header=True,
        )

        self.assertIn(" Watching /tmp", screen)
        self.assertIn("Mode: explicit folders + Herdr", screen)
        self.assertIn("Mode: explicit + Herdr", narrow)

    def test_expanded_multi_root_header_lists_every_folder_location(self) -> None:
        arguments = {
            "records": [],
            "root": Path("/Users/example/worktrees/alpha-main"),
            "width": 100,
            "height": 24,
            "color": False,
            "root_count": 2,
            "repository_context": "/Users/example/src/alpha +1",
            "roster_roots": (
                {"key": "/Users/example/worktrees/alpha-main"},
                {"key": "/Users/example/worktrees/beta-review"},
            ),
        }

        compact = render(**arguments)
        expanded = render(**arguments, expanded_header=True)

        self.assertNotIn("/Users/example/worktrees/alpha-main", compact)
        self.assertNotIn("/Users/example/worktrees/beta-review", compact)
        self.assertIn("Folders /Users/example/worktrees/alpha-main", expanded)
        self.assertIn("/Users/example/worktrees/beta-review", expanded)
        self.assertNotIn("/Users/example/src/alpha +1", expanded)

    def test_expanded_focused_header_uses_the_focused_worktree_path(self) -> None:
        arguments = {
            "records": [],
            "root": Path("/Users/example/worktrees/beta-review"),
            "width": 100,
            "height": 24,
            "color": False,
            "root_count": 2,
            "focused_root_label": "PR #9",
            "repository_context": "/Users/example/src/beta",
            "roster_roots": ({"key": "/Users/example/worktrees/beta-review"},),
        }

        compact = render(**arguments)
        expanded = render(**arguments, expanded_header=True)

        self.assertNotIn("/Users/example/worktrees/beta-review", compact)
        self.assertIn(
            "Folder  /Users/example/worktrees/beta-review",
            expanded,
        )
        self.assertNotIn("/Users/example/src/beta", expanded)

    def test_expanded_folder_locations_crop_from_the_left_in_a_narrow_pane(
        self,
    ) -> None:
        lines = expanded_watch_location_lines(
            (
                "/Users/example/very/long/worktrees/alpha-main",
                "/Users/example/very/long/worktrees/beta-review",
            ),
            32,
        )

        self.assertEqual(len(lines), 2)
        self.assertTrue(all(terminal_cell_width(line) <= 32 for line in lines))
        self.assertTrue(lines[0].endswith("/worktrees/alpha-main"), lines[0])
        self.assertTrue(lines[1].endswith("/worktrees/beta-review"), lines[1])

    def test_uppercase_E_toggles_only_header_expansion(self) -> None:
        self.assertTrue(expanded_header_for_key(b"E", False))
        self.assertFalse(expanded_header_for_key(b"E", True))
        self.assertFalse(expanded_header_for_key(b"e", False))
        self.assertIn("visible", expanded_header_notice(True))
        self.assertIn("hidden", expanded_header_notice(False))

    def test_i_toggles_only_idle_agents(self) -> None:
        self.assertTrue(idle_agents_for_key(b"i", False))
        self.assertFalse(idle_agents_for_key(b"i", True))
        self.assertFalse(idle_agents_for_key(b"I", False))
        self.assertIn("showing every session", idle_agents_notice(True))
        self.assertIn("summary line", idle_agents_notice(False))

    def test_three_folder_roster_snapshot_groups_and_folds_eight_agents(self) -> None:
        now_ms = 2_000_000_000_000
        roots = [
            {
                "key": "/tmp/cocos-story",
                "name": "cocos-story",
                "label": "develop",
                "color_index": 0,
                "git": {"branch": "develop"},
                "latest_epoch": now_ms,
            },
            {
                "key": "/tmp/side-dog",
                "name": "side-dog",
                "label": "PR #53",
                "color_index": 1,
                "github": {
                    "number": 53,
                    "state": "OPEN",
                    "ci": "CI 5/6 blocked",
                },
                "latest_epoch": now_ms - 60_000,
            },
            {
                "key": "/tmp/tony-the-tiger",
                "name": "tony-the-tiger",
                "label": "main",
                "color_index": 2,
                "git": {"branch": "main"},
                "latest_epoch": now_ms - 120_000,
            },
        ]

        def identity(
            number: int,
            root: str,
            agent: str,
            label: str,
            model: str,
            effort: str,
            status: str,
            age_minutes: int,
        ) -> dict[str, object]:
            return {
                "agent": agent,
                "pane_id": f"p{number}",
                "working_root": root,
                "label": label,
                "model": model,
                "effort": effort,
                "status": status,
                "epoch_ms": now_ms - age_minutes * 60_000,
                SOURCE_KEY: root,
                SOURCE_LABEL: root.rsplit("/", 1)[-1],
                SOURCE_COLOR_INDEX: str(number % 3),
            }

        identities = {
            "a": identity(
                1,
                "/tmp/cocos-story",
                "claude-code",
                "CI fleet capacity",
                "claude-fable-5-1",
                "medium",
                "working",
                2,
            ),
            "b": identity(
                2,
                "/tmp/cocos-story",
                "codex",
                "cocos-story",
                "gpt-5.6-sol",
                "high",
                "working",
                5,
            ),
            "c": identity(
                3,
                "/tmp/side-dog",
                "codex",
                "Codex Desktop",
                "gpt-5.6-luna",
                "max",
                "working",
                1,
            ),
            "d": identity(
                4,
                "/tmp/side-dog",
                "claude-code",
                "Older review",
                "claude-fable-5-1",
                "medium",
                "idle",
                10,
            ),
            "e": identity(
                5, "/tmp/side-dog", "pi", "Docs", "gemini-3", "high", "idle", 20
            ),
            "f": identity(
                6,
                "/tmp/tony-the-tiger",
                "codex",
                "Campaign Help",
                "gpt-5.6-sol",
                "high",
                "working",
                3,
            ),
            "g": identity(
                7,
                "/tmp/tony-the-tiger",
                "cline",
                "Inbox",
                "claude-sonnet-4",
                "low",
                "idle",
                30,
            ),
            "h": identity(
                8,
                "/tmp/tony-the-tiger",
                "opencode",
                "Leads",
                "gpt-5",
                "medium",
                "idle",
                40,
            ),
        }
        with patch("side_dog.cli.time.time", return_value=now_ms / 1000):
            roster = "\n".join(
                render_agent_roster(identities, [], 80, False, roots=roots)
            )

        self.assertEqual(
            roster,
            "\n".join(
                (
                    "│ cocos-story  develop                                                 2 working",
                    "│   Claude     CI fleet capacity           fable-5-1/med      ● working   2m",
                    "│   Codex      cocos-story                 5.6-sol/high       ● working   5m",
                    "│ side-dog  PR #53 · CI 5/6 blocked                           1 working · 2 idle",
                    "│   Codex      Codex Desktop               5.6-luna/max       ● working   1m",
                    "│ tony-the-tiger  main                                        1 working · 2 idle",
                    "│   Codex      Campaign Help               5.6-sol/high       ● working   3m",
                    "  4 idle in side-dog:2, tony-the-tiger:2                               i to show",
                )
            ),
        )
        self.assertNotIn("/tmp/", roster)

        expanded = render_agent_roster(
            identities, [], 80, False, show_idle_agents=True, roots=roots
        )
        self.assertLess(
            next(
                index for index, line in enumerate(expanded) if "Codex Desktop" in line
            ),
            next(
                index for index, line in enumerate(expanded) if "Older review" in line
            ),
        )
        self.assertNotIn("i to show", "\n".join(expanded))

    def test_roster_heading_counts_only_running_statuses_as_working(self) -> None:
        root = "/tmp/side-dog"
        statuses = ("working", "completed", "blocked", "unexpected", "idle")
        identities = {
            status: {
                "agent": "codex",
                "pane_id": status,
                "label": status,
                "working_root": root,
                "status": status,
                SOURCE_KEY: root,
            }
            for status in statuses
        }

        roster = render_agent_roster(
            identities,
            [],
            80,
            False,
            roots=({"key": root, "name": "side-dog"},),
        )

        self.assertIn("1 working · 1 idle", roster[0])
        self.assertIn("✓ completed", "\n".join(roster))
        self.assertIn("× blocked", "\n".join(roster))
        self.assertIn("? unknown", "\n".join(roster))

    def test_root_column_heading_does_not_count_terminal_statuses_as_working(
        self,
    ) -> None:
        state = WatchRootState(
            root=Path("/tmp/side-dog"),
            path=Path("/tmp/side-dog/events.jsonl"),
            records=deque(maxlen=500),
            position=0,
            known_files={},
            git_status=None,
            last_hook_writes={},
            identities={},
            github_status=None,
            last_github_fingerprint=None,
            last_scan=0.0,
            last_git_refresh=0.0,
            last_herdr_refresh=0.0,
            last_github_refresh=0.0,
        )
        identities = {
            status: {
                "agent": "codex",
                "pane_id": status,
                "label": status,
                "status": status,
            }
            for status in ("done", "blocked", "unexpected")
        }

        lines = render_root_column(
            state,
            "side-dog",
            [],
            identities,
            0,
            80,
            10,
            False,
            session_filter=None,
            expanded_history=False,
            event_filter="all",
            paused=False,
            new_event_count=0,
            newest_first=True,
        )

        self.assertIn("0 working", lines[0])
        self.assertNotIn("3 working", lines[0])

    def test_roster_keeps_pane_only_activity_ages_and_order_independent(self) -> None:
        now_ms = 2_000_000_000_000
        root = {
            "key": "/tmp/side-dog",
            "name": "side-dog",
            "color_index": 0,
            "git": {"branch": "main"},
        }
        identities = {
            "older": {
                "agent": "codex",
                "pane_id": "pane-older",
                "working_root": root["key"],
                "label": "Older pane",
                "status": "working",
                SOURCE_KEY: root["key"],
            },
            "newer": {
                "agent": "codex",
                "pane_id": "pane-newer",
                "working_root": root["key"],
                "label": "Newer pane",
                "status": "working",
                SOURCE_KEY: root["key"],
            },
        }
        records = [
            {
                "agent": "codex",
                "herdr_pane_id": "pane-older",
                "epoch_ms": now_ms - 10 * 60_000,
                SOURCE_KEY: root["key"],
            },
            {
                "agent": "codex",
                "herdr_pane_id": "pane-newer",
                "epoch_ms": now_ms - 60_000,
                SOURCE_KEY: root["key"],
            },
        ]

        with patch("side_dog.cli.time.time", return_value=now_ms / 1000):
            roster = render_agent_roster(
                identities, records, 80, False, roots=(root,)
            )

        newer = next(line for line in roster if "Newer pane" in line)
        older = next(line for line in roster if "Older pane" in line)
        self.assertLess(roster.index(newer), roster.index(older))
        self.assertTrue(newer.endswith("1m"), newer)
        self.assertTrue(older.endswith("10m"), older)

    def test_roster_columns_degrade_age_then_model_then_task(self) -> None:
        now_ms = 2_000_000_000_000
        root = {
            "key": "/tmp/cocos-story",
            "name": "cocos-story",
            "color_index": 0,
            "git": {"branch": "develop"},
        }
        identity = {
            "agent": "claude-code",
            "pane_id": "p1",
            "working_root": root["key"],
            "label": "CI fleet capacity",
            "model": "claude-fable-5-1",
            "effort": "medium",
            "status": "working",
            "epoch_ms": now_ms - 120_000,
            SOURCE_KEY: root["key"],
        }
        with patch("side_dog.cli.time.time", return_value=now_ms / 1000):
            wide = "\n".join(
                render_agent_roster({"a": identity}, [], 80, False, roots=(root,))
            )
            no_age = "\n".join(
                render_agent_roster({"a": identity}, [], 77, False, roots=(root,))
            )
            no_model = "\n".join(
                render_agent_roster({"a": identity}, [], 68, False, roots=(root,))
            )
            status_only = "\n".join(
                render_agent_roster({"a": identity}, [], 48, False, roots=(root,))
            )

        self.assertIn("fable-5-1/med", wide)
        self.assertIn("2m", wide)
        self.assertIn("fable-5-1/med", no_age)
        self.assertNotIn("2m", no_age)
        self.assertIn("CI fleet capacity", no_model)
        self.assertNotIn("fable-5-1/med", no_model)
        self.assertNotIn("CI fleet capacity", status_only)
        self.assertIn("● working", status_only)

    def test_compact_header_keeps_a_missing_folder_warning_visible(self) -> None:
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "deleted-project"
            mode = folder_discovery_mode(
                explicit_roots=True, follow_herdr=False, require_herdr=False
            )

            screen = render(
                [],
                missing,
                width=80,
                height=12,
                color=False,
                discovery_mode=mode,
            )

        self.assertIn("Watching folder is gone ·", screen)
        self.assertNotIn("Mode: explicit folder selection", screen)

    def test_all_discovery_policies_are_distinct(self) -> None:
        modes = (
            folder_discovery_mode(
                explicit_roots=True, follow_herdr=False, require_herdr=False
            ),
            folder_discovery_mode(
                explicit_roots=False, follow_herdr=False, require_herdr=False
            ),
            folder_discovery_mode(
                explicit_roots=False, follow_herdr=True, require_herdr=False
            ),
            folder_discovery_mode(
                explicit_roots=False, follow_herdr=True, require_herdr=True
            ),
            folder_discovery_mode(
                explicit_roots=True, follow_herdr=True, require_herdr=True
            ),
            folder_discovery_mode(
                explicit_roots=False,
                follow_herdr=False,
                require_herdr=False,
                automatic=False,
            ),
        )

        self.assertEqual(len({mode.key for mode in modes}), 6)
        self.assertEqual(len({mode.label for mode in modes}), 6)

    def test_help_shows_controls_and_current_commit(self) -> None:
        screen = render(
            [],
            Path("/tmp/example-project"),
            width=80,
            # Tall enough to hold the whole help card, folders note included.
            height=35,
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
        self.assertIn("E       show folder, mode, and usage details", screen)
        self.assertIn("Divider: r newest first · e compact", screen)
        self.assertIn("e       expand detail", screen)
        self.assertIn("r       put oldest activity first", screen)
        self.assertNotIn("Folder colors", screen)
        self.assertIn("Color: blue navigation · purple identity", screen)
        self.assertIn("red failed · neutral idle/unknown", screen)
        self.assertIn('an agent works in ("found")', screen)
        self.assertIn("watch @NAME opens a saved space", screen)
        self.assertIn("? unknown", screen)
        self.assertIn("A task card links one agent turn", screen)
        self.assertIn("Codex", screen)
        self.assertIn("example/high", screen)
        self.assertIn("● working", screen)
        self.assertIn("example-project  feature/sidebar", screen)

        help_text = "\n".join(render_help(100, False, root_count=1))
        self.assertIn("API estimate = public list prices applied to local logs", help_text)
        self.assertIn("not a subscription bill", help_text)
        self.assertIn("tracked lifetime use matched shown roots", help_text)
        self.assertIn("current 5h window is machine-wide", help_text)

    def test_event_status_colors_override_event_kind_colors(self) -> None:
        for status, glyph, color in (
            ("success", "✎", ANSI["green"]),
            ("running", "…", ANSI["yellow"]),
            ("warning", "!", ANSI["yellow"]),
            ("failed", "×", ANSI["red"]),
            ("unknown", "?", ANSI["dim"]),
        ):
            with self.subTest(status=status):
                self.assertEqual(
                    event_style({"kind": "file", "status": status}),
                    (glyph, color),
                )

    def test_help_explains_root_colors_only_when_roots_are_shared(self) -> None:
        one_root = "\n".join(render_help(80, False, True, root_count=1))
        many_roots = "\n".join(render_help(80, False, True, root_count=3))

        self.assertNotIn("Folder colors", one_root)
        self.assertIn("Folder colors", many_roots)
        self.assertIn("source badge", many_roots)
        self.assertIn("column title", many_roots)
        self.assertNotIn("name in the header", many_roots)
        self.assertIn("all share one color", many_roots)

    def test_removed_file_label_is_compact(self) -> None:
        self.assertEqual(display_title({"title": "File removed"}), "removed")

    def test_help_explains_active_oldest_first_order(self) -> None:
        screen = render(
            [],
            Path("/tmp/example-project"),
            width=80,
            height=26,
            color=False,
            show_help=True,
            newest_first=False,
        )

        self.assertIn("Newest activity is at the bottom", screen)
        self.assertNotIn("Newest activity is at the top", screen)

    def test_help_names_the_action_each_contextual_key_will_take(self) -> None:
        help_lines = "\n".join(
            render_help(
                100,
                False,
                newest_first=False,
                root_count=3,
                expanded_history=True,
                event_filter="files",
                paused=True,
                focused_root_label="api",
                expanded_header=True,
            )
        )

        self.assertIn("E       hide folder, mode, and usage details", help_lines)
        self.assertIn("Divider: r oldest first · e expanded · f files", help_lines)
        self.assertIn("e       compact detail", help_lines)
        self.assertIn("f       show all (now files)", help_lines)
        self.assertIn("p       resume display", help_lines)
        self.assertIn("r       put newest activity first", help_lines)
        self.assertIn("a       show all folders again", help_lines)

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

    def test_command_titles_and_bodies_are_not_recorded(self) -> None:
        canary = "SIDE_DOG_PRIVATE_COMMAND_72"
        issue = classify_commands(
            f"gh issue create --title '{canary}' --body '{canary}'"
        )
        pull_request = classify_commands(
            f"gh pr create --title='{canary}' --body '{canary}'"
        )

        self.assertEqual(issue, [("issue", "Opening issue", "gh issue create")])
        self.assertEqual(
            pull_request,
            [("pr", "Opening pull request", "gh pr create")],
        )
        self.assertNotIn(canary, repr(issue + pull_request))

    def test_native_bash_producers_do_not_record_gh_title_arguments(self) -> None:
        canary = "SIDE_DOG_PRIVATE_NATIVE_TITLE_72"
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "repo").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            command = f"gh pr create --title '{canary}' --body '{canary}'"
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                emit_tool_event(
                    {
                        "agent": "claude-code",
                        "session_id": "session-72",
                        "tool_use_id": "call-72",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                    root,
                    status="success",
                )
                stream = OpenCodeStream(
                    session_id="opencode-session-72",
                    db_path=Path(directory) / "unused.db",
                    position=0,
                    agent_root=os.fspath(root),
                )
                self.assertEqual(
                    _poll_opencode_part(
                        root,
                        stream,
                        "opencode-call-72",
                        {
                            "type": "tool",
                            "tool": "bash",
                            "state": {
                                "status": "completed",
                                "input": {"command": command},
                                "metadata": {"exit": 0},
                            },
                        },
                        1_000,
                    ),
                    1,
                )
                records = latest_events(events_path(root), root=root)
                persisted = events_path(root).read_text()

        self.assertEqual(len(records), 2)
        self.assertEqual(
            {record["agent"] for record in records}, {"claude-code", "opencode"}
        )
        self.assertEqual({record["detail"] for record in records}, {"gh pr create"})
        self.assertNotIn(canary, persisted)

    def test_opencode_context_tools_do_not_record_queries(self) -> None:
        canary = "SIDE_DOG_PRIVATE_OPENCODE_QUERY_72"
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "repo").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            stream = OpenCodeStream(
                session_id="opencode-session-72",
                db_path=Path(directory) / "unused.db",
                position=0,
                agent_root=os.fspath(root),
            )
            tools = [
                ("read", {"filePath": os.fspath(root / "src" / "app.py")}),
                ("grep", {"pattern": canary}),
                ("glob", {"pattern": f"**/{canary}*"}),
                ("webfetch", {"url": f"https://example.test/{canary}?q={canary}"}),
                (
                    "todowrite",
                    {"todos": [{"content": canary, "status": "in_progress"}]},
                ),
            ]
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                for index, (tool, tool_input) in enumerate(tools):
                    self.assertEqual(
                        _poll_opencode_part(
                            root,
                            stream,
                            f"context-{index}",
                            {
                                "type": "tool",
                                "tool": tool,
                                "state": {
                                    "status": "completed",
                                    "input": tool_input,
                                },
                            },
                            1_700_000_000_000 + index * 1_000,
                        ),
                        1,
                    )
                records = latest_events(events_path(root), root=root)
                persisted = events_path(root).read_text()

        self.assertEqual(
            [(record["title"], record["detail"]) for record in records],
            [
                ("Read file", "src/app.py"),
                ("Searched code", "code"),
                ("Searched files", "files"),
                ("Fetched web page", "web page"),
                ("Todo updated", "1 task"),
            ],
        )
        self.assertNotIn(canary, persisted)


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


class FooterShortcutTest(TestCase):
    def test_wide_footer_contains_only_core_actions(self) -> None:
        footer = "\n".join(
            render_footer(
                100,
                False,
                root_count=3,
                expanded_history=False,
                paused=False,
            )
        )

        self.assertEqual(
            footer,
            "─ Tab folder · e expand · F show files · p pause · / find · ? help · q quit",
        )
        for removed_hint in ("R reload", "C web", "E header", "r oldest", "f all"):
            self.assertNotIn(removed_hint, footer)

    def test_narrow_footer_wraps_without_losing_core_actions(self) -> None:
        lines = render_footer(
            28,
            False,
            root_count=3,
            expanded_history=False,
            paused=False,
        )
        footer = "\n".join(lines)

        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= 28 for line in lines))
        for action in (
            "Tab folder",
            "e expand",
            "F show files",
            "p pause",
            "/ find",
            "? help",
            "q quit",
        ):
            self.assertIn(action, footer)

    def test_footer_actions_follow_the_current_view_state(self) -> None:
        footer = "\n".join(
            render_footer(
                80,
                False,
                root_count=3,
                expanded_history=True,
                paused=True,
                focused_root_label="api",
            )
        )

        self.assertIn("a all folders", footer)
        self.assertIn("e compact", footer)
        self.assertIn("F show files", footer)
        self.assertIn("p resume", footer)
        self.assertNotIn("Tab folder", footer)

    def test_plain_footer_uses_words_without_ansi_color(self) -> None:
        footer = "\n".join(
            render_footer(
                28,
                False,
                root_count=1,
                expanded_history=False,
                paused=False,
            )
        )

        self.assertNotIn("\x1b[", footer)
        self.assertIn("e expand", footer)
        self.assertIn("p pause", footer)


class FilesystemActivityDisplayTest(TestCase):
    @staticmethod
    def records() -> list[dict[str, object]]:
        return [
            event(1_000, "file", "File changed", "passive.py"),
            event(2_000, "config", "Config changed", "agent.toml", agent="codex"),
        ]

    def test_default_terminal_render_hides_only_passive_filesystem_events(self) -> None:
        hidden = render(
            self.records(),
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
            expanded_history=True,
        )
        visible = render(
            self.records(),
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
            expanded_history=True,
            show_filesystem_activity=True,
        )

        self.assertNotIn("passive.py", hidden)
        self.assertIn("agent.toml", hidden)
        self.assertIn("passive.py", visible)
        self.assertIn("agent.toml", visible)

    def test_files_filter_keeps_agent_attributed_file_events_when_passive_is_hidden(
        self,
    ) -> None:
        screen = render(
            self.records(),
            Path("/tmp/project"),
            width=100,
            height=20,
            color=False,
            expanded_history=True,
            event_filter="files",
        )

        self.assertNotIn("passive.py", screen)
        self.assertIn("agent.toml", screen)

    def test_uppercase_toggle_is_independent_from_the_lowercase_filter(self) -> None:
        self.assertTrue(filesystem_activity_for_key(b"F", False))
        self.assertFalse(filesystem_activity_for_key(b"F", True))
        self.assertFalse(filesystem_activity_for_key(b"f", False))
        self.assertTrue(filesystem_activity_for_key(b"r", True))

    def test_toggle_notices_and_help_use_the_requested_copy(self) -> None:
        self.assertEqual(
            filesystem_activity_notice(True),
            "Filesystem activity visible — source is unattributed",
        )
        self.assertEqual(filesystem_activity_notice(False), "Filesystem activity hidden")
        hidden_help = "\n".join(
            render_help(
                100,
                False,
                True,
                root_count=1,
                show_filesystem_activity=False,
            )
        )
        visible_help = "\n".join(
            render_help(
                100,
                False,
                True,
                root_count=1,
                show_filesystem_activity=True,
            )
        )

        self.assertIn("F       show unattributed filesystem activity", hidden_help)
        self.assertIn("F       hide unattributed filesystem activity", visible_help)
        self.assertEqual(
            filesystem_activity_action(False), "show unattributed filesystem activity"
        )

    def test_browser_preference_save_preserves_the_other_display_toggles(self) -> None:
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {STATE_ENV: directory}):
                save_display_settings(
                    newest_first=False,
                    expanded_history=True,
                    expanded_header=True,
                    event_filter="files",
                    show_filesystem_activity=True,
                )
                save_filesystem_activity_setting(False)

                self.assertEqual(
                    load_display_settings(),
                    {
                        "newest_first": False,
                        "expanded_history": True,
                        "expanded_header": True,
                        "event_filter": "files",
                        "show_filesystem_activity": False,
                    },
                )


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
        show_filesystem_activity: bool = True,
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
            show_filesystem_activity=show_filesystem_activity,
        )
        return "\n".join(lines)

    def test_view_hint_uses_controls_and_omits_the_all_filter(self) -> None:
        self.assertEqual(
            timeline_view_hint(True, False, "all"),
            "r newest first · e compact",
        )
        self.assertEqual(
            timeline_view_hint(False, True, "milestones", 12),
            "r oldest first · e expanded · f milestones · 12 more ↑",
        )

    def test_render_moves_view_state_onto_the_first_day_divider(self) -> None:
        now = int(time.time() * 1000)
        screen = render(
            [event(now, "test", "Tests passed", "unit", agent="codex")],
            Path("/tmp/project"),
            width=100,
            height=12,
            color=False,
            expanded_history=True,
            event_filter="milestones",
        )

        divider = next(line for line in screen.splitlines() if "Today ·" in line)
        self.assertIn("r newest first · e expanded · f milestones", divider)
        self.assertFalse(
            any(line.startswith("┌ newest first") for line in screen.splitlines())
        )

    def test_empty_search_keeps_view_state_on_the_day_divider(self) -> None:
        now = int(time.time() * 1000)
        screen = render(
            [event(now, "test", "Tests passed", "unit", agent="codex")],
            Path("/tmp/project"),
            width=140,
            height=12,
            color=False,
            expanded_history=True,
            paused=True,
            new_event_count=2,
            newest_first=False,
            search="no match",
        )

        divider = next(line for line in screen.splitlines() if "Today ·" in line)
        self.assertIn("p paused · 2 new", divider)
        self.assertIn("r oldest first · e expanded", divider)
        self.assertIn("/ no match", divider)
        self.assertNotIn("waiting for coding-agent activity", screen)

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

    def test_consecutive_context_reads_from_one_session_collapse(self) -> None:
        screen = self.render_lines(
            [
                event(
                    1_000,
                    "search",
                    "Read file",
                    "panel.py",
                    agent="opencode",
                    session_id="session-a",
                ),
                event(
                    61_000,
                    "search",
                    "Read file",
                    "panel.py",
                    agent="opencode",
                    session_id="session-a",
                ),
            ],
            expanded=True,
            now_ms=61_000,
        )

        self.assertEqual(screen.count("Read file"), 1)
        self.assertIn("×2", screen)
        self.assertIn("→", screen)

    def test_context_reads_from_different_sessions_remain_separate(self) -> None:
        screen = self.render_lines(
            [
                event(
                    1_000,
                    "search",
                    "Read file",
                    "panel.py",
                    agent="opencode",
                    session_id="session-a",
                ),
                event(
                    2_000,
                    "search",
                    "Read file",
                    "panel.py",
                    agent="opencode",
                    session_id="session-b",
                ),
            ],
            expanded=True,
        )

        self.assertEqual(screen.count("Read file"), 2)
        self.assertNotIn("×2", screen)

    def test_context_reads_outside_short_window_remain_separate(self) -> None:
        screen = self.render_lines(
            [
                event(
                    1_000,
                    "search",
                    "Read file",
                    "panel.py",
                    agent="opencode",
                    session_id="session-a",
                ),
                event(
                    122_000,
                    "search",
                    "Read file",
                    "panel.py",
                    agent="opencode",
                    session_id="session-a",
                ),
            ],
            expanded=True,
            now_ms=122_000,
        )

        self.assertEqual(screen.count("Read file"), 2)
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
            show_filesystem_activity=True,
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
            show_filesystem_activity=True,
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
            show_filesystem_activity=True,
        )

        self.assertIn("1 more ↑", screen)
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
            show_filesystem_activity=True,
        )

        self.assertEqual(events, original)
        self.assertIn("p paused · 2 new · r oldest first · e compact", screen)
        self.assertNotIn("· all", screen)
        self.assertNotIn("r newest", screen)
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
            show_filesystem_activity=True,
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
            expanded=False,
        )

        self.assertIn(
            "Edit ×1 → Tests ✓ → Commit abc1234 → Push ✓ → PR ✓",
            screen,
        )
        self.assertIn("Agent task · ✓ completed · 5 events · 4.0s", screen)
        self.assertIn("│   └─ Edit ×1", screen)

    def test_expanded_task_indents_children_in_chronological_order(self) -> None:
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
            ],
            expanded=True,
        )

        self.assertIn("Agent task · ✓ completed · 3 events", screen)
        self.assertEqual(screen.count("│   ├─"), 2)
        self.assertEqual(screen.count("│   └─"), 1)
        self.assertIn("✎ Codex · wrote · app.py", screen)
        self.assertIn("✓ Codex · passed · unittest", screen)
        self.assertIn("◆ Codex · committed · abc1234 add feature", screen)
        self.assertLess(screen.index("app.py"), screen.index("unittest"))
        self.assertLess(screen.index("unittest"), screen.index("abc1234"))

    def test_task_status_vocabulary_covers_each_visible_state(self) -> None:
        cases = (
            (
                [event(1_000, "test", "Tests passed", "unit")],
                None,
                ("success", "✓", "completed"),
            ),
            (
                [
                    event(
                        1_000,
                        "test",
                        "Running tests",
                        "unit",
                        status="running",
                    )
                ],
                None,
                ("running", "…", "running"),
            ),
            (
                [
                    event(
                        1_000,
                        "test",
                        "Tests failed",
                        "unit",
                        status="failed",
                    )
                ],
                None,
                ("failure", "×", "failed"),
            ),
            (
                [
                    event(
                        1_000,
                        "test",
                        "Tests finished",
                        "unit",
                        status="unknown",
                    )
                ],
                None,
                ("unknown", "?", "unknown"),
            ),
            (
                [event(1_000, "pr", "PR updated", "blocked")],
                {"merge_state": "BLOCKED"},
                ("failure", "×", "blocked"),
            ),
        )

        for events, github_status, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(task_state(events, github_status), expected)

    def test_task_completion_supersedes_its_matching_running_event(self) -> None:
        events = [
            event(
                1_000,
                "test",
                "Running tests",
                "unit",
                status="running",
                operation_id="tests",
            ),
            event(
                2_000,
                "test",
                "Tests passed",
                "unit",
                operation_id="tests",
            ),
        ]

        self.assertEqual(task_state(events), ("success", "✓", "completed"))

    def test_equal_time_task_outcome_uses_append_order_everywhere(self) -> None:
        shared = {"turn_id": "turn", "agent": "codex", "operation_id": "tests"}
        events = [
            event(
                1_000,
                "test",
                "Running tests",
                "unit",
                status="running",
                _append_ordinal=1,
                **shared,
            ),
            event(
                1_000,
                "test",
                "Tests passed",
                "unit",
                _append_ordinal=2,
                **shared,
            ),
        ]

        screen = self.render_lines(events, expanded=False)

        self.assertIn("Agent task · ✓ completed", screen)
        self.assertIn("Tests ×2 ✓", screen)
        self.assertNotIn("Tests ×2 …", screen)

    def test_latest_retry_outcome_controls_the_task_state(self) -> None:
        events = [
            event(
                1_000,
                "test",
                "Tests failed",
                "unit",
                status="failed",
                operation_id="tests-first",
            ),
            event(
                2_000,
                "test",
                "Tests passed",
                "unit",
                operation_id="tests-retry",
            ),
        ]

        self.assertEqual(task_state(events), ("success", "✓", "completed"))

    def test_latest_issue_retry_controls_the_task_state(self) -> None:
        events = [
            event(
                1_000,
                "issue",
                "Issue update failed",
                "issue #93",
                status="failed",
                operation_id="issue-first",
            ),
            event(
                2_000,
                "issue",
                "Closed issue",
                "issue #93",
                operation_id="issue-retry",
            ),
        ]

        self.assertEqual(task_state(events), ("success", "✓", "completed"))

    def test_issue_retry_options_do_not_split_the_semantic_stage(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed('gh issue close "12"', "first", "failed")
        passed = observed(
            "gh issue close 12 --reason completed",
            "retry",
            "success",
        )
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(
            task_state([failed, passed]), ("success", "✓", "completed")
        )
        self.assertEqual(pipeline_stages([failed, passed]), ["Issue closed"])

    def test_issue_creations_are_scoped_by_private_title(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed(
            "gh issue create --title alpha --body first",
            "first",
            "failed",
        )
        passed = observed(
            "gh issue create --title beta --body second",
            "second",
            "success",
        )
        retry = observed(
            "gh issue create --title alpha --body corrected",
            "retry",
            "success",
        )
        web = observed(
            "gh issue create --title alpha --web",
            "web",
            "success",
        )
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000
        retry["epoch_ms"] = 3_000
        web["epoch_ms"] = 4_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(failed["task_stage_id"], retry["task_stage_id"])
        self.assertNotEqual(failed["task_stage_id"], web["task_stage_id"])
        self.assertNotIn("alpha", repr(failed))
        self.assertNotIn("beta", repr(passed))
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))
        self.assertEqual(task_state([failed, retry]), ("success", "✓", "completed"))
        self.assertEqual(task_state([failed, web]), ("failure", "×", "failed"))

    def test_issue_creations_are_scoped_by_private_recovery_key(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("gh issue create --recover alpha", "first", "failed")
        passed = observed("gh issue create --recover beta", "second", "success")
        retry = observed("gh issue create --recover=alpha", "retry", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000
        retry["epoch_ms"] = 3_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(failed["task_stage_id"], retry["task_stage_id"])
        self.assertNotIn("alpha", repr(failed))
        self.assertNotIn("beta", repr(passed))
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))
        self.assertEqual(task_state([failed, retry]), ("success", "✓", "completed"))

    def test_titleless_issue_creations_use_their_operation_scope(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        running = observed("gh issue create --editor", "first", "running")
        failed = observed("gh issue create --editor", "first", "failed")
        passed = observed("gh issue create", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(running["task_stage_id"], failed["task_stage_id"])
        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_issue_option_values_are_not_mistaken_for_the_target(self) -> None:
        classified = classify_commands(
            "gh issue close --duplicate-of 456 123 --reason completed"
        )

        self.assertEqual(
            classified,
            [("issue", "Closing issue", "issue #123")],
        )

    def test_pull_request_retry_options_do_not_split_the_semantic_stage(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("gh pr create", "first", "failed")
        passed = observed("gh pr create --fill", "retry", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("success", "✓", "completed"))
        self.assertEqual(pipeline_stages([failed, passed]), ["PR ✓"])

    def test_pull_request_non_creating_modes_are_not_real_retries(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        with patch("side_dog.cli._git_current_branch", return_value="topic"):
            failed = observed("gh pr create", "first", "failed")
        failed["epoch_ms"] = 1_000

        for command in (
            "gh pr create --dry-run",
            "gh pr create --web",
            "gh pr create -w",
            "gh pr create -wHtopic -Bmain",
        ):
            with (
                self.subTest(command=command),
                patch("side_dog.cli._git_current_branch", return_value="topic"),
            ):
                non_creating = observed(command, "second", "success")
            non_creating["epoch_ms"] = 2_000

            self.assertNotEqual(
                failed["task_stage_id"], non_creating["task_stage_id"]
            )
            self.assertEqual(
                task_state([failed, non_creating]), ("failure", "×", "failed")
            )

        explicit = observed(
            "gh pr create -H topic -B main", "explicit", "failed"
        )
        combined_web = observed(
            "gh pr create -wHtopic -Bmain", "combined", "success"
        )
        self.assertNotEqual(
            explicit["task_stage_id"], combined_web["task_stage_id"]
        )

    def test_pull_request_creation_targets_remain_independent(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed(
            "gh pr create -R org/a --head alpha --fill",
            "first",
            "failed",
        )
        passed = observed(
            "gh pr create -R org/b --head alpha --fill",
            "second",
            "success",
        )
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_bare_pull_request_creations_use_the_current_branch(self) -> None:
        def observed(
            command: str, tool_use_id: str, status: str
        ) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        with patch(
            "side_dog.cli._git_current_branch",
            side_effect=["alpha", "beta"],
        ):
            failed = observed("gh pr create", "first", "failed")
            passed = observed("gh pr create --fill", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_delivery_retry_options_do_not_split_semantic_stages(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        cases = (
            ("git push", "git push -u origin topic", ["Push ✓"], True),
            (
                "git push -odeploy origin topic",
                "git push -o deploy origin topic",
                ["Push ✓"],
                True,
            ),
            (
                "git push --recurse-submodules check",
                "git push --recurse-submodules=check",
                ["Push ✓"],
                True,
            ),
            (
                "gh pr merge 42",
                "gh pr merge 42 --squash",
                ["Merge ✓"],
                True,
            ),
            (
                "git worktree remove /tmp/topic",
                "git worktree remove --force /tmp/topic",
                ["Worktree"],
                True,
            ),
        )
        with patch(
            "side_dog.cli._git_push_default_target",
            return_value="origin/topic",
        ):
            for failed_command, passed_command, expected_stages, same_identity in cases:
                with self.subTest(command=failed_command):
                    failed = observed(failed_command, "first", "failed")
                    passed = observed(passed_command, "retry", "success")
                    failed["epoch_ms"] = 1_000
                    passed["epoch_ms"] = 2_000

                    comparison = (
                        self.assertEqual if same_identity else self.assertNotEqual
                    )
                    comparison(failed["task_stage_id"], passed["task_stage_id"])
                    self.assertEqual(
                        task_state([failed, passed]),
                        ("success", "✓", "completed"),
                    )
                    self.assertEqual(
                        pipeline_stages([failed, passed]), expected_stages
                    )

    def test_first_push_retry_correlates_when_it_sets_the_upstream(self) -> None:
        def observed(
            command: str, tool_use_id: str, status: str
        ) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        with (
            patch(
                "side_dog.cli._git_push_default_target",
                side_effect=["", "origin/topic"],
            ),
            patch("side_dog.cli._git_current_branch", return_value="topic"),
            patch("side_dog.cli._git_push_default_remote", return_value="origin"),
        ):
            failed = observed("git push", "first", "failed")
            passed = observed("git push -u origin topic", "retry", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("success", "✓", "completed"))
        self.assertEqual(pipeline_stages([failed, passed]), ["Push ✓"])

    def test_repository_only_pushes_retain_remote_and_current_branch(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        cases = (
            ("git push origin", "alpha", "git push fork", "alpha"),
            ("git push origin", "alpha", "git push origin", "beta"),
        )
        for failed_command, failed_branch, passed_command, passed_branch in cases:
            with (
                self.subTest(command=failed_command, branch=passed_branch),
                patch("side_dog.cli._git_push_default_target", return_value=""),
                patch(
                    "side_dog.cli._git_current_branch",
                    side_effect=[failed_branch, passed_branch],
                ),
            ):
                failed = observed(failed_command, "first", "failed")
                passed = observed(passed_command, "second", "success")
            failed["epoch_ms"] = 1_000
            passed["epoch_ms"] = 2_000

            self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
            self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_delivery_targets_remain_independent_private_stages(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        cases = (
            ("git push origin alpha", "git push origin beta"),
            ("git push -u origin topic", "git push -u fork topic"),
            ("gh pr merge 42", "gh pr merge 43"),
            ("gh pr merge --auto 42", "gh pr merge --disable-auto 42"),
            ("gh pr merge -R org/a 42", "gh pr merge -R org/b 42"),
            ("gh pr merge -Rorg/a 42", "gh pr merge -Rorg/b 42"),
        )
        for failed_command, passed_command in cases:
            with self.subTest(command=failed_command):
                failed = observed(failed_command, "first", "failed")
                passed = observed(passed_command, "second", "success")
                failed["epoch_ms"] = 1_000
                passed["epoch_ms"] = 2_000

                self.assertNotEqual(
                    failed["task_stage_id"], passed["task_stage_id"]
                )
                self.assertEqual(
                    task_state([failed, passed]), ("failure", "×", "failed")
                )

    def test_clustered_merge_repository_scopes_remain_independent(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("gh pr merge -dRorg/a 42", "first", "failed")
        passed = observed("gh pr merge -dRorg/b 42", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))
        self.assertNotIn("org/a", repr(failed))
        self.assertNotIn("org/b", repr(passed))

    def test_bare_merge_uses_the_current_branch_target(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        with (
            patch("side_dog.cli._git_push_default_target", return_value=""),
            patch(
                "side_dog.cli._git_current_branch",
                side_effect=["alpha", "beta"],
            ),
        ):
            failed = observed("gh pr merge", "first", "failed")
            passed = observed("gh pr merge", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_commit_quiet_flag_does_not_split_a_retry(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("git commit -qm private-alpha", "first", "failed")
        passed = observed("git commit -m private-alpha", "retry", "success")
        no_quiet = observed(
            "git commit --no-quiet -m private-alpha", "no-quiet", "success"
        )
        other = observed("git commit -m private-beta", "other", "success")
        attached = observed("git commit -mquiet", "attached", "success")
        changed = observed("git commit -muiet", "changed", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000
        other["epoch_ms"] = 3_000

        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(failed["task_stage_id"], no_quiet["task_stage_id"])
        self.assertNotEqual(failed["task_stage_id"], other["task_stage_id"])
        self.assertNotEqual(attached["task_stage_id"], changed["task_stage_id"])
        self.assertNotIn("private-alpha", repr(failed))
        self.assertEqual(task_state([failed, passed]), ("success", "✓", "completed"))

    def test_target_changing_push_modes_remain_independent_stages(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        with patch(
            "side_dog.cli._git_push_default_target",
            return_value="origin/topic",
        ):
            for mode in ("--all", "--dry-run", "--mirror", "--tags", "-n"):
                with self.subTest(mode=mode):
                    failed = observed(f"git push {mode}", "first", "failed")
                    passed = observed("git push", "second", "success")
                    failed["epoch_ms"] = 1_000
                    passed["epoch_ms"] = 2_000

                    self.assertNotEqual(
                        failed["task_stage_id"], passed["task_stage_id"]
                    )
                    self.assertEqual(
                        task_state([failed, passed]),
                        ("failure", "×", "failed"),
                    )

        with patch(
            "side_dog.cli._git_push_default_target",
            return_value="origin/topic",
        ):
            failed = observed("git push -fd origin topic", "first", "failed")
            passed = observed("git push origin topic", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000
        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_clustered_push_option_payload_is_not_decoded_as_flags(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("git push --delete origin alpha", "first", "failed")
        passed = observed(
            "git push -fodeploy origin alpha", "second", "success"
        )
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_push_branches_alias_coalesces_with_all(self) -> None:
        def observed(
            command: str, tool_use_id: str, status: str
        ) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("git push --all", "first", "failed")
        passed = observed("git push --branches", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(
            task_state([failed, passed]), ("success", "✓", "completed")
        )

    def test_pr_merge_option_values_do_not_split_target_retries(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed(
            "gh pr merge --match-head-commit abc123 --subject first 42",
            "first",
            "failed",
        )
        passed = observed(
            "gh pr merge --match-head-commit def456 --subject retry 42 --squash",
            "second",
            "success",
        )

        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])

    def test_issue_repository_scopes_remain_independent_stages(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("gh issue close -Rorg/a 12", "first", "failed")
        passed = observed("gh issue close -Rorg/b 12", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_issue_url_repositories_remain_independent_stages(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed(
            "gh issue close https://github.com/org/a/issues/12",
            "first",
            "failed",
        )
        passed = observed(
            "gh issue close https://github.com/org/b/issues/12",
            "second",
            "success",
        )
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_enterprise_issue_url_repositories_remain_independent_stages(self) -> None:
        def observed(
            command: str, tool_use_id: str, status: str
        ) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed(
            "gh issue close https://github.example.com/org/a/issues/12",
            "first",
            "failed",
        )
        passed = observed(
            "gh issue close https://github.example.com/org/b/issues/12",
            "second",
            "success",
        )
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))

    def test_test_presentation_flags_do_not_split_retries(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("pytest -q tests/unit", "first", "failed")
        for passed_command in (
            "pytest tests/unit",
            "python -m pytest tests/unit",
            "uv run pytest tests/unit",
        ):
            with self.subTest(command=passed_command):
                passed = observed(passed_command, "second", "success")
                failed["epoch_ms"] = 1_000
                passed["epoch_ms"] = 2_000

                self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
                self.assertEqual(
                    task_state([failed, passed]),
                    ("success", "✓", "completed"),
                )

    def test_test_runner_families_normalize_presentation_flags(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        cases = (
            ("go test -v ./...", "go test ./..."),
            ("cargo test --quiet --workspace", "cargo test --workspace"),
            ("vitest --silent run", "vitest run"),
            ("jest --verbose src", "jest src"),
            ("rspec --color spec", "rspec spec"),
            ("mix test --color test", "mix test test"),
            ("npm --silent test", "npm test"),
            ("pnpm --silent test", "pnpm test"),
            ("yarn --silent test", "yarn test"),
            ("bun --silent test", "bun test"),
            ("make test -s", "make test"),
        )
        for failed_command, passed_command in cases:
            with self.subTest(command=failed_command):
                failed = observed(failed_command, "first", "failed")
                passed = observed(passed_command, "retry", "success")
                failed["epoch_ms"] = 1_000
                passed["epoch_ms"] = 2_000

                self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
                self.assertEqual(
                    task_state([failed, passed]),
                    ("success", "✓", "completed"),
                )

    def test_equivalent_command_workdirs_share_a_stage_identity(self) -> None:
        base = {
            "agent": "codex",
            "session_id": "session",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/unit"},
        }
        root = Path("/tmp/project")
        failed = normalized_tool_events(
            {**base, "tool_use_id": "first"},
            root,
            status="failed",
        )[0]
        passed = normalized_tool_events(
            {**base, "tool_use_id": "second", "cwd": "."},
            root,
            status="success",
        )[0]
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(
            task_state([failed, passed]), ("success", "✓", "completed")
        )

    def test_worktree_targets_remain_independent_private_stages(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("git worktree remove /tmp/one", "first", "failed")
        passed = observed("git worktree remove /tmp/two", "second", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertNotIn("/tmp/one", repr(failed))
        self.assertNotIn("/tmp/two", repr(passed))
        self.assertEqual(task_state([failed, passed]), ("failure", "×", "failed"))
        self.assertEqual(
            pipeline_stages([failed, passed]), ["Worktree", "Worktree"]
        )

    def test_worktree_prune_dry_run_is_not_a_real_retry(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("git worktree prune", "first", "failed")
        dry_run = observed("git worktree prune -n", "second", "success")
        failed["epoch_ms"] = 1_000
        dry_run["epoch_ms"] = 2_000

        self.assertNotEqual(failed["task_stage_id"], dry_run["task_stage_id"])
        self.assertEqual(task_state([failed, dry_run]), ("failure", "×", "failed"))

        expiring = observed(
            "git worktree prune --expire now", "expiring", "failed"
        )
        ordinary = observed("git worktree prune", "ordinary", "success")
        equivalent = observed(
            "git worktree prune --expire=now", "equivalent", "success"
        )
        expiring["epoch_ms"] = 3_000
        ordinary["epoch_ms"] = 4_000
        equivalent["epoch_ms"] = 5_000

        self.assertNotEqual(expiring["task_stage_id"], ordinary["task_stage_id"])
        self.assertEqual(expiring["task_stage_id"], equivalent["task_stage_id"])
        self.assertEqual(
            task_state([expiring, ordinary]), ("failure", "×", "failed")
        )

    def test_worktree_add_retry_correlates_its_branch_stage(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> list[dict[str, object]]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )

        failed = observed(
            'git worktree add -b "topic" /tmp/topic',
            "first",
            "failed",
        )
        passed = observed(
            "git worktree add -B topic /tmp/topic",
            "retry",
            "success",
        )
        for event_index, item in enumerate([*failed, *passed]):
            item["epoch_ms"] = 1_000 + event_index

        failed_branch = next(item for item in failed if item["kind"] == "branch")
        passed_branch = next(item for item in passed if item["kind"] == "branch")
        failed_worktree = next(item for item in failed if item["kind"] == "worktree")
        passed_worktree = next(item for item in passed if item["kind"] == "worktree")
        self.assertEqual(failed_branch["detail"], "topic")
        self.assertEqual(
            failed_worktree["task_stage_id"], passed_worktree["task_stage_id"]
        )
        self.assertEqual(
            failed_branch["task_stage_id"], passed_branch["task_stage_id"]
        )
        self.assertEqual(
            task_state([*failed, *passed]), ("success", "✓", "completed")
        )
        self.assertEqual(
            pipeline_stages([*failed, *passed]), ["Worktree", "Branch"]
        )

    def test_direct_branch_retry_correlates_equivalent_syntax(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed('git switch -c "topic" --no-track', "first", "failed")
        passed = observed("git switch -C topic", "retry", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(failed["detail"], "topic")
        self.assertEqual(passed["detail"], "topic")
        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertEqual(task_state([failed, passed]), ("success", "✓", "completed"))
        self.assertEqual(pipeline_stages([failed, passed]), ["Branch"])

        private = "private-branch-canary"
        classified = classify_commands(
            f"echo 'git switch -c {private}'; git switch -c \"topic\""
        )
        self.assertEqual(classified, [("branch", "Creating branch", "topic")])
        self.assertNotIn(private, repr(classified))

    def test_git_branch_retry_correlates_by_created_branch(self) -> None:
        def observed(
            command: str, tool_use_id: str, status: str
        ) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed = observed("git branch topic missing-start", "first", "failed")
        passed = observed("git branch topic main", "retry", "success")
        failed["epoch_ms"] = 1_000
        passed["epoch_ms"] = 2_000

        self.assertEqual(failed["detail"], "topic")
        self.assertEqual(passed["detail"], "topic")
        self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
        self.assertNotIn("missing-start", repr(failed))
        self.assertEqual(task_state([failed, passed]), ("success", "✓", "completed"))
        self.assertEqual(pipeline_stages([failed, passed]), ["Branch"])

    def test_a_different_passing_suite_does_not_hide_a_failed_suite(self) -> None:
        events = [
            event(
                1_000,
                "test",
                "Tests failed",
                "pytest",
                status="failed",
                operation_id="pytest",
            ),
            event(
                2_000,
                "test",
                "Tests passed",
                "unittest",
                operation_id="unittest",
            ),
        ]

        self.assertEqual(task_state(events), ("failure", "×", "failed"))

    def test_command_families_distinguish_non_python_test_suites(self) -> None:
        runners = {
            command: classify_commands(command)[0][2]
            for command in (
                "cargo test",
                "go test ./...",
                "npm test",
                "pnpm test",
                "NPM TEST",
            )
        }

        self.assertEqual(
            runners,
            {
                "cargo test": "cargo test",
                "go test ./...": "go test",
                "npm test": "npm",
                "pnpm test": "pnpm",
                "NPM TEST": "npm",
            },
        )
        events = [
            event(
                1_000,
                "test",
                "Tests failed",
                runners["cargo test"],
                status="failed",
                operation_id="cargo",
            ),
            event(
                2_000,
                "test",
                "Tests passed",
                runners["go test ./..."],
                operation_id="go",
            ),
        ]
        self.assertEqual(task_state(events), ("failure", "×", "failed"))

    def test_test_invocations_are_private_stable_task_identities(self) -> None:
        def observed(command: str, tool_use_id: str, status: str) -> dict[str, object]:
            return normalized_tool_events(
                {
                    "agent": "codex",
                    "session_id": "session",
                    "tool_use_id": tool_use_id,
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                Path("/tmp/project"),
                status=status,
            )[0]

        failed_unit = observed("pytest tests/unit", "first", "failed")
        passed_integration = observed("pytest tests/integration", "second", "success")
        passed_unit_retry = observed("pytest tests/unit", "retry", "success")
        failed_unit["epoch_ms"] = 1_000
        passed_integration["epoch_ms"] = 2_000
        passed_unit_retry["epoch_ms"] = 3_000

        self.assertEqual(failed_unit["detail"], "pytest")
        self.assertEqual(passed_integration["detail"], "pytest")
        self.assertNotEqual(
            failed_unit["task_stage_id"], passed_integration["task_stage_id"]
        )
        self.assertEqual(failed_unit["task_stage_id"], passed_unit_retry["task_stage_id"])
        predictable = hashlib.sha256(
            "/tmp/project\0test\0pytest tests/unit".encode()
        ).hexdigest()[:16]
        self.assertNotEqual(failed_unit["task_stage_id"], f"test:{predictable}")
        self.assertEqual(
            task_state([failed_unit, passed_integration]),
            ("failure", "×", "failed"),
        )
        self.assertEqual(
            pipeline_stages([failed_unit, passed_integration]),
            ["Tests ×", "Tests ✓"],
        )
        self.assertEqual(
            task_state([failed_unit, passed_unit_retry]),
            ("success", "✓", "completed"),
        )
        self.assertEqual(
            pipeline_stages([failed_unit, passed_unit_retry]),
            ["Tests ×2 ✓"],
        )

        other_package = normalized_tool_events(
            {
                "agent": "codex",
                "session_id": "session",
                "tool_use_id": "other-package",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest tests/unit"},
                "cwd": "/tmp/project/packages/other",
            },
            Path("/tmp/project"),
            status="success",
        )[0]
        other_package["epoch_ms"] = 4_000
        self.assertNotEqual(
            failed_unit["task_stage_id"], other_package["task_stage_id"]
        )
        self.assertEqual(
            task_state([failed_unit, other_package]),
            ("failure", "×", "failed"),
        )

        quoted_double_space = observed(
            'pytest "tests/a  b.py"', "quoted-double", "failed"
        )
        quoted_single_space = observed(
            'pytest "tests/a b.py"', "quoted-single", "success"
        )
        self.assertNotEqual(
            quoted_double_space["task_stage_id"],
            quoted_single_space["task_stage_id"],
        )

    def test_managed_hooks_share_a_private_stage_identity_across_processes(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            payload = {
                "agent": "claude-code",
                "session_id": "session",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest tests/unit --tenant private-name"},
            }
            with patch.dict(
                os.environ,
                {STATE_ENV: os.fspath(state), "SIDE_DOG_MANAGED": "1"},
            ):
                failed = normalized_tool_events(
                    {**payload, "tool_use_id": "first"},
                    Path("/tmp/project"),
                    status="failed",
                )[0]
                _managed_task_stage_key.cache_clear()
                passed = normalized_tool_events(
                    {**payload, "tool_use_id": "retry"},
                    Path("/tmp/project"),
                    status="success",
                )[0]

            self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
            key_path = state / "task-stage.key"
            self.assertEqual(len(key_path.read_bytes()), 32)
            self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-name", failed["task_stage_id"])

    def test_managed_stage_key_repairs_an_interrupted_creation(self) -> None:
        with TemporaryDirectory() as directory:
            key_path = Path(directory) / "state" / "task-stage.key"
            key_path.parent.mkdir()
            key_path.write_bytes(b"partial")

            first = _managed_task_stage_key(os.fspath(key_path))
            _managed_task_stage_key.cache_clear()
            second = _managed_task_stage_key(os.fspath(key_path))

            self.assertEqual(len(first), 32)
            self.assertEqual(first, second)
            self.assertEqual(key_path.read_bytes(), first)
            self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)

    def test_native_collectors_share_stage_identity_across_restarts(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            payload = {
                "agent": "codex",
                "session_id": "session",
                "tool_name": "Bash",
                "tool_input": {"command": "pytest tests/unit"},
            }
            with patch.dict(
                os.environ,
                {STATE_ENV: os.fspath(state), "SIDE_DOG_MANAGED": "0"},
            ):
                failed = normalized_tool_events(
                    {**payload, "tool_use_id": "first"},
                    Path("/tmp/project"),
                    status="failed",
                )[0]
                _managed_task_stage_key.cache_clear()
                passed = normalized_tool_events(
                    {**payload, "tool_use_id": "retry"},
                    Path("/tmp/project"),
                    status="success",
                )[0]

            self.assertEqual(failed["task_stage_id"], passed["task_stage_id"])
            self.assertEqual(task_state([failed, passed]), ("success", "✓", "completed"))

    def test_case_sensitive_edit_targets_remain_independent(self) -> None:
        events = [
            event(
                1_000,
                "file",
                "File write failed",
                "src/Foo.py",
                status="failed",
                operation_id="first",
            ),
            event(
                2_000,
                "file",
                "Wrote file",
                "src/foo.py",
                operation_id="second",
            ),
        ]

        self.assertEqual(task_state(events), ("failure", "×", "failed"))

    def test_independent_worktree_actions_keep_independent_outcomes(self) -> None:
        removal_failed = event(
            1_000,
            "worktree",
            "Worktree update failed",
            "git worktree",
            status="failed",
            operation_id="remove",
        )
        addition_passed = event(
            2_000,
            "worktree",
            "Worktree updated",
            "git worktree add",
            operation_id="add",
        )
        removal_retry = event(
            3_000,
            "worktree",
            "Worktree updated",
            "git worktree",
            operation_id="remove-retry",
        )

        self.assertEqual(
            task_state([removal_failed, addition_passed]),
            ("failure", "×", "failed"),
        )
        self.assertEqual(
            task_state([removal_failed, removal_retry]),
            ("success", "✓", "completed"),
        )

    def test_later_successful_work_recovers_from_a_command_diagnostic(self) -> None:
        failed_command = event(
            1_000,
            "command",
            "Command failed",
            "python",
            status="failed",
            operation_id="failed-command",
        )
        completed_commit = event(
            2_000,
            "commit",
            "Commit created",
            "abc1234 recovered",
            operation_id="commit",
        )

        self.assertEqual(
            task_state([failed_command, completed_commit]),
            ("success", "✓", "completed"),
        )
        self.assertEqual(
            task_state([completed_commit, {**failed_command, "epoch_ms": 3_000}]),
            ("failure", "×", "failed"),
        )
        self.assertEqual(
            task_state(
                [
                    {**failed_command, "epoch_ms": 4_000, "_append_ordinal": 1},
                    {
                        **completed_commit,
                        "epoch_ms": 4_000,
                        "_append_ordinal": 2,
                    },
                ]
            ),
            ("success", "✓", "completed"),
        )

    def test_a_different_successful_edit_does_not_hide_a_failed_edit(self) -> None:
        events = [
            event(
                1_000,
                "file",
                "File write failed",
                "broken.py",
                status="failed",
                operation_id="broken",
            ),
            event(
                2_000,
                "file",
                "Wrote file",
                "working.py",
                operation_id="working",
            ),
        ]

        self.assertEqual(task_state(events), ("failure", "×", "failed"))

    def test_a_retry_of_the_same_edit_replaces_its_failure(self) -> None:
        events = [
            event(
                1_000,
                "file",
                "File write failed",
                "app.py",
                status="failed",
                operation_id="first",
            ),
            event(
                2_000,
                "file",
                "Wrote file",
                "app.py",
                operation_id="retry",
            ),
        ]

        self.assertEqual(task_state(events), ("success", "✓", "completed"))

    def test_conflicting_pull_request_makes_the_task_blocked(self) -> None:
        events = [event(1_000, "pr", "PR updated", "pull request")]

        for github_status in (
            {"merge_state": "DIRTY"},
            {"mergeable": "CONFLICTING"},
        ):
            with self.subTest(github_status=github_status):
                self.assertEqual(
                    task_state(events, github_status),
                    ("failure", "×", "blocked"),
                )

    def test_failed_checks_or_requested_changes_make_the_task_failed(self) -> None:
        events = [event(1_000, "pr", "PR updated", "pull request")]

        for github_status in (
            {"merge_state": "UNSTABLE", "checks_failed": 1},
            {"merge_state": "CLEAN", "review": "CHANGES_REQUESTED"},
        ):
            with self.subTest(github_status=github_status):
                self.assertEqual(
                    task_state(events, github_status),
                    ("failure", "×", "failed"),
                )

    def test_pending_checks_make_the_task_running(self) -> None:
        events = [event(1_000, "pr", "PR updated", "pull request")]

        self.assertEqual(
            task_state(events, {"merge_state": "UNSTABLE", "checks_pending": 2}),
            ("running", "…", "running"),
        )

    def test_task_uses_the_associated_github_blocked_state(self) -> None:
        shared = {"turn_id": "turn", "agent": "codex"}
        events = [
            event(1_000, "file", "Wrote file", "app.py", **shared),
            event(2_000, "test", "Tests passed", "unit", **shared),
            event(3_000, "pr", "PR created", "gh pr create", **shared),
            event(
                4_000,
                "github",
                "PR #7 confirmed",
                "blocked",
                **shared,
                github={
                    "number": 7,
                    "title": "Feature",
                    "state": "OPEN",
                    "ci": "CI 7/7",
                    "merge_state": "BLOCKED",
                },
            ),
        ]

        screen = self.render_lines(events, expanded=False)

        self.assertIn("Agent task · × blocked · 3 events", screen)
        self.assertIn("PR #7", screen)

    def test_narrow_task_wraps_metadata_without_cropping_it(self) -> None:
        events = [
            event(
                1_000,
                "file",
                "Wrote file",
                "app.py",
                agent="codex",
                turn_id="turn",
            ),
            event(
                3_000,
                "test",
                "Tests passed",
                "unit",
                agent="codex",
                turn_id="turn",
            ),
        ]
        lines, _ = render_timeline_activity(
            events,
            line_budget=20,
            width=28,
            color=False,
            now_ms=3_000,
            identities={},
            expanded_history=False,
            event_filter="all",
        )
        screen = "\n".join(lines)

        self.assertTrue(all(terminal_cell_width(line) <= 28 for line in lines))
        self.assertIn("✓ completed", screen)
        self.assertIn("2 events", screen)
        self.assertIn("2.0s", screen)
        self.assertIn("└─ Edit ×1 → Tests ✓", screen)

    def test_colored_task_keeps_status_and_structure_semantic(self) -> None:
        events = [
            event(
                1_000,
                "file",
                "Wrote file",
                "app.py",
                agent="codex",
                turn_id="turn",
            ),
            event(
                2_000,
                "test",
                "Tests failed",
                "unit",
                agent="codex",
                turn_id="turn",
                status="failed",
            ),
        ]
        colored, _ = render_timeline_activity(
            events,
            line_budget=20,
            width=60,
            color=True,
            now_ms=2_000,
            identities={},
            expanded_history=True,
            event_filter="all",
        )
        plain, _ = render_timeline_activity(
            events,
            line_budget=20,
            width=60,
            color=False,
            now_ms=2_000,
            identities={},
            expanded_history=True,
            event_filter="all",
        )

        self.assertIn(f"{ANSI['red']}{ANSI['bold']}× failed", "\n".join(colored))
        self.assertIn(f"{ANSI['dim']}├─", "\n".join(colored))
        self.assertNotIn("\x1b[", "\n".join(plain))
        self.assertIn("× failed", "\n".join(plain))

    def test_tight_expanded_task_reports_hidden_children_and_keeps_outcome(self) -> None:
        shared = {"turn_id": "turn", "agent": "codex"}
        events = [
            event(1_000, "file", "Wrote file", "one.py", **shared),
            event(2_000, "file", "Wrote file", "two.py", **shared),
            event(3_000, "file", "Wrote file", "three.py", **shared),
            event(4_000, "test", "Running tests", "unit", status="running", **shared),
            event(5_000, "test", "Tests passed", "latest outcome", **shared),
        ]
        lines, hidden = render_timeline_activity(
            events,
            line_budget=4,
            width=80,
            color=False,
            now_ms=5_000,
            identities={},
            expanded_history=True,
            event_filter="all",
        )
        screen = "\n".join(lines)

        self.assertEqual(hidden, 4)
        self.assertIn("4 earlier events hidden", screen)
        self.assertIn("latest outcome", screen)
        self.assertNotIn("one.py", screen)

    def test_narrow_task_uses_its_only_child_row_for_the_latest_outcome(self) -> None:
        shared = {"turn_id": "turn", "agent": "codex"}
        events = [
            event(1_000, "file", "Wrote file", "one.py", **shared),
            event(2_000, "file", "Wrote file", "two.py", **shared),
            event(3_000, "file", "Wrote file", "three.py", **shared),
            event(4_000, "test", "Running tests", "unit", status="running", **shared),
            event(5_000, "test", "Tests passed", "latest outcome", **shared),
        ]

        lines, hidden = render_timeline_activity(
            events,
            line_budget=5,
            width=28,
            color=False,
            now_ms=5_000,
            identities={},
            expanded_history=True,
            event_filter="all",
        )
        screen = "\n".join(lines)

        self.assertEqual(hidden, 4)
        self.assertIn("└─", screen)
        self.assertIn("✓", screen)
        self.assertIn("4 hidden", screen)
        self.assertNotIn("\x1b[", screen)

        colored, colored_hidden = render_timeline_activity(
            events,
            line_budget=5,
            width=28,
            color=True,
            now_ms=5_000,
            identities={},
            expanded_history=True,
            event_filter="all",
        )
        colored_screen = ANSI_ESCAPE.sub("", "\n".join(colored))
        self.assertEqual(colored_hidden, 4)
        self.assertIn("✓", colored_screen)
        self.assertIn("4 hidden", colored_screen)
        self.assertTrue(
            all(
                terminal_cell_width(ANSI_ESCAPE.sub("", line)) <= 28
                for line in colored
            )
        )

    def test_non_task_truncation_still_reports_omitted_rows(self) -> None:
        unit = {
            "type": "filesystem_burst",
            "events": [{}],
        }

        visible, hidden = truncate_activity_unit(
            unit,
            ["heading", "detail"],
            1,
            80,
            False,
            expanded_history=False,
        )

        self.assertEqual(visible, ["heading"])
        self.assertEqual(hidden, 1)

    def test_multi_child_truncation_marker_fits_the_viewport(self) -> None:
        unit = {"type": "pipeline", "events": [{}, {}, {}, {}, {}]}
        lines = [
            "heading",
            "child one",
            "child two",
            "child three",
            "child four",
            "child five",
        ]

        visible, hidden = truncate_activity_unit(
            unit,
            lines,
            4,
            28,
            False,
            expanded_history=True,
        )

        self.assertEqual(hidden, 3)
        self.assertTrue(all(terminal_cell_width(line) <= 28 for line in visible))

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
            show_filesystem_activity=True,
        )

        self.assertIn("p paused · 3 new", screen)
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

    def test_wrapper_option_operands_never_become_collected_programs(self) -> None:
        canaries = ("PRIVATE_USERNAME_81", "PRIVATE_VARIABLE_81")
        commands = (
            f"sudo -u {canaries[0]} make",
            f"env -u {canaries[1]} make",
        )
        for command, canary in zip(commands, canaries, strict=True):
            with self.subTest(command=command):
                events = self.events(command, "failed")
                self.assertEqual(events[0]["detail"], "command")
                self.assertNotIn(canary, json.dumps(events))


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
        self.assertEqual(
            command[-4:], ["panel", "--no-notify", "/tmp/one", "/tmp/two"]
        )
        self.assertEqual(command[: len(side_dog_command())], side_dog_command())
        self.assertTrue(panel.alive())

    def test_launching_from_a_herdr_watch_keeps_following_the_session(self) -> None:
        with patch("side_dog.cli.subprocess.Popen") as popen:
            popen.return_value.stdout = None
            popen.return_value.poll.return_value = None
            launch_web_panel([Path("/tmp/one")], follow_herdr=True)

        self.assertEqual(
            popen.call_args.args[0][-4:],
            ["panel", "--no-notify", "/tmp/one", "--herdr"],
        )

    def test_launching_from_a_herdr_watch_only_pins_requested_roots(self) -> None:
        pinned = Path("/tmp/pinned")
        discovered = Path("/tmp/discovered")
        with patch("side_dog.cli.subprocess.Popen") as popen:
            popen.return_value.stdout = None
            popen.return_value.poll.return_value = None
            launch_web_panel(
                [pinned, discovered],
                follow_herdr=True,
                requested_roots={pinned},
            )

        self.assertEqual(
            popen.call_args.args[0][-4:],
            ["panel", "--no-notify", "/tmp/pinned", "--herdr"],
        )

    def test_launching_preserves_the_originating_discovery_mode(self) -> None:
        mode = folder_discovery_mode(
            explicit_roots=False, follow_herdr=False, require_herdr=False
        )
        with patch("side_dog.cli.subprocess.Popen") as popen:
            popen.return_value.stdout = None
            popen.return_value.poll.return_value = None
            launch_web_panel([Path("/tmp/discovered")], discovery_mode=mode)

        self.assertEqual(
            popen.call_args.args[0][-2:],
            ["--discovery-mode", "automatic"],
        )

    def test_zero_argument_herdr_watch_does_not_pin_discovered_roots(self) -> None:
        with patch("side_dog.cli.subprocess.Popen") as popen:
            popen.return_value.stdout = None
            popen.return_value.poll.return_value = None
            launch_web_panel(
                [Path("/tmp/discovered")],
                follow_herdr=True,
                requested_roots=set(),
            )

        self.assertEqual(
            popen.call_args.args[0],
            [*side_dog_command(), "panel", "--no-notify", "--herdr"],
        )

    def test_a_panel_that_will_not_start_is_reported_as_dead(self) -> None:
        with patch("side_dog.cli.subprocess.Popen", side_effect=OSError):
            panel = launch_web_panel([Path("/tmp/one")])

        self.assertFalse(panel.alive())
        self.assertEqual(panel.url, "")
        panel.stop()

    def test_the_key_is_kept_in_help_without_crowding_the_footer(self) -> None:
        help_lines = "\n".join(render_help(80, False, True, root_count=1))
        screen = render(
            [], Path("/tmp/example-project"), width=80, height=12, color=False
        )

        self.assertIn("C       open the browser panel", help_lines)
        self.assertNotIn("C web", screen)


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
            events,
            20,
            80,
            False,
            10_000,
            {},
            False,
            "all",
            search="README",
            show_filesystem_activity=True,
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
                    newest_first=False,
                    expanded_history=True,
                    expanded_header=True,
                    event_filter="files",
                )

                self.assertEqual(
                    load_display_settings(),
                    {
                        "newest_first": False,
                        "expanded_history": True,
                        "expanded_header": True,
                        "event_filter": "files",
                        "show_filesystem_activity": False,
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

    def test_confirming_quit_and_double_ctrl_c_are_advertised(self) -> None:
        screen = render(
            [], Path("/tmp/example-project"), width=100, height=8, color=False
        )
        help_lines = "\n".join(render_help(80, False, True, root_count=1))

        self.assertIn("q quit", screen)
        self.assertIn("q       confirm before quitting Side Dog", help_lines)
        self.assertIn("Ctrl-C  confirm once; press twice", help_lines)

    def test_quit_confirmation_defaults_to_no_and_second_request_quits(self) -> None:
        confirmation = QuitConfirmation()

        self.assertFalse(confirmation.request())
        self.assertTrue(confirmation.visible)
        self.assertFalse(confirmation.selected_yes)
        self.assertTrue(confirmation.request())

    def test_quit_confirmation_navigation_and_shortcuts(self) -> None:
        confirmation = QuitConfirmation(visible=True)

        self.assertEqual(confirmation.handle_key(b"\x1b[C"), "stay")
        self.assertTrue(confirmation.selected_yes)
        self.assertEqual(confirmation.handle_key(b"\r"), "quit")

        for key in (b"\t", b"\x1b[D", b"\x1b[C"):
            with self.subTest(key=key):
                confirmation = QuitConfirmation(visible=True)
                self.assertEqual(confirmation.handle_key(key), "stay")
                self.assertTrue(confirmation.selected_yes)
        for key in (b"n", b"N", b"\x1b", b"\r"):
            with self.subTest(key=key):
                confirmation = QuitConfirmation(visible=True)
                self.assertEqual(confirmation.handle_key(key), "cancel")
                self.assertFalse(confirmation.visible)
        for key in (b"y", b"Y"):
            with self.subTest(key=key):
                confirmation = QuitConfirmation(visible=True)
                self.assertEqual(confirmation.handle_key(key), "quit")

    def test_terminal_reader_assembles_arrow_keys_for_the_dialog(self) -> None:
        with (
            patch("side_dog.cli.os.read", side_effect=[b"\x1b", b"[C"]),
            patch("side_dog.cli.select.select", return_value=([7], [], [])),
        ):
            self.assertEqual(read_terminal_key(7), b"\x1b[C")

    def test_terminal_reader_waits_for_a_split_arrow_sequence(self) -> None:
        ready = ([7], [], [])
        with (
            patch("side_dog.cli.os.read", side_effect=[b"\x1b", b"[", b"D"]),
            patch("side_dog.cli.select.select", side_effect=[ready, ready]),
        ):
            self.assertEqual(read_terminal_key(7), b"\x1b[D")

    def test_terminal_reader_preserves_a_bare_escape(self) -> None:
        with (
            patch("side_dog.cli.os.read", return_value=b"\x1b"),
            patch("side_dog.cli.select.select", return_value=([], [], [])),
        ):
            self.assertEqual(read_terminal_key(7), b"\x1b")

    def test_quit_dialog_is_narrow_safe_and_clear_without_color(self) -> None:
        screen = "\n".join(f"timeline row {index}" for index in range(20))

        rendered = render_quit_confirmation(
            screen, width=12, height=30, color=False
        )

        self.assertIn("Are you", rendered)
        self.assertIn("quit?", rendered)
        self.assertIn("> No <", rendered)
        self.assertIn("Ctrl-C", rendered)
        self.assertIn("timeline", rendered)
        self.assertTrue(
            all(terminal_cell_width(line) <= 12 for line in rendered.splitlines())
        )

    def test_colored_quit_dialog_subdues_timeline_and_marks_selection(self) -> None:
        screen = "\n".join(f"timeline row {index}" for index in range(20))

        rendered = render_quit_confirmation(
            screen, width=80, height=20, color=True, selected_yes=True
        )

        self.assertIn(f"{ANSI['dim']}timeline row 0", rendered)
        self.assertIn(ANSI["inverse"], rendered)
        self.assertIn("> Yes <", rendered)

    def test_quit_dialog_centers_in_the_terminal_not_the_content(self) -> None:
        rendered = render_quit_confirmation(
            "timeline", width=60, height=20, color=False
        ).splitlines()

        self.assertEqual(rendered[0], "timeline")
        box_top = next(i for i, line in enumerate(rendered) if "┌" in line)
        self.assertGreaterEqual(box_top, 3)

    def test_reloading_with_R_is_advertised(self) -> None:
        screen = render(
            [], Path("/tmp/example-project"), width=100, height=8, color=False
        )
        help_lines = "\n".join(render_help(80, False, True, root_count=1))

        self.assertNotIn("R reload", screen)
        self.assertIn("R       reload Side Dog", help_lines)

    def test_a_reload_runs_this_side_dog_again_with_the_same_arguments(self) -> None:
        with (
            patch("side_dog.cli.sys.argv", ["side-dog", "watch", ".", "--width", "42"]),
            patch("side_dog.cli.side_dog_command", return_value=["/bin/side-dog"]),
            patch("side_dog.cli.os.execvp") as execvp,
        ):
            restart_side_dog()

        self.assertEqual(
            execvp.call_args.args,
            ("/bin/side-dog", ["/bin/side-dog", "watch", ".", "--width", "42"]),
        )

    def test_a_reload_that_cannot_start_gives_up_quietly(self) -> None:
        with (
            patch("side_dog.cli.side_dog_command", return_value=["/nope"]),
            patch("side_dog.cli.os.execvp", side_effect=OSError),
        ):
            restart_side_dog()  # must not raise

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
        shared = {
            "sid": {
                "agent": "claude-code",
                "label": "from herdr",
                "session_id": "sid",
            }
        }
        with (
            patch(
                "side_dog.cli.claude_identities",
                return_value={
                    "sid": {
                        "agent": "claude-code",
                        "label": "from the registry",
                        "session_id": "sid",
                    }
                },
            ),
            patch("side_dog.cli.load_codex_session_identities", return_value={}),
            patch("side_dog.cli.opencode_identities", return_value={}),
            patch("side_dog.cli.cline_identities", return_value={}),
            patch("side_dog.cli.load_herdr_identities", return_value=shared),
        ):
            merged = load_agent_identities(Path("/tmp"))

        self.assertEqual(merged["claude-code:sid"]["label"], "from herdr")

    def test_two_sessions_sharing_a_label_stay_two_agents(self) -> None:
        # Two desktop conversations in one folder are both labelled "desktop"
        # and neither has a pane, so only the session id keeps them apart.
        identities = {
            "one": {"agent": "claude-code", "label": "desktop", "session_id": "one"},
            "two": {"agent": "claude-code", "label": "desktop", "session_id": "two"},
        }

        self.assertEqual(len(active_agent_identities(identities)), 2)

    def test_an_agent_without_a_session_still_collapses_by_label(self) -> None:
        identities = {
            "a": {"agent": "codex", "label": "same"},
            "b": {"agent": "codex", "label": "same"},
        }

        self.assertEqual(len(active_agent_identities(identities)), 1)

    def test_the_session_listing_is_walked_once_per_tick(self) -> None:
        with TemporaryDirectory() as directory:
            sessions = Path(directory) / "sessions" / "2026" / "09" / "01"
            sessions.mkdir(parents=True)
            (sessions / "rollout-one.jsonl").write_text("{}\n")
            CODEX_LISTING_CACHE.clear()
            try:
                with patch.dict(os.environ, {"CODEX_HOME": directory}):
                    first = codex_session_listing()
                    (sessions / "rollout-two.jsonl").write_text("{}\n")
                    second = codex_session_listing()

                    self.assertEqual(len(first), 1)
                    # The new file is not seen until the shared listing expires.
                    self.assertEqual(second, first)

                    CODEX_LISTING_CACHE.clear()
                    self.assertEqual(len(codex_session_listing()), 2)
            finally:
                CODEX_LISTING_CACHE.clear()
