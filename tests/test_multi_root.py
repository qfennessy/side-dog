import json
import os
import subprocess
import time
from collections import deque
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog import __version__
from side_dog.cli import (
    ANSI,
    ANSI_ESCAPE,
    CLAUDE_METADATA_CACHE,
    ROOT_NAME_INK,
    ROOT_PALETTE,
    SOURCE_COLOR_INDEX,
    WatchRootExternalRefresh,
    WatchRootState,
    apply_completed_watch_root_refreshes,
    apply_watch_root_external_refresh,
    aggregate_watch_identities,
    append_event,
    initialize_watch_root,
    STATE_ENV,
    aggregate_watch_records,
    build_parser,
    canonical_root,
    canonical_watch_roots,
    clear_herdr_snapshot_cache,
    clear_session_path_cache,
    crop,
    discovered_worktrees,
    busy_worktrees,
    events_path,
    folder_due_for_scan,
    folder_discovery_mode,
    git_changed_paths,
    github_refresh_due,
    github_fingerprint,
    github_refresh_interval,
    latest_events,
    snapshot,
    folder_is_finished,
    folders_worth_a_column,
    follow_new_worktrees,
    herdr_session_roots,
    initial_watch_roots,
    invoked_within_herdr,
    load_herdr_identities,
    reconcile_herdr_roots,
    retired_worktrees,
    git_worktree_paths,
    git_worktree_root,
    load_claude_metadata,
    poll_watch_root,
    render,
    render_agent_context_text,
    render_context_banners,
    render_milestone_card,
    render_root_columns,
    render_timeline_activity,
    root_color,
    root_column_widths,
    root_focus_for_key,
    schedule_watch_root_refreshes,
    should_render_root_columns,
    terminal_cell_width,
    verified_post_switch_delivery_context,
    wait_for_watch_root_refreshes,
    watch_root_column_identities,
    watch_root_labels,
    watch_root_summary,
)
from side_dog.model import (
    SOURCE_KEY,
    SOURCE_LABEL,
    coalesce_operations,
    identity_for_event,
    latest_delivery_context,
)
from side_dog.usage import LiveUsageSnapshot, UsageBlock, UsageReport


def activity(
    epoch_ms: int,
    detail: str,
    *,
    kind: str = "file",
    agent: str = "filesystem",
    status: str = "success",
    **extra: object,
) -> dict[str, object]:
    return {
        "epoch_ms": epoch_ms,
        "timestamp": datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).isoformat(),
        "kind": kind,
        "title": "File changed" if kind == "file" else "Tests passed",
        "detail": detail,
        "agent": agent,
        "status": status,
        **extra,
    }


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def root_state(
    root: Path,
    records: list[dict[str, object]],
    *,
    branch: str | None = None,
    pr_number: int | None = None,
) -> WatchRootState:
    git_status = (
        {
            "branch": branch,
            "oid": "1234567890abcdef",
            "short_oid": "1234567",
            "repository": "side-dog",
        }
        if branch
        else None
    )
    github_status = (
        {
            "number": pr_number,
            "title": "Multi-root watch",
            "state": "OPEN",
            "merge_state": "CLEAN",
        }
        if pr_number is not None
        else None
    )
    return WatchRootState(
        root=root,
        path=root / "events.jsonl",
        records=deque(records, maxlen=500),
        position=0,
        known_files={},
        git_status=git_status,
        last_hook_writes={},
        identities={},
        github_status=github_status,
        last_github_fingerprint=None,
        last_scan=0.0,
        last_git_refresh=0.0,
        last_herdr_refresh=0.0,
        last_github_refresh=0.0,
    )


class MultiRootWatchTest(TestCase):
    def tearDown(self) -> None:
        CLAUDE_METADATA_CACHE.clear()

    def test_watch_parser_accepts_zero_one_or_many_roots(self) -> None:
        parser = build_parser()

        # Bare `watch` keeps the sentinel default, which main() reads as
        # "nobody named a folder" before handing watch() an empty list.
        self.assertEqual(parser.parse_args(["watch"]).projects, ["."])
        self.assertEqual(parser.parse_args(["watch"]).layout, "auto")
        self.assertFalse(parser.parse_args(["watch"]).herdr)
        self.assertTrue(parser.parse_args(["watch", "--herdr"]).herdr)
        self.assertEqual(parser.parse_args(["watch", "one"]).projects, ["one"])
        self.assertEqual(
            parser.parse_args(["watch", "one", "two"]).projects,
            ["one", "two"],
        )
        self.assertEqual(
            parser.parse_args(["watch", "one", "two", "--layout", "columns"]).layout,
            "columns",
        )
        self.assertEqual(parser.parse_args(["watch"]).github_poll, 60.0)

    def test_github_polling_backs_off_by_branch_state(self) -> None:
        self.assertEqual(github_refresh_interval(None, 60.0), 300.0)
        self.assertEqual(
            github_refresh_interval({"state": "OPEN"}, 60.0),
            60.0,
        )
        self.assertEqual(
            github_refresh_interval(
                {"state": "OPEN", "coverage": "PARTIAL"}, 60.0
            ),
            300.0,
        )
        self.assertEqual(
            github_refresh_interval({"state": "MERGED"}, 60.0),
            900.0,
        )
        self.assertEqual(github_refresh_interval(None, 600.0), 600.0)

    def test_github_polling_starts_immediately_then_uses_backoff(self) -> None:
        self.assertTrue(github_refresh_due(None, float("-inf"), 10.0, 60.0))
        self.assertFalse(github_refresh_due(None, 10.0, 309.0, 60.0))
        self.assertTrue(github_refresh_due(None, 10.0, 310.0, 60.0))
        self.assertTrue(
            github_refresh_due({"state": "OPEN"}, 10.0, 70.0, 60.0)
        )

    def test_herdr_context_detection_uses_the_pane_or_socket_environment(self) -> None:
        self.assertTrue(invoked_within_herdr({"HERDR_ENV": "1"}))
        self.assertTrue(invoked_within_herdr({"HERDR_SOCKET_PATH": "/tmp/herdr.sock"}))
        self.assertFalse(invoked_within_herdr({}))
        self.assertFalse(invoked_within_herdr({"HERDR_ENV": "0"}))

    def test_one_herdr_snapshot_discovers_and_identifies_several_roots(self) -> None:
        with TemporaryDirectory() as directory:
            first = (Path(directory) / "first").resolve()
            second = (Path(directory) / "second").resolve()
            first.mkdir()
            second.mkdir()
            document = {
                "result": {
                    "snapshot": {
                        "agents": [
                            {
                                "agent": "codex",
                                "agent_status": "idle",
                                "foreground_cwd": os.fspath(first),
                                "pane_id": "w1:p1",
                                "agent_session": {"value": "codex-session"},
                            },
                            {
                                "agent": "codex",
                                "agent_status": "working",
                                "foreground_cwd": os.fspath(second),
                                "pane_id": "w1:p2",
                                "agent_session": {"value": "other-session"},
                            },
                            {
                                "agent": "codex",
                                "agent_status": "working",
                                "foreground_cwd": os.fspath(second),
                                "pane_id": "w1:p3",
                                "agent_session": {"value": "third-session"},
                            },
                            {
                                "agent": "unsupported",
                                "foreground_cwd": os.fspath(first),
                            },
                        ]
                    }
                }
            }
            completed = subprocess.CompletedProcess(
                ["herdr", "api", "snapshot"], 0, json.dumps(document), ""
            )
            clear_herdr_snapshot_cache()
            with (
                patch("side_dog.cli.shutil.which", return_value="/usr/bin/herdr"),
                patch("side_dog.cli.subprocess.run", return_value=completed) as run,
                patch("side_dog.cli.git_worktree_root", return_value=""),
                patch("side_dog.cli.git_common_dir", return_value=""),
                patch("side_dog.cli.load_codex_metadata", return_value={}),
                patch.dict(os.environ, {"HERDR_WORKSPACE_ID": ""}, clear=False),
            ):
                roots, error = herdr_session_roots()
                first_identities = load_herdr_identities(first)
                second_identities = load_herdr_identities(second)

            self.assertIsNone(error)
            self.assertEqual(roots, [second, first])
            herdr_calls = [
                call
                for call in run.call_args_list
                if call.args and call.args[0] == ["herdr", "api", "snapshot"]
            ]
            self.assertEqual(len(herdr_calls), 1)
            self.assertIn("codex-session", first_identities)
            self.assertEqual(
                {value["pane_id"] for value in second_identities.values()},
                {"w1:p2", "w1:p3"},
            )

    def test_herdr_roots_are_limited_to_the_inherited_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            inherited = (Path(directory) / "inherited").resolve()
            unrelated = (Path(directory) / "unrelated").resolve()
            inherited.mkdir()
            unrelated.mkdir()
            agents = [
                {
                    "agent": "codex",
                    "workspace_id": "ours",
                    "foreground_cwd": os.fspath(inherited),
                },
                {
                    "agent": "codex",
                    "workspace_id": "theirs",
                    "foreground_cwd": os.fspath(unrelated),
                },
            ]
            with (
                patch("side_dog.cli.load_herdr_snapshot", return_value=(agents, None)),
                patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "ours"}, clear=False),
                patch("side_dog.cli.git_worktree_root", return_value=""),
            ):
                roots, error = herdr_session_roots()

            self.assertIsNone(error)
            self.assertEqual(roots, [inherited])

    def test_herdr_roots_join_explicit_roots_and_make_room_for_live_work(self) -> None:
        with TemporaryDirectory() as directory:
            explicit = (Path(directory) / "explicit").resolve()
            stale = (Path(directory) / "stale").resolve()
            live = (Path(directory) / "live").resolve()
            for root in (explicit, stale, live):
                root.mkdir()
            with patch("side_dog.cli.herdr_session_roots", return_value=([live], None)):
                roots, requested, error = initial_watch_roots(
                    [explicit], follow_herdr=True
                )

            self.assertEqual(roots, [explicit, live])
            self.assertEqual(requested, {explicit})
            self.assertIsNone(error)
            retired, additions = reconcile_herdr_roots(
                [explicit, stale], [live], {explicit}, limit=2
            )
            self.assertEqual(retired, [stale])
            self.assertEqual(additions, [live])

    def test_outside_herdr_an_empty_folder_list_still_means_current_folder(self) -> None:
        roots, requested, error = initial_watch_roots([], follow_herdr=False)

        self.assertEqual(roots, [Path.cwd().resolve()])
        self.assertEqual(requested, set())
        self.assertIsNone(error)

    def test_first_herdr_agent_replaces_the_temporary_current_folder(self) -> None:
        with TemporaryDirectory() as directory:
            shell = (Path(directory) / "shell").resolve()
            live = (Path(directory) / "live").resolve()
            shell.mkdir()
            live.mkdir()
            with patch("side_dog.cli.canonical_root", return_value=shell):
                retired, additions = reconcile_herdr_roots(
                    [shell], [live], set(), limit=8
                )

            self.assertEqual(retired, [shell])
            self.assertEqual(additions, [live])

    def test_higher_priority_live_root_replaces_the_last_ranked_root(self) -> None:
        with TemporaryDirectory() as directory:
            roots = [
                (Path(directory) / f"root-{index}").resolve()
                for index in range(9)
            ]
            for root in roots:
                root.mkdir()
            newly_working = roots[0]
            watched = roots[1:]

            retired, additions = reconcile_herdr_roots(
                watched,
                [newly_working, *watched],
                set(),
                limit=8,
            )

            self.assertEqual(retired, [watched[-1]])
            self.assertEqual(additions, [newly_working])

    def test_column_widths_allocate_remainders_and_fall_back(self) -> None:
        self.assertEqual(root_column_widths(84, 2), [42, 42])
        self.assertEqual(root_column_widths(85, 2), [43, 42])
        self.assertEqual(root_column_widths(126, 3), [42, 42, 42])
        self.assertEqual(root_column_widths(83, 2), [])
        self.assertEqual(root_column_widths(120, 1), [])

    def test_column_layout_respects_mode_width_focus_and_help(self) -> None:
        self.assertTrue(should_render_root_columns("auto", 120, 2, None, False))
        self.assertTrue(should_render_root_columns("columns", 120, 2, None, False))
        self.assertFalse(should_render_root_columns("timeline", 120, 2, None, False))
        self.assertFalse(should_render_root_columns("columns", 83, 2, None, False))
        self.assertFalse(should_render_root_columns("columns", 120, 2, 0, False))
        self.assertFalse(should_render_root_columns("columns", 120, 2, None, True))

    def test_canonical_roots_reject_missing_and_duplicate_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(canonical_watch_roots([root]), [root.resolve()])
            with self.assertRaisesRegex(SystemExit, "listed twice"):
                canonical_watch_roots([root, root / "."])
            with self.assertRaisesRegex(SystemExit, "no folder and no saved activity"):
                canonical_watch_roots([root / "missing"])

    def test_a_removed_folder_is_watchable_while_its_activity_is_recorded(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            gone = Path(directory) / "removed-worktree"
            gone.mkdir()
            resolved = gone.resolve()
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                append_event(
                    resolved,
                    {
                        "agent": "codex",
                        "kind": "commit",
                        "status": "success",
                        "title": "Commit created",
                        "detail": "work that outlived its folder",
                    },
                )
                gone.rmdir()

                self.assertEqual(canonical_watch_roots([resolved]), [resolved])

                watched = initialize_watch_root(resolved, 0.0)
                self.assertFalse(watched.present)
                self.assertEqual(len(watched.records), 1)
                self.assertIn(" · gone", watch_root_summary(watched, "removed"))

    def test_a_folder_that_comes_back_is_adopted_rather_than_announced(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = (Path(directory) / "project").resolve()
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                watched = initialize_watch_root(root, 0.0)
                self.assertFalse(watched.present)

                root.mkdir()
                (root / "app.py").write_text("print('hi')\n")
                watched.last_scan = -100.0
                watched.last_herdr_refresh = time.monotonic()
                watched.last_github_refresh = time.monotonic()
                new_events = poll_watch_root(
                    watched, time.monotonic(), 0.0, 0.0, poll_external=False
                )

                self.assertTrue(watched.present)
                self.assertEqual(new_events, 0)
                self.assertIn("app.py", watched.known_files)

    def test_a_new_test_failure_is_sent_as_a_desktop_notification(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                watched = initialize_watch_root(root, 0.0)
                append_event(
                    root,
                    {
                        "agent": "codex",
                        "kind": "test",
                        "status": "failed",
                        "title": "Tests failed",
                        "detail": "pytest",
                    },
                )
                with patch("side_dog.cli.notify_for_event") as notified:
                    poll_watch_root(
                        watched, time.monotonic(), 0.0, 0.0, poll_external=False
                    )
                notified.assert_called_once()
                label, event = notified.call_args.args
                self.assertEqual(event["kind"], "test")
                self.assertEqual(event["status"], "failed")

    def test_notify_false_sends_no_desktop_notification(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                watched = initialize_watch_root(root, 0.0)
                append_event(
                    root,
                    {
                        "agent": "codex",
                        "kind": "test",
                        "status": "failed",
                        "title": "Tests failed",
                        "detail": "pytest",
                    },
                )
                with patch("side_dog.cli.notify_for_event") as notified:
                    poll_watch_root(
                        watched,
                        time.monotonic(),
                        0.0,
                        0.0,
                        poll_external=False,
                        notify=False,
                    )
                notified.assert_not_called()

    def test_labels_prefer_pr_then_branch_and_remain_unique(self) -> None:
        states = [
            root_state(Path("/tmp/main"), [], branch="main"),
            root_state(Path("/tmp/review"), [], branch="issue-9", pr_number=9),
            root_state(Path("/tmp/other"), [], branch="main"),
        ]

        labels = watch_root_labels(states)

        self.assertEqual(labels, ["main", "PR #9", "other"])
        self.assertEqual(
            watch_root_summary(states[1], labels[1]), "PR #9 @ 1234567 OPEN CLEAN"
        )

    def test_a_merged_pull_request_does_not_report_an_unknown_merge_state(
        self,
    ) -> None:
        state = root_state(Path("/tmp/review"), [], branch="issue-9", pr_number=9)
        state.github_status = {
            **(state.github_status or {}),
            "state": "MERGED",
            "merge_state": "UNKNOWN",
        }

        self.assertEqual(
            watch_root_summary(state, "PR #9"), "PR #9 @ 1234567 MERGED"
        )

        state.github_status["state"] = "OPEN"
        state.github_status["merge_state"] = "BLOCKED"

        self.assertEqual(
            watch_root_summary(state, "PR #9"), "PR #9 @ 1234567 OPEN BLOCKED"
        )

    def test_labels_cannot_collide_with_a_natural_suffixed_name(self) -> None:
        states = [
            root_state(Path("/a/x"), [], branch="main"),
            root_state(Path("/b/x"), [], branch="main"),
            root_state(Path("/c/x:2"), []),
        ]

        labels = watch_root_labels(states)

        self.assertEqual(labels, ["x", "x:2", "x:2:2"])
        self.assertEqual(len(labels), len(set(labels)))

    def test_agent_subdirectory_normalizes_to_its_git_worktree_root(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            git_worktree_root(os.fspath(repo_root / "side_dog")),
            os.fspath(repo_root),
        )

    def test_aggregate_merges_by_time_labels_sources_and_preserves_raw_records(
        self,
    ) -> None:
        first = root_state(
            Path("/tmp/main"),
            [activity(3_000, "newest.py")],
            branch="main",
        )
        second = root_state(
            Path("/tmp/review"),
            [activity(2_000, "older.py")],
            branch="issue-9",
            pr_number=9,
        )
        states = [first, second]
        original = [deepcopy(list(state.records)) for state in states]

        records = aggregate_watch_records(states, watch_root_labels(states), None, None)

        self.assertEqual(
            [record["detail"] for record in records], ["older.py", "newest.py"]
        )
        self.assertEqual(records[0][SOURCE_LABEL], "PR #9")
        self.assertEqual(records[1][SOURCE_LABEL], "main")
        self.assertEqual(records[0][SOURCE_KEY], "/tmp/review")
        self.assertEqual([list(state.records) for state in states], original)

    def test_root_color_assignment_is_stable_and_rolls_over_predictably(self) -> None:
        states = [
            root_state(
                Path(f"/tmp/root-{index}"),
                [activity(1_000 + index, f"file-{index}.py")],
                branch=f"branch-{index}",
            )
            for index in range(len(ROOT_PALETTE) + 1)
        ]

        records = aggregate_watch_records(states, watch_root_labels(states), None, None)

        self.assertEqual(
            [record[SOURCE_COLOR_INDEX] for record in records],
            [*range(len(ROOT_PALETTE)), 0],
        )
        states[1].git_status["branch"] = "renamed"  # type: ignore[index]
        renamed = aggregate_watch_records(states, watch_root_labels(states), None, None)
        self.assertEqual(renamed[1][SOURCE_COLOR_INDEX], 1)

    def test_same_operation_id_from_different_roots_never_coalesces(self) -> None:
        states = [
            root_state(
                Path("/tmp/one"),
                [
                    activity(
                        1_000,
                        "one running",
                        kind="test",
                        agent="codex",
                        status="running",
                        operation_id="shared",
                    ),
                    activity(
                        2_000,
                        "one passed",
                        kind="test",
                        agent="codex",
                        operation_id="shared",
                    ),
                ],
                branch="one",
            ),
            root_state(
                Path("/tmp/two"),
                [
                    activity(
                        1_000,
                        "two running",
                        kind="test",
                        agent="codex",
                        status="running",
                        operation_id="shared",
                    ),
                    activity(
                        2_000,
                        "two passed",
                        kind="test",
                        agent="codex",
                        operation_id="shared",
                    ),
                ],
                branch="two",
            ),
        ]
        records = aggregate_watch_records(states, watch_root_labels(states), None, None)

        coalesced = coalesce_operations(records)

        self.assertEqual(len(coalesced), 2)
        self.assertEqual(
            {record["detail"] for record in coalesced},
            {"one passed", "two passed"},
        )

    def test_polling_one_root_does_not_advance_another_roots_state(self) -> None:
        first = root_state(Path("/tmp/one"), [], branch="one")
        second = root_state(Path("/tmp/two"), [], branch="two")
        event = activity(1_000, "one.py")

        with (
            patch("side_dog.cli.read_new_events", return_value=([event], 17)),
            patch("side_dog.cli.snapshot", return_value={}),
            patch("side_dog.cli.load_git_state", return_value=None),
            patch("side_dog.cli.load_herdr_identities", return_value={}),
        ):
            poll_watch_root(first, now=10.0, poll=0.5, github_poll=0.0)

        self.assertEqual(first.position, 17)
        self.assertEqual(list(first.records), [event])
        self.assertEqual(second.position, 0)
        self.assertEqual(list(second.records), [])

    def test_manual_branch_switch_clears_context_before_github_refresh(self) -> None:
        watched = root_state(
            Path("/tmp/one"),
            [
                activity(
                    1_000,
                    "old push",
                    kind="push",
                    agent="codex",
                    turn_id="old",
                )
            ],
            branch="old-branch",
        )
        watched.last_git_refresh = float("-inf")
        watched.last_herdr_refresh = 10.0
        verified = {
            "number": 12,
            "title": "New branch pull request",
            "state": "OPEN",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }

        with (
            patch("side_dog.cli.read_new_events", return_value=([], 0)),
            patch(
                "side_dog.cli.load_git_state",
                return_value={
                    "branch": "new-branch",
                    "oid": "abcdef1234567890",
                    "short_oid": "abcdef1",
                    "repository": "side-dog",
                },
            ),
            patch(
                "side_dog.cli.load_watch_root_external_refresh",
                return_value=WatchRootExternalRefresh(
                    identities=None,
                    github_result=(verified, None),
                    github_branch="new-branch",
                ),
            ),
            patch("side_dog.cli.append_event") as appended,
        ):
            poll_watch_root(
                watched,
                now=10.0,
                poll=0.5,
                github_poll=1.0,
                scan_files=False,
            )

        github_record = appended.call_args_list[-1].args[1]
        self.assertEqual(github_record["agent"], "github")
        self.assertNotIn("turn_id", github_record)
        self.assertTrue(watched.delivery_context_reset)

    def test_branch_switch_precedes_new_delivery_from_the_same_poll(self) -> None:
        watched = root_state(
            Path("/tmp/one"),
            [activity(1_000, "old push", kind="push", turn_id="old-turn")],
            branch="old-branch",
        )
        watched.last_git_refresh = float("-inf")
        watched.last_herdr_refresh = 10.0
        new_push = activity(
            2_000,
            "new push",
            kind="push",
            agent="codex",
            turn_id="new-turn",
        )
        branch_created = activity(
            1_900,
            "new-branch",
            kind="branch",
            agent="codex",
            title="Branch switched",
            turn_id="new-turn",
        )
        verified = {
            "number": 12,
            "title": "New branch pull request",
            "state": "OPEN",
            "branch": "new-branch",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }

        with (
            patch(
                "side_dog.cli.read_new_events",
                return_value=([branch_created, new_push], 2),
            ),
            patch(
                "side_dog.cli.load_git_state",
                return_value={
                    "branch": "new-branch",
                    "oid": "abcdef1234567890",
                    "short_oid": "abcdef1",
                    "repository": "side-dog",
                },
            ),
            patch(
                "side_dog.cli.load_watch_root_external_refresh",
                return_value=WatchRootExternalRefresh(
                    identities=None,
                    github_result=(verified, None),
                    github_branch="new-branch",
                ),
            ),
            patch("side_dog.cli.append_event") as appended,
        ):
            poll_watch_root(
                watched,
                now=10.0,
                poll=0.5,
                github_poll=1.0,
                scan_files=False,
            )

        github_record = next(
            call.args[1]
            for call in appended.call_args_list
            if call.args[1]["kind"] == "github"
        )
        branch_record = next(
            call.args[1]
            for call in appended.call_args_list
            if call.args[1]["kind"] == "branch"
        )
        self.assertEqual(github_record["turn_id"], "new-turn")
        self.assertEqual(branch_record["turn_id"], "new-turn")
        self.assertFalse(watched.delivery_context_reset)
        self.assertEqual(watched.records[-1]["turn_id"], "new-turn")

    def test_branch_creation_is_not_a_verified_checkout_boundary(self) -> None:
        branch_created = activity(
            1_900,
            "new-branch",
            kind="branch",
            agent="codex",
            title="Branch created",
            turn_id="new-turn",
        )
        new_push = activity(
            2_000,
            "new push",
            kind="push",
            agent="codex",
            turn_id="new-turn",
        )

        self.assertEqual(
            verified_post_switch_delivery_context(
                [branch_created, new_push], "new-branch"
            ),
            {},
        )

    def test_delivery_context_stops_at_a_later_branch_boundary(self) -> None:
        current_switch = activity(
            1_000,
            "current",
            kind="branch",
            title="Branch switched",
        )
        current_push = activity(
            1_100,
            "current push",
            kind="push",
            agent="codex",
            turn_id="current-turn",
        )
        away_switch = activity(
            1_200,
            "away",
            kind="branch",
            title="Branch switched",
        )
        away_push = activity(
            1_300,
            "away push",
            kind="push",
            agent="codex",
            turn_id="away-turn",
        )

        context = verified_post_switch_delivery_context(
            [current_switch, current_push, away_switch, away_push],
            "current",
        )

        self.assertEqual(context["turn_id"], "current-turn")

    def test_synchronous_refresh_uses_segmented_delivery_context(self) -> None:
        watched = root_state(Path("/tmp/one"), [], branch="old")
        watched.last_git_refresh = float("-inf")
        watched.last_herdr_refresh = 10.0
        records = [
            activity(
                1_000,
                "current",
                kind="branch",
                title="Branch switched",
            ),
            activity(
                1_100,
                "current push",
                kind="push",
                agent="codex",
                turn_id="current-turn",
            ),
            activity(
                1_200,
                "away",
                kind="branch",
                title="Branch switched",
            ),
            activity(
                1_300,
                "away push",
                kind="push",
                agent="codex",
                turn_id="away-turn",
            ),
        ]
        verified = {
            "number": 12,
            "title": "Current branch pull request",
            "state": "OPEN",
            "branch": "current",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }

        with (
            patch("side_dog.cli.read_new_events", return_value=(records, 4)),
            patch(
                "side_dog.cli.load_git_state",
                return_value={
                    "branch": "current",
                    "oid": "abcdef1234567890",
                    "short_oid": "abcdef1",
                    "repository": "side-dog",
                },
            ),
            patch(
                "side_dog.cli.load_watch_root_external_refresh",
                return_value=WatchRootExternalRefresh(
                    identities=None,
                    github_result=(verified, None),
                    github_branch="current",
                ),
            ) as refresh,
            patch("side_dog.cli.append_event") as appended,
        ):
            poll_watch_root(
                watched,
                now=10.0,
                poll=0.5,
                github_poll=1.0,
                scan_files=False,
            )

        self.assertEqual(refresh.call_args.args[-1]["turn_id"], "current-turn")
        github_record = next(
            call.args[1]
            for call in appended.call_args_list
            if call.args[1]["kind"] == "github"
        )
        self.assertEqual(github_record["turn_id"], "current-turn")

    def test_branch_switch_does_not_carry_an_unverified_old_branch_delivery(
        self,
    ) -> None:
        watched = root_state(Path("/tmp/one"), [], branch="old-branch")
        watched.last_git_refresh = float("-inf")
        watched.last_herdr_refresh = 10.0
        old_push = activity(
            2_000,
            "old push",
            kind="push",
            agent="codex",
            turn_id="old-turn",
        )
        verified = {
            "number": 12,
            "title": "New branch pull request",
            "state": "OPEN",
            "branch": "new-branch",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }

        with (
            patch("side_dog.cli.read_new_events", return_value=([old_push], 1)),
            patch(
                "side_dog.cli.load_git_state",
                return_value={
                    "branch": "new-branch",
                    "oid": "abcdef1234567890",
                    "short_oid": "abcdef1",
                    "repository": "side-dog",
                },
            ),
            patch(
                "side_dog.cli.load_watch_root_external_refresh",
                return_value=WatchRootExternalRefresh(
                    identities=None,
                    github_result=(verified, None),
                    github_branch="new-branch",
                ),
            ),
            patch("side_dog.cli.append_event") as appended,
        ):
            poll_watch_root(
                watched,
                now=10.0,
                poll=0.5,
                github_poll=1.0,
                scan_files=False,
            )

        github_record = next(
            call.args[1]
            for call in appended.call_args_list
            if call.args[1]["kind"] == "github"
        )
        branch_record = next(
            call.args[1]
            for call in appended.call_args_list
            if call.args[1]["kind"] == "branch"
        )
        self.assertNotIn("turn_id", github_record)
        self.assertNotIn("turn_id", branch_record)
        self.assertTrue(watched.delivery_context_reset)

    def test_async_refresh_carries_a_pending_branch_context_reset(self) -> None:
        watched = root_state(
            Path("/tmp/one"),
            [activity(1_000, "old push", kind="push", agent="codex", turn_id="old")],
            branch="new-branch",
        )
        watched.github_status = None
        watched.last_github_refresh = float("-inf")
        watched.last_herdr_refresh = 10.0
        watched.delivery_context_reset = True
        verified = {
            "number": 12,
            "title": "New branch pull request",
            "state": "OPEN",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }

        class ImmediateExecutor:
            submitted: tuple[object, ...] = ()

            def submit(self, function: object, *args: object) -> Future[object]:
                self.submitted = args
                future: Future[object] = Future()
                future.set_result(function(*args))  # type: ignore[operator]
                return future

        executor = ImmediateExecutor()
        pending: dict[str, Future[WatchRootExternalRefresh]] = {}
        with (
            patch("side_dog.cli.load_github_pr", return_value=(verified, None)),
            patch("side_dog.cli.append_event") as appended,
        ):
            schedule_watch_root_refreshes(
                [watched],
                now=10.0,
                github_poll=1.0,
                executor=executor,  # type: ignore[arg-type]
                pending=pending,
            )
            wait_for_watch_root_refreshes([watched], pending)

        self.assertEqual(executor.submitted[-1], {})
        github_record = appended.call_args.args[1]
        self.assertEqual(github_record["agent"], "github")
        self.assertNotIn("turn_id", github_record)

    def test_async_refresh_uses_delivery_context_present_at_completion(self) -> None:
        watched = root_state(
            Path("/tmp/one"),
            [activity(1_000, "old push", kind="push", agent="codex", turn_id="old")],
            branch="feature",
        )
        watched.github_status = None
        watched.last_github_refresh = float("-inf")
        watched.last_herdr_refresh = 10.0
        verified = {
            "number": 12,
            "title": "Current pull request",
            "state": "OPEN",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }
        future: Future[WatchRootExternalRefresh] = Future()

        class DeferredExecutor:
            submitted: tuple[object, ...] = ()

            def submit(
                self, _function: object, *args: object
            ) -> Future[WatchRootExternalRefresh]:
                self.submitted = args
                return future

        executor = DeferredExecutor()
        pending: dict[str, Future[WatchRootExternalRefresh]] = {}
        schedule_watch_root_refreshes(
            [watched],
            now=10.0,
            github_poll=1.0,
            executor=executor,  # type: ignore[arg-type]
            pending=pending,
        )
        self.assertIsNone(executor.submitted[-1])
        watched.records.append(
            activity(
                2_000,
                "new push",
                kind="push",
                agent="codex",
                turn_id="new",
            )
        )
        future.set_result(
            WatchRootExternalRefresh(
                identities=None,
                github_result=(verified, None),
                github_branch="feature",
                delivery_context=executor.submitted[-1],  # type: ignore[arg-type]
            )
        )

        with patch("side_dog.cli.append_event") as appended:
            apply_completed_watch_root_refreshes([watched], pending)

        github_record = appended.call_args.args[1]
        self.assertEqual(github_record["turn_id"], "new")

    def test_async_refresh_rechecks_a_reset_consumed_before_completion(self) -> None:
        watched = root_state(Path("/tmp/one"), [], branch="feature")
        watched.github_status = None
        watched.last_github_refresh = float("-inf")
        watched.last_herdr_refresh = 10.0
        watched.delivery_context_reset = True
        verified = {
            "number": 12,
            "title": "Current pull request",
            "state": "OPEN",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }
        future: Future[WatchRootExternalRefresh] = Future()

        class DeferredExecutor:
            submitted: tuple[object, ...] = ()

            def submit(
                self, _function: object, *args: object
            ) -> Future[WatchRootExternalRefresh]:
                self.submitted = args
                return future

        executor = DeferredExecutor()
        pending: dict[str, Future[WatchRootExternalRefresh]] = {}
        schedule_watch_root_refreshes(
            [watched],
            now=10.0,
            github_poll=1.0,
            executor=executor,  # type: ignore[arg-type]
            pending=pending,
        )
        self.assertEqual(executor.submitted[-1], {})
        watched.records.append(
            activity(
                2_000,
                "new push",
                kind="push",
                agent="codex",
                turn_id="new",
            )
        )
        watched.delivery_context_reset = False
        future.set_result(
            WatchRootExternalRefresh(
                identities=None,
                github_result=(verified, None),
                github_branch="feature",
                delivery_context=executor.submitted[-1],  # type: ignore[arg-type]
            )
        )

        with patch("side_dog.cli.append_event") as appended:
            apply_completed_watch_root_refreshes([watched], pending)

        github_record = appended.call_args.args[1]
        self.assertEqual(github_record["turn_id"], "new")

    def test_unchanged_github_state_is_attached_to_a_new_delivery(self) -> None:
        verified = {
            "number": 12,
            "title": "Current pull request",
            "state": "OPEN",
            "ci": "CI 1/1",
            "merge_state": "BLOCKED",
        }
        watched = root_state(
            Path("/tmp/one"),
            [activity(2_000, "new push", kind="push", agent="codex", turn_id="new")],
            branch="feature",
        )
        watched.github_status = verified
        watched.last_github_fingerprint = github_fingerprint(verified)
        watched.last_github_delivery_id = "old"
        refresh = WatchRootExternalRefresh(
            identities=None,
            github_result=(verified, None),
            github_branch="feature",
        )

        with patch("side_dog.cli.append_event") as appended:
            apply_watch_root_external_refresh(watched, refresh)

        github_record = appended.call_args.args[1]
        self.assertEqual(github_record["turn_id"], "new")
        self.assertEqual(watched.last_github_delivery_id, "new")

    def test_slow_external_refreshes_are_scheduled_without_waiting(self) -> None:
        states = [
            root_state(Path("/tmp/one"), [], branch="one"),
            root_state(Path("/tmp/two"), [], branch="two"),
        ]
        futures: list[Future[WatchRootExternalRefresh]] = []

        class DeferredExecutor:
            def submit(self, *_args: object) -> Future[WatchRootExternalRefresh]:
                future: Future[WatchRootExternalRefresh] = Future()
                futures.append(future)
                return future

        pending: dict[str, Future[WatchRootExternalRefresh]] = {}
        schedule_watch_root_refreshes(
            states,
            now=10.0,
            github_poll=15.0,
            executor=DeferredExecutor(),  # type: ignore[arg-type]
            pending=pending,
        )

        self.assertEqual(len(pending), 2)
        self.assertEqual([state.identities for state in states], [{}, {}])
        apply_completed_watch_root_refreshes(states, pending)
        self.assertEqual(len(pending), 2)

        futures[0].set_result(
            WatchRootExternalRefresh(
                identities={"one": {"agent": "codex", "label": "One"}},
                github_result=None,
            )
        )
        apply_completed_watch_root_refreshes(states, pending)

        self.assertEqual(states[0].identities["one"]["label"], "One")
        self.assertEqual(states[1].identities, {})
        self.assertEqual(len(pending), 1)

    def test_one_shot_wait_applies_every_initial_external_refresh(self) -> None:
        states = [
            root_state(Path("/tmp/one"), [], branch="one"),
            root_state(Path("/tmp/two"), [], branch="two"),
        ]
        first: Future[WatchRootExternalRefresh] = Future()
        second: Future[WatchRootExternalRefresh] = Future()
        first.set_result(
            WatchRootExternalRefresh(
                identities={"first": {"agent": "codex", "label": "First"}},
                github_result=None,
            )
        )
        second.set_result(
            WatchRootExternalRefresh(
                identities={"second": {"agent": "claude-code", "label": "Second"}},
                github_result=None,
            )
        )
        pending = {"/tmp/one": first, "/tmp/two": second}

        wait_for_watch_root_refreshes(states, pending)

        self.assertEqual(states[0].identities["first"]["label"], "First")
        self.assertEqual(states[1].identities["second"]["label"], "Second")
        self.assertEqual(pending, {})

    def test_completed_github_refresh_is_ignored_after_branch_switch(self) -> None:
        state = root_state(Path("/tmp/one"), [], branch="new-branch")
        refresh = WatchRootExternalRefresh(
            identities=None,
            github_result=(
                {
                    "number": 9,
                    "title": "Old branch PR",
                    "state": "OPEN",
                    "merge_state": "CLEAN",
                },
                None,
            ),
            github_branch="old-branch",
        )

        apply_watch_root_external_refresh(state, refresh)

        self.assertIsNone(state.github_status)
        self.assertEqual(list(state.records), [])

    def test_initialization_resets_github_context_after_offline_branch_switch(
        self,
    ) -> None:
        old_status = {
            "number": 9,
            "title": "Old branch PR",
            "state": "OPEN",
            "branch": "old-branch",
            "merge_state": "BLOCKED",
        }
        old_record = activity(
            1_000,
            "old pull request",
            kind="github",
            agent="github",
            turn_id="old-turn",
            github=old_status,
            github_fingerprint=github_fingerprint(old_status),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "side_dog.cli.read_new_events",
                    return_value=([old_record], 1),
                ),
                patch("side_dog.cli.snapshot", return_value={}),
                patch(
                    "side_dog.cli.load_git_state",
                    return_value={
                        "branch": "new-branch",
                        "oid": "abcdef1234567890",
                        "short_oid": "abcdef1",
                        "repository": "side-dog",
                    },
                ),
            ):
                watched = initialize_watch_root(root, 1.0)

        self.assertTrue(watched.delivery_context_reset)
        self.assertIsNone(watched.github_status)
        self.assertIsNone(watched.last_github_fingerprint)
        self.assertIsNone(watched.last_github_delivery_id)

    def test_initialization_resets_unverified_delivery_context(self) -> None:
        old_push = activity(
            1_000,
            "old push",
            kind="push",
            agent="codex",
            turn_id="old-turn",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "side_dog.cli.read_new_events",
                    return_value=([old_push], 1),
                ),
                patch("side_dog.cli.snapshot", return_value={}),
                patch(
                    "side_dog.cli.load_git_state",
                    return_value={
                        "branch": "new-branch",
                        "oid": "abcdef1234567890",
                        "short_oid": "abcdef1",
                        "repository": "side-dog",
                    },
                ),
            ):
                watched = initialize_watch_root(root, 1.0)

        self.assertTrue(watched.delivery_context_reset)
        self.assertIsNone(watched.github_status)

    def test_initialization_preserves_delivery_carried_by_current_branch_boundary(
        self,
    ) -> None:
        boundary = activity(
            2_000,
            "new-branch",
            kind="branch",
            agent="git",
            title="Branch switched",
            turn_id="new-turn",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "side_dog.cli.read_new_events",
                    return_value=([boundary], 1),
                ),
                patch("side_dog.cli.snapshot", return_value={}),
                patch(
                    "side_dog.cli.load_git_state",
                    return_value={
                        "branch": "new-branch",
                        "oid": "abcdef1234567890",
                        "short_oid": "abcdef1",
                        "repository": "side-dog",
                    },
                ),
            ):
                watched = initialize_watch_root(root, 1.0)

        self.assertFalse(watched.delivery_context_reset)
        self.assertEqual(
            latest_delivery_context(watched.records), {"turn_id": "new-turn"}
        )

    def test_new_delivery_consumes_a_startup_context_reset(self) -> None:
        watched = root_state(Path("/tmp/one"), [], branch="new-branch")
        watched.delivery_context_reset = True
        watched.github_status = None
        new_push = activity(
            2_000,
            "new push",
            kind="push",
            agent="codex",
            turn_id="new-turn",
        )
        verified = {
            "number": 12,
            "title": "New branch pull request",
            "state": "OPEN",
            "branch": "new-branch",
            "ci": "CI 1/1",
            "merge_state": "CLEAN",
        }
        with (
            patch("side_dog.cli.read_new_events", return_value=([new_push], 1)),
            patch(
                "side_dog.cli.load_git_state",
                return_value={
                    "branch": "new-branch",
                    "oid": "abcdef1234567890",
                    "short_oid": "abcdef1",
                    "repository": "side-dog",
                },
            ),
            patch(
                "side_dog.cli.load_watch_root_external_refresh",
                return_value=WatchRootExternalRefresh(
                    identities=None,
                    github_result=(verified, None),
                    github_branch="new-branch",
                    delivery_context={"turn_id": "new-turn", "agent": "codex"},
                ),
            ),
            patch("side_dog.cli.append_event") as appended,
        ):
            poll_watch_root(
                watched,
                now=10.0,
                poll=0.5,
                github_poll=1.0,
                scan_files=False,
            )

        self.assertFalse(watched.delivery_context_reset)
        github_record = appended.call_args.args[1]
        self.assertEqual(github_record["turn_id"], "new-turn")

    def test_event_identity_is_scoped_to_its_root(self) -> None:
        first = root_state(Path("/tmp/one"), [], branch="one")
        second = root_state(Path("/tmp/two"), [], branch="two")
        first.identities = {"shared-session": {"agent": "codex", "label": "First pane"}}
        second.identities = {
            "shared-session": {"agent": "codex", "label": "Second pane"}
        }
        identities = aggregate_watch_identities([first, second], None)

        first_event = activity(1_000, "one.py", session_id="shared-session")
        first_event[SOURCE_KEY] = "/tmp/one"
        second_event = activity(2_000, "two.py", session_id="shared-session")
        second_event[SOURCE_KEY] = "/tmp/two"

        self.assertEqual(
            identity_for_event(first_event, identities)["label"], "First pane"
        )
        self.assertEqual(
            identity_for_event(second_event, identities)["label"], "Second pane"
        )

    def test_agent_banners_use_their_root_labels_and_colors(self) -> None:
        first = root_state(Path("/tmp/main"), [], branch="main")
        second = root_state(Path("/tmp/review"), [], branch="review")
        first.identities = {
            "main-session": {
                "agent": "codex",
                "pane_id": "p1",
                "label": "Main task",
                "working_root": "/tmp/main",
                "status": "working",
            }
        }
        second.identities = {
            "review-session": {
                "agent": "claude-code",
                "pane_id": "p2",
                "label": "Review task",
                "working_root": "/tmp/review",
                "status": "idle",
            }
        }
        states = [first, second]
        labels = ["main", "review"]
        identities = aggregate_watch_identities(states, None, labels)

        screen = render(
            [],
            first.root,
            width=100,
            height=20,
            color=True,
            identities=identities,
            root_count=2,
        )

        self.assertNotIn("[main]", screen)
        self.assertNotIn("[review]", screen)
        self.assertGreaterEqual(screen.count(f"{root_color(0)}  {ANSI['reset']}"), 2)
        self.assertGreaterEqual(screen.count(f"{root_color(1)}  {ANSI['reset']}"), 1)
        self.assertNotIn(ROOT_NAME_INK, screen)

    def test_column_associates_agents_with_their_exact_root(self) -> None:
        first = root_state(Path("/tmp/main"), [], branch="main")
        second = root_state(Path("/tmp/other"), [], branch="other")
        identities = {
            "main": {
                "agent": "codex",
                "pane_id": "p1",
                "label": "Main agent",
                "working_root": "/tmp/main",
            },
            "other": {
                "agent": "claude-code",
                "pane_id": "p2",
                "label": "Other agent",
                "working_root": "/tmp/other",
            },
        }
        first.identities = identities
        second.identities = identities

        assignments = watch_root_column_identities([first, second])

        self.assertEqual(set(assignments[0]), {"main"})
        self.assertEqual(set(assignments[1]), {"other"})

    def test_column_assigns_nested_root_agent_to_most_specific_root(self) -> None:
        outer = root_state(Path("/tmp/repo"), [], branch="main")
        inner = root_state(Path("/tmp/repo/sub"), [], branch="feature")
        identity = {
            "agent": "codex",
            "pane_id": "p1",
            "label": "Nested agent",
            "working_root": "/tmp/repo/sub",
        }
        outer.identities = {"nested": identity}
        inner.identities = {"nested": identity}

        assignments = watch_root_column_identities([outer, inner])

        self.assertEqual(assignments[0], {})
        self.assertEqual(set(assignments[1]), {"nested"})

    def test_columns_keep_each_roots_events_in_its_own_panel(self) -> None:
        now = int(time.time() * 1000)
        first = root_state(
            Path("/tmp/main"),
            [activity(now, "main.py")],
            branch="main",
        )
        second = root_state(
            Path("/tmp/review"),
            [activity(now, "review.py")],
            branch="issue-11",
            pr_number=11,
        )
        first.identities = {
            "first": {
                "agent": "codex",
                "pane_id": "p1",
                "label": "Main task",
                "working_root": "/tmp/main",
                "status": "working",
            }
        }
        second.identities = {
            "second": {
                "agent": "claude-code",
                "pane_id": "p2",
                "label": "Review task",
                "working_root": "/tmp/review",
                "status": "idle",
            }
        }
        states = [first, second]

        screen = render_root_columns(
            states,
            watch_root_labels(states),
            None,
            width=100,
            height=16,
            color=False,
            session_filter=None,
            expanded_history=True,
            event_filter="all",
            paused=False,
            new_event_counts=None,
            newest_first=True,
            expanded_header=True,
        )

        self.assertIn(
            f"SIDE DOG v{__version__} · all 2 folders · 1 working", screen
        )
        self.assertIn("PR #11 · review", screen)
        self.assertIn("┬ PR #11 · review", screen)
        for line in screen.splitlines():
            left, right = line[:50], line[50:]
            if "main.py" in line:
                self.assertIn("main.py", left)
                self.assertNotIn("main.py", right)
            if "review.py" in line:
                self.assertNotIn("review.py", left)
                self.assertIn("review.py", right)

    def test_columns_report_paused_new_events_per_root(self) -> None:
        now = int(time.time() * 1000)
        first = root_state(Path("/tmp/main"), [activity(now, "main.py")])
        second = root_state(Path("/tmp/review"), [activity(now, "review.py")])
        paused_records = {
            "/tmp/main": list(first.records),
            "/tmp/review": list(second.records),
        }

        screen = render_root_columns(
            [first, second],
            ["main", "review"],
            paused_records,
            width=100,
            height=16,
            color=False,
            session_filter=None,
            expanded_history=True,
            event_filter="all",
            paused=True,
            new_event_counts={"/tmp/main": 3, "/tmp/review": 0},
            newest_first=True,
            show_filesystem_activity=True,
        )

        paused_headers = [line for line in screen.splitlines() if "p paused" in line]
        self.assertEqual(len(paused_headers), 1)
        self.assertIn("3 new", paused_headers[0][:50])
        self.assertIn("0 new", paused_headers[0][50:])

    def test_columns_keep_view_hints_when_filter_matches_nothing(self) -> None:
        now = int(time.time() * 1000)
        states = [
            root_state(
                Path("/tmp/main"),
                [activity(now, "main tests", kind="test", agent="codex")],
            ),
            root_state(
                Path("/tmp/review"),
                [activity(now, "review tests", kind="test", agent="codex")],
            ),
        ]

        screen = render_root_columns(
            states,
            ["main", "review"],
            None,
            width=160,
            height=12,
            color=False,
            session_filter=None,
            expanded_history=False,
            event_filter="files",
            paused=False,
            new_event_counts=None,
            newest_first=True,
        )

        self.assertEqual(screen.count("f files"), 2)
        self.assertNotIn("waiting for coding-agent activity", screen)

    def test_columns_use_terminal_cell_width_for_wide_and_combining_text(self) -> None:
        now = int(time.time() * 1000)
        first = root_state(
            Path("/tmp/main"), [activity(now, "資料/e\u0301.py")], branch="功能"
        )
        second = root_state(
            Path("/tmp/review"), [activity(now, "plain.py")], branch="review"
        )

        screen = render_root_columns(
            [first, second],
            watch_root_labels([first, second]),
            None,
            width=100,
            height=12,
            color=False,
            session_filter=None,
            expanded_history=True,
            event_filter="all",
            paused=False,
            new_event_counts=None,
            newest_first=True,
        )

        column_rows = screen.splitlines()[2:-1]
        self.assertTrue(column_rows)
        self.assertTrue(all(terminal_cell_width(line) == 100 for line in column_rows))

    def test_crop_measures_emoji_presentation_sequences_as_a_whole(self) -> None:
        cropped = crop("♥️x", 2)

        self.assertEqual(cropped, "…")
        self.assertLessEqual(terminal_cell_width(cropped), 2)

    def test_columns_count_agent_banners_synthesized_from_events(self) -> None:
        now = int(time.time() * 1000)
        codex_event = activity(
            now,
            "main.py",
            agent="codex",
            session_id="codex-session",
            model="gpt-example",
            effort="high",
        )
        states = [
            root_state(Path("/tmp/main"), [codex_event], branch="main"),
            root_state(Path("/tmp/review"), [], branch="review"),
        ]

        screen = render_root_columns(
            states,
            watch_root_labels(states),
            None,
            width=160,
            height=12,
            color=False,
            session_filter=None,
            expanded_history=True,
            event_filter="all",
            paused=False,
            new_event_counts=None,
            newest_first=True,
            expanded_header=True,
        )

        self.assertIn("Watching 2 folders · 1 agent", screen)
        self.assertIn("example/high", screen)

    def test_columns_hide_watching_and_mode_until_header_is_expanded(self) -> None:
        states = [
            root_state(Path("/tmp/main"), [], branch="main"),
            root_state(Path("/tmp/review"), [], branch="review"),
        ]
        mode = folder_discovery_mode(
            explicit_roots=True, follow_herdr=False, require_herdr=False
        )
        empty_usage = LiveUsageSnapshot(
            UsageReport("session"),
            UsageReport("session"),
            UsageBlock(detail="no block"),
        )

        compact = render_root_columns(
            states,
            watch_root_labels(states),
            None,
            width=100,
            height=12,
            color=False,
            session_filter=None,
            expanded_history=False,
            event_filter="all",
            paused=False,
            new_event_counts=None,
            newest_first=True,
            discovery_mode=mode,
            usage_report=empty_usage,
        )
        expanded = render_root_columns(
            states,
            watch_root_labels(states),
            None,
            width=100,
            height=12,
            color=False,
            session_filter=None,
            expanded_history=False,
            event_filter="all",
            paused=False,
            new_event_counts=None,
            newest_first=True,
            discovery_mode=mode,
            expanded_header=True,
            usage_report=empty_usage,
        )

        self.assertNotIn("Watching 2 folders", compact)
        self.assertNotIn("Mode: explicit folder selection", compact)
        self.assertIn("Watching 2 folders", expanded)
        self.assertIn("Mode: explicit folder selection", expanded)

    def test_columns_use_root_colors_only_in_the_left_gutter(self) -> None:
        now = int(time.time() * 1000)
        states = [
            root_state(Path("/tmp/main"), [activity(now, "main.py")], branch="main"),
            root_state(
                Path("/tmp/review"),
                [activity(now + 1, "review.py")],
                branch="review",
            ),
        ]

        screen = render_root_columns(
            states,
            watch_root_labels(states),
            None,
            width=100,
            height=16,
            color=True,
            session_filter=None,
            expanded_history=True,
            event_filter="all",
            paused=False,
            new_event_counts={"/tmp/main": 0, "/tmp/review": 0},
            newest_first=True,
            show_filesystem_activity=True,
        )

        # Folder identity uses the same gutter as timeline rows, never a badge.
        self.assertGreaterEqual(screen.count(root_color(0)), 1)
        self.assertGreaterEqual(screen.count(root_color(1)), 1)
        self.assertIn(f"{root_color(0)}  {ANSI['reset']}", screen)
        self.assertIn(f"{root_color(1)}  {ANSI['reset']}", screen)
        self.assertNotIn(ROOT_NAME_INK, screen)
        self.assertIn("main.py", screen)
        self.assertIn("review.py", screen)

    def test_claude_model_and_effort_are_read_without_session_content(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text(
                '{"type":"user","message":{"content":"private prompt"}}\n'
                '{"type":"assistant","effort":"xhigh",'
                '"message":{"model":"claude-opus-5","content":"private answer"}}\n'
            )
            with patch("side_dog.cli.claude_session_path", return_value=path):
                metadata = load_claude_metadata("session")

        self.assertEqual(
            metadata,
            {"model": "claude-opus-5", "effort": "xhigh"},
        )
        self.assertNotIn("content", metadata)

    @staticmethod
    def repository(directory: Path) -> Path:
        main = directory / "project"
        main.mkdir()
        git(main, "init", "-b", "main")
        git(main, "config", "user.email", "side-dog@example.com")
        git(main, "config", "user.name", "Side Dog")
        (main / "README.md").write_text("start\n")
        git(main, "add", "README.md")
        git(main, "commit", "-m", "start")
        return main

    def test_a_worktree_created_after_start_up_joins_straight_away(self) -> None:
        # A clock two days ahead makes every folder look quiet, so only the
        # "created since start-up" rule can admit anything here.
        later = int(time.time() * 1000) + 2 * 24 * 60 * 60 * 1000
        with TemporaryDirectory() as directory:
            main = self.repository(Path(directory))
            root = canonical_root(main)
            states = [root_state(root, [])]
            known = discovered_worktrees([root]) | {root}
            with patch("side_dog.cli.load_herdr_identities", return_value={}):
                additions, known = follow_new_worktrees(states, known, later)
                self.assertEqual(additions, [])

                branch = Path(directory) / "project-feature"
                git(main, "worktree", "add", os.fspath(branch), "-b", "feature")
                additions, known = follow_new_worktrees(states, known, later)

                self.assertEqual(additions, [canonical_root(branch)])

                states.append(root_state(canonical_root(branch), []))
                repeat, _ = follow_new_worktrees(states, known, later)
                self.assertEqual(repeat, [])

    def test_a_quiet_worktree_waits_until_something_happens_in_it(self) -> None:
        later = int(time.time() * 1000) + 2 * 24 * 60 * 60 * 1000
        with TemporaryDirectory() as directory:
            main = self.repository(Path(directory))
            for index in range(3):
                git(
                    main,
                    "worktree",
                    "add",
                    os.fspath(Path(directory) / f"project-{index}"),
                    "-b",
                    f"branch-{index}",
                )
            root = canonical_root(main)
            states = [root_state(root, [])]
            known = discovered_worktrees([root]) | {root}
            state_dir = Path(directory) / "state"
            with (
                patch("side_dog.cli.load_herdr_identities", return_value={}),
                patch.dict(os.environ, {STATE_ENV: os.fspath(state_dir)}),
            ):
                additions, known = follow_new_worktrees(states, known, later)
                self.assertEqual(additions, [])

                busy = canonical_root(Path(directory) / "project-1")
                append_event(
                    busy,
                    {
                        "agent": "codex",
                        "kind": "commit",
                        "status": "success",
                        "title": "Commit created",
                        "detail": "work in a worktree nobody was watching",
                        "epoch_ms": later,
                    },
                )
                additions, _ = follow_new_worktrees(states, known, later)

                self.assertEqual(additions, [busy])

    def test_the_busiest_worktrees_win_when_the_pane_is_full(self) -> None:
        later = int(time.time() * 1000) + 2 * 24 * 60 * 60 * 1000
        with TemporaryDirectory() as directory:
            main = self.repository(Path(directory))
            for index in range(4):
                git(
                    main,
                    "worktree",
                    "add",
                    os.fspath(Path(directory) / f"project-{index}"),
                    "-b",
                    f"branch-{index}",
                )
            root = canonical_root(main)
            states = [root_state(root, [])]
            known = discovered_worktrees([root]) | {root}
            state_dir = Path(directory) / "state"
            with (
                patch("side_dog.cli.load_herdr_identities", return_value={}),
                patch.dict(os.environ, {STATE_ENV: os.fspath(state_dir)}),
            ):
                for index in range(4):
                    append_event(
                        canonical_root(Path(directory) / f"project-{index}"),
                        {
                            "agent": "codex",
                            "kind": "commit",
                            "status": "success",
                            "title": "Commit created",
                            "detail": f"work {index}",
                            "epoch_ms": later - (4 - index) * 1000,
                        },
                    )
                additions, _ = follow_new_worktrees(states, known, later, limit=3)

            self.assertEqual(
                additions,
                [
                    canonical_root(Path(directory) / "project-3"),
                    canonical_root(Path(directory) / "project-2"),
                ],
            )

    def test_a_finished_worktree_is_not_adopted_and_is_retired(self) -> None:
        now = int(time.time() * 1000)
        with TemporaryDirectory() as directory:
            main = self.repository(Path(directory))
            branch = Path(directory) / "project-landed"
            git(main, "worktree", "add", os.fspath(branch), "-b", "landed")
            landed = canonical_root(branch)
            root = canonical_root(main)
            state_dir = Path(directory) / "state"
            with (
                patch("side_dog.cli.load_herdr_identities", return_value={}),
                patch.dict(os.environ, {STATE_ENV: os.fspath(state_dir)}),
            ):
                # A fresh worktree with a recent commit is busy.
                self.assertEqual(busy_worktrees([root], now, 8), [landed])

                append_event(
                    landed,
                    {
                        "agent": "github",
                        "kind": "github",
                        "status": "success",
                        "title": "PR #7 merged",
                        "detail": "landed",
                        "github": {"number": 7, "state": "MERGED"},
                    },
                )

                # Once its pull request lands it is finished, however recent.
                self.assertTrue(folder_is_finished(landed))
                self.assertEqual(busy_worktrees([root], now, 8), [])

                states = [root_state(root, []), root_state(landed, [])]
                self.assertEqual(
                    retired_worktrees(states, {root}, set()), [landed]
                )
                # A folder named on the command line is never retired.
                self.assertEqual(
                    retired_worktrees(states, {root, landed}, set()), []
                )
                # Nor is one an agent is sitting in.
                self.assertEqual(
                    retired_worktrees(states, {root}, {landed}), []
                )

    def test_an_empty_folder_does_not_take_half_the_pane(self) -> None:
        now = int(time.time() * 1000)
        busy = root_state(Path("/tmp/busy"), [activity(now, "app.py")], branch="main")
        fresh = root_state(Path("/tmp/fresh-worktree"), [], branch="feature")

        # A worktree is adopted the moment it appears, before its agent writes
        # anything. Collecting from it is right; giving it a column is not.
        self.assertEqual(folders_worth_a_column([busy, fresh]), [0])
        self.assertFalse(
            should_render_root_columns(
                "auto", 200, len(folders_worth_a_column([busy, fresh])), None, False
            )
        )
        self.assertTrue(
            should_render_root_columns(
                "auto", 200, len(folders_worth_a_column([busy, busy])), None, False
            )
        )

    def test_worktree_paths_are_empty_outside_a_repository(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertEqual(git_worktree_paths(Path(directory)), [])

    def test_claude_model_is_read_when_the_session_file_arrives_late(self) -> None:
        session_id = "2c3b0f01-f40a-4b82-ae77-459d9098132a"
        with TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / ".claude" / "projects" / "-src-side-dog"
            project.mkdir(parents=True)
            clear_session_path_cache()
            CLAUDE_METADATA_CACHE.clear()
            try:
                with (
                    patch.dict(os.environ, {"HOME": os.fspath(home)}),
                    patch("side_dog.cli.SESSION_PATH_RETRY_SECONDS", 0.0),
                ):
                    self.assertEqual(load_claude_metadata(session_id), {})
                    (project / f"{session_id}.jsonl").write_text(
                        '{"type":"assistant","effort":"xhigh",'
                        '"message":{"model":"claude-opus-5"}}\n'
                    )
                    self.assertEqual(
                        load_claude_metadata(session_id),
                        {"model": "claude-opus-5", "effort": "xhigh"},
                    )
            finally:
                clear_session_path_cache()
                CLAUDE_METADATA_CACHE.clear()

    def test_claude_model_survives_a_half_written_transcript_line(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            complete = (
                '{"type":"assistant","effort":"xhigh",'
                '"message":{"model":"claude-opus-5"}}\n'
            )
            torn = '{"type":"assistant","effort":"high","message":{"model":"clau'
            path.write_text(complete + torn)
            CLAUDE_METADATA_CACHE.clear()
            try:
                with patch("side_dog.cli.claude_session_path", return_value=path):
                    self.assertEqual(
                        load_claude_metadata("session"),
                        {"model": "claude-opus-5", "effort": "xhigh"},
                    )
                    path.write_text(
                        complete
                        + '{"type":"assistant","effort":"high",'
                        '"message":{"model":"claude-fable-5"}}\n'
                    )
                    self.assertEqual(
                        load_claude_metadata("session"),
                        {"model": "claude-fable-5", "effort": "high"},
                    )
            finally:
                CLAUDE_METADATA_CACHE.clear()

    def test_agent_banner_distinguishes_claude_sessions_by_task(self) -> None:
        lines = render_context_banners(
            {
                "first": {
                    "agent": "claude-code",
                    "pane_id": "w3:p2",
                    "label": "Issue 2107 review",
                    "model": "claude-fable-5",
                    "effort": "high",
                    "status": "idle",
                },
                "second": {
                    "agent": "claude-code",
                    "pane_id": "w6:p1",
                    "label": "Local CI runners",
                    "model": "claude-opus-5",
                    "effort": "xhigh",
                    "status": "idle",
                },
            },
            None,
            120,
            False,
        )

        self.assertEqual(len(lines), 2)
        lines = [line.strip() for line in lines]
        self.assertIn("Claude · Issue 2107 review · fable-5/high · ○ idle", lines)
        self.assertIn("Claude · Local CI runners · opus-5/xhigh · ○ idle", lines)

    def test_agent_banner_names_working_folder_and_compacts_medium_effort(self) -> None:
        line = render_agent_context_text(
            {
                "agent": "codex",
                "label": "side-dog",
                "model": "gpt-5.6-sol",
                "effort": "medium",
                "working_root": "/tmp/worktrees/side-dog-codex-issue-73",
                "status": "working",
            },
            120,
        )

        self.assertEqual(
            line.strip(),
            "Codex · side-dog · 5.6-sol/med · "
            "/tmp/worktrees/side-dog-codex-issue-73 · … working",
        )

    def test_agent_banner_keeps_gpt_prefix_for_non_codex_agent(self) -> None:
        line = render_agent_context_text(
            {
                "agent": "opencode",
                "label": "OpenCode",
                "model": "gpt-5",
                "effort": "medium",
                "working_root": "/tmp/project",
                "status": "working",
            },
            120,
        )

        self.assertIn("Opencode · gpt-5/med · /tmp/project · … working", line)

    def test_narrow_agent_banner_keeps_worktree_tail_and_terminal_width(self) -> None:
        line = render_agent_context_text(
            {
                "agent": "claude-code",
                "label": "A deliberately long task description that cannot fit",
                "model": "claude-fable-5-1",
                "effort": "medium",
                "working_root": "/Users/example/src/side-dog-codex-issue-73",
                "status": "idle",
            },
            68,
        )

        self.assertLessEqual(terminal_cell_width(line), 68)
        self.assertIn("Claude · fable-5-1/med", line)
        self.assertIn("…", line)
        self.assertIn("/side-dog-codex-issue-73", line)
        self.assertTrue(line.endswith(" · ○ idle"), line)

    def test_agent_banner_separates_identity_from_semantic_status_color(self) -> None:
        lines = render_context_banners(
            {
                "working": {
                    "agent": "codex",
                    "label": "side-dog",
                    "status": "working",
                },
                "blocked": {
                    "agent": "claude-code",
                    "label": "review",
                    "status": "blocked",
                },
            },
            None,
            100,
            True,
        )

        rendered = "\n".join(lines)
        self.assertIn(f"{ANSI['magenta']}{ANSI['bold']}Codex", rendered)
        self.assertIn(f"{ANSI['yellow']}… working", rendered)
        self.assertIn(f"{ANSI['red']}× blocked", rendered)

    def test_render_combines_roots_without_a_branch_inventory_header(
        self,
    ) -> None:
        now = int(time.time() * 1000)
        states = [
            root_state(
                Path("/tmp/main"),
                [activity(now - 1_000, "main.py")],
                branch="main",
            ),
            root_state(
                Path("/tmp/review"),
                [
                    activity(
                        now,
                        "review tests",
                        kind="test",
                        agent="codex",
                    )
                ],
                branch="issue-9",
                pr_number=9,
            ),
        ]
        labels = watch_root_labels(states)
        records = aggregate_watch_records(states, labels, None, None)

        screen = render(
            records,
            states[0].root,
            width=110,
            height=24,
            color=False,
            expanded_history=True,
            root_count=2,
            expanded_header=True,
            show_filesystem_activity=True,
        )

        self.assertIn(
            f"SIDE DOG v{__version__} · all 2 folders · 0 working", screen
        )
        self.assertIn("Watching 2 folders · 0 agents", screen)
        self.assertNotIn("main @ 1234567", screen)
        self.assertNotIn("PR #9 @ 1234567 OPEN CLEAN", screen)
        self.assertIn("[main]", screen)
        self.assertIn("[PR #9]", screen)
        self.assertLess(screen.index("review tests"), screen.index("main.py"))

    def test_multi_root_ansi_colors_matching_root_names_and_source_badges(self) -> None:
        now = int(time.time() * 1000)
        states = [
            root_state(
                Path("/tmp/main"),
                [activity(now - 2_000, "main.py")],
                branch="main",
            ),
            root_state(
                Path("/tmp/review"),
                [
                    activity(now - 1_000, "one.py"),
                    activity(now, "two.py"),
                ],
                branch="review",
            ),
        ]
        labels = watch_root_labels(states)
        records = aggregate_watch_records(states, labels, None, None)

        screen = render(
            records,
            states[0].root,
            width=100,
            height=24,
            color=True,
            root_count=2,
            show_filesystem_activity=True,
        )

        self.assertGreaterEqual(screen.count(root_color(0)), 1)
        self.assertGreaterEqual(screen.count(root_color(1)), 1)
        self.assertIn("all 2 folders", screen.splitlines()[0])
        self.assertIn(
            f"{root_color(0)}{ROOT_NAME_INK}{ANSI['bold']}[main]", screen
        )
        self.assertIn(
            f"{root_color(1)}{ROOT_NAME_INK}{ANSI['bold']}[review]", screen
        )
        self.assertNotIn(f"{root_color(0)} {ANSI['reset']}", screen)
        self.assertNotIn(f"{root_color(1)} {ANSI['reset']}", screen)

    def test_root_badge_preserves_semantic_foreground_without_strip(self) -> None:
        event = activity(
            int(time.time() * 1000),
            "failed test",
            kind="test",
            agent="codex",
            status="failed",
        )
        event[SOURCE_LABEL] = "review"
        event[SOURCE_COLOR_INDEX] = 1

        lines, _ = render_timeline_activity(
            [event],
            line_budget=4,
            width=80,
            color=True,
            now_ms=int(time.time() * 1000),
            identities={},
            expanded_history=True,
            event_filter="all",
        )

        rendered = "\n".join(lines)
        event_line = next(line for line in lines if "failed test" in line)
        self.assertIn(root_color(1), rendered)
        self.assertIn(ANSI["red"], rendered)
        self.assertTrue(
            event_line.startswith(f"{root_color(1)}  {ANSI['reset']}"), event_line
        )
        self.assertNotIn(f"{root_color(1)} {ANSI['reset']}", event_line)

    def test_no_color_output_has_labels_without_ansi(self) -> None:
        now = int(time.time() * 1000)
        states = [
            root_state(Path("/tmp/main"), [activity(now, "main.py")], branch="main"),
            root_state(
                Path("/tmp/review"),
                [activity(now + 1, "review.py")],
                branch="review",
            ),
        ]
        labels = watch_root_labels(states)
        records = aggregate_watch_records(states, labels, None, None)

        screen = render(
            records,
            states[0].root,
            width=100,
            height=20,
            color=False,
            root_count=2,
            show_filesystem_activity=True,
        )

        self.assertNotIn("\x1b[", screen)
        self.assertIn("[main]", screen)
        self.assertIn("[review]", screen)

    def test_narrow_milestone_retains_its_root_label(self) -> None:
        milestone = activity(
            2_000,
            "abc1234 long commit subject",
            kind="commit",
            agent="git",
        )
        milestone[SOURCE_LABEL] = "PR #9"

        line = render_milestone_card(milestone, 32, False, 2_000, {})[0]

        self.assertIn("[PR #9]", line)
        self.assertLessEqual(len(line), 32)

    def test_focus_controls_select_all_cycle_and_jump(self) -> None:
        self.assertEqual(root_focus_for_key(b"\t", None, 3), 0)
        self.assertEqual(root_focus_for_key(b"\t", 0, 3), 1)
        self.assertEqual(root_focus_for_key(b"3", 1, 3), 2)
        self.assertEqual(root_focus_for_key(b"9", 1, 3), 1)
        self.assertIsNone(root_focus_for_key(b"a", 2, 3))

    def test_focused_view_keeps_collectors_separate_and_help_lists_controls(
        self,
    ) -> None:
        states = [
            root_state(Path("/tmp/main"), [activity(1_000, "main.py")], branch="main"),
            root_state(
                Path("/tmp/review"),
                [activity(2_000, "review.py")],
                branch="issue-9",
                pr_number=9,
            ),
        ]
        labels = watch_root_labels(states)
        focused = aggregate_watch_records(states, labels, None, 1)

        screen = render(
            focused,
            states[1].root,
            width=90,
            height=26,
            color=False,
            show_help=True,
            root_count=2,
            focused_root_label="PR #9",
            expanded_header=True,
        )

        self.assertNotIn("main.py", repr(focused))
        self.assertIn("review.py", repr(focused))
        self.assertIn(" · review · 0 working", screen.splitlines()[0])
        self.assertIn("Watching PR #9 · 1 of 2 folders", screen)
        self.assertIn("Views (default: auto)", screen)
        self.assertIn(
            "All     wide pane: a column per folder; narrow: one list", screen
        )
        self.assertIn("Focus   one folder fills the pane", screen)
        self.assertIn("a       show all folders again", screen)
        self.assertIn("Tab     move to the next folder", screen)
        self.assertIn("1-2     jump to a folder by position", screen)
        self.assertIn("--layout auto|columns|timeline", screen)

    def dated_repository(self, directory: Path, name: str, when: str = "") -> Path:
        main = directory / name
        main.mkdir()
        git(main, "init", "-b", "main")
        git(main, "config", "user.email", "side-dog@example.com")
        git(main, "config", "user.name", "Side Dog")
        (main / "README.md").write_text("start\n")
        git(main, "add", "README.md")
        environment = dict(os.environ)
        if when:
            environment["GIT_AUTHOR_DATE"] = when
            environment["GIT_COMMITTER_DATE"] = when
        subprocess.run(
            ["git", "commit", "-m", "start"],
            cwd=main,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return main

    def test_a_branch_name_two_repositories_share_is_not_confused(self) -> None:
        now = int(time.time() * 1000)
        with TemporaryDirectory() as directory:
            base = Path(directory)
            # Both repositories have a branch called "shared". Only one of them
            # committed to it today.
            quiet = self.dated_repository(base, "quiet", "2020-01-01T00:00:00 +0000")
            busy = self.dated_repository(base, "busy")
            git(quiet, "worktree", "add", os.fspath(base / "quiet-shared"), "-b", "shared")
            git(busy, "worktree", "add", os.fspath(base / "busy-shared"), "-b", "shared")
            watched = [canonical_root(quiet), canonical_root(busy)]

            with (
                patch("side_dog.cli.load_herdr_identities", return_value={}),
                patch.dict(os.environ, {STATE_ENV: os.fspath(base / "state")}),
            ):
                busy_folders = busy_worktrees(watched, now, 8)

            self.assertEqual(busy_folders, [canonical_root(base / "busy-shared")])

    def test_a_quick_folder_does_not_wait_out_a_slow_one(self) -> None:
        slow = root_state(Path("/tmp/slow"), [])
        slow.last_scan = 10.0
        slow.scan_seconds = 3.0  # swept every 30 seconds
        quick = root_state(Path("/tmp/quick"), [])
        quick.last_scan = 11.0
        quick.scan_seconds = 0.01  # swept every half second

        # The slow folder was scanned longest ago but is nowhere near due.
        self.assertIs(folder_due_for_scan([slow, quick], 12.0, 0.0), quick)

        quick.last_scan = 12.0
        self.assertIsNone(folder_due_for_scan([slow, quick], 12.1, 0.0))

        # The slow folder still gets its turn once its own interval is up.
        quick.last_scan = 41.0
        self.assertIs(folder_due_for_scan([slow, quick], 41.1, 0.0), slow)

    def test_narrow_status_bar_keeps_name_version_and_clock(self) -> None:
        screen = render(
            [],
            Path("/tmp/main"),
            width=42,
            height=12,
            color=True,
            root_count=10,
        )

        header = screen.splitlines()[0]
        plain = ANSI_ESCAPE.sub("", header)
        self.assertIn(f"SIDE DOG v{__version__}", plain)
        self.assertIn("all 10 folders", plain)
        self.assertNotIn("working", plain)
        self.assertRegex(plain, r"\d\d:\d\d:\d\d$")
        self.assertEqual(terminal_cell_width(ANSI_ESCAPE.sub("", header)), 42)

    def test_focused_header_uses_terminal_cells_for_wide_labels(self) -> None:
        screen = render(
            [],
            Path("/tmp/功能"),
            width=55,
            height=12,
            color=True,
            root_count=2,
            focused_root_label="PR #115",
        )

        header = screen.splitlines()[0]
        self.assertIn(" · 功能 · 0 working", ANSI_ESCAPE.sub("", header))
        self.assertEqual(terminal_cell_width(ANSI_ESCAPE.sub("", header)), 55)


def git_repository(directory: str, name: str = "project") -> Path:
    root = (Path(directory) / name).resolve()
    root.mkdir(parents=True)
    git(root, "init", "--quiet", ".")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Side Dog Test")
    return root


class GitBackedSnapshotTest(TestCase):
    def test_deleting_a_file_that_was_clean_is_announced_once(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = git_repository(directory)
            (root / "app.py").write_text("print('hi')\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "first")

            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                watched = initialize_watch_root(root, 0.0)
                # Nothing differs from the last commit, so git names nothing.
                self.assertEqual(watched.known_files, {})

                (root / "app.py").unlink()
                for _ in range(2):
                    watched.last_scan = -100.0
                    watched.last_herdr_refresh = time.monotonic()
                    watched.last_github_refresh = time.monotonic()
                    poll_watch_root(
                        watched, time.monotonic(), 0.0, 0.0, poll_external=False
                    )

                removals = [
                    record
                    for record in latest_events(events_path(root))
                    if record.get("title") == "File removed"
                ]
                self.assertEqual([record["detail"] for record in removals], ["app.py"])

    def test_watching_a_subfolder_names_files_from_that_subfolder(self) -> None:
        with TemporaryDirectory() as directory:
            root = git_repository(directory)
            (root / "sub").mkdir()
            (root / "sub" / "app.py").write_text("print('hi')\n")
            (root / "top.txt").write_text("outside\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "first")
            (root / "sub" / "app.py").write_text("print('there')\n")
            (root / "top.txt").write_text("changed outside\n")

            names = snapshot(root / "sub")

            self.assertEqual(list(names), ["app.py"])

    def test_a_filename_git_cannot_spell_does_not_stop_the_watcher(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            listed = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b" M caf\xe9.py\x00?? plain.py\x00",
            )

            with patch("side_dog.cli.subprocess.run", return_value=listed):
                paths = git_changed_paths(root)

            self.assertEqual(paths, [os.fsdecode(b"caf\xe9.py"), "plain.py"])

    def test_putting_a_file_back_the_way_it_was_still_counts_as_a_write(self) -> None:
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            root = git_repository(directory)
            (root / "app.py").write_text("print('hi')\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "first")

            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                watched = initialize_watch_root(root, 0.0)

                # Sweeps run three seconds apart on this clock. A file change
                # inside two seconds of the last one is taken for the hook and
                # the sweep reporting the same write, and only one is kept, so
                # every write here gets a sweep of its own and one to spare.
                clock = time.monotonic()

                def sweep() -> None:
                    nonlocal clock
                    clock += 3.0
                    watched.last_scan = -100.0
                    watched.last_herdr_refresh = clock
                    watched.last_github_refresh = clock
                    poll_watch_root(watched, clock, 0.0, 0.0, poll_external=False)

                def changes() -> list[str]:
                    return [
                        str(record["detail"])
                        for record in latest_events(events_path(root))
                        if record.get("title") == "File changed"
                    ]

                (root / "app.py").write_text("print('there')\n")
                sweep()
                sweep()
                self.assertEqual(changes(), ["app.py"])

                # Undoing the edit leaves the file exactly as the commit had
                # it, so git stops naming it - but it was still written.
                (root / "app.py").write_text("print('hi')\n")
                sweep()
                sweep()
                self.assertEqual(changes(), ["app.py", "app.py"])

                # Committing an edit is not a write, and is not announced.
                (root / "app.py").write_text("print('again')\n")
                sweep()
                sweep()
                self.assertEqual(len(changes()), 3)
                git(root, "commit", "--quiet", "-am", "second")
                sweep()
                self.assertEqual(len(changes()), 3)

    def test_an_ignored_file_is_watched_but_an_ignored_folder_is_not(self) -> None:
        with TemporaryDirectory() as directory:
            root = git_repository(directory)
            (root / ".gitignore").write_text(".env\nbuilt/\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "first")
            (root / ".env").write_text("TOKEN=1\n")
            (root / "built").mkdir()
            (root / "built" / "big.js").write_text("generated\n")

            names = snapshot(root)

            self.assertIn(".env", names)
            self.assertNotIn("built/", names)
            self.assertNotIn("built/big.js", names)

    def test_a_folder_name_with_spaces_still_finds_its_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = git_repository(directory)
            spaced = root / " sub "
            spaced.mkdir()
            (spaced / "app.py").write_text("print('hi')\n")
            git(root, "add", "-A")
            git(root, "commit", "--quiet", "-m", "first")
            (spaced / "app.py").write_text("print('there')\n")

            self.assertEqual(list(snapshot(spaced)), ["app.py"])
