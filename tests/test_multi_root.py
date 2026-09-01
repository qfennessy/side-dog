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

from side_dog.cli import (
    ANSI,
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
    clear_session_path_cache,
    crop,
    discovered_worktrees,
    follow_new_worktrees,
    git_worktree_paths,
    git_worktree_root,
    load_claude_metadata,
    poll_watch_root,
    render,
    render_context_banners,
    render_milestone_card,
    render_root_summaries,
    render_root_columns,
    render_timeline_activity,
    root_color,
    root_column_widths,
    root_focus_for_key,
    schedule_watch_root_refreshes,
    should_render_root_columns,
    terminal_cell_width,
    wait_for_watch_root_refreshes,
    watch_root_column_identities,
    watch_root_labels,
    watch_root_activity_state,
    watch_root_summary,
)
from side_dog.model import (
    SOURCE_KEY,
    SOURCE_LABEL,
    coalesce_operations,
    identity_for_event,
)


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

        self.assertEqual(parser.parse_args(["watch"]).projects, ["."])
        self.assertEqual(parser.parse_args(["watch"]).layout, "auto")
        self.assertEqual(parser.parse_args(["watch", "one"]).projects, ["one"])
        self.assertEqual(
            parser.parse_args(["watch", "one", "two"]).projects,
            ["one", "two"],
        )
        self.assertEqual(
            parser.parse_args(["watch", "one", "two", "--layout", "columns"]).layout,
            "columns",
        )

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

    def test_the_header_names_live_roots_and_counts_the_quiet_ones(self) -> None:
        summaries = (
            "main @ f37ef95",
            *(f"PR #{number} @ 42a9cdc MERGED" for number in range(10)),
        )
        activity = ("working", *("inactive",) * 10)

        line = render_root_summaries(summaries, activity, 60, False)

        named = line.count("@")
        self.assertIn("main @ f37ef95", line)
        self.assertLessEqual(len(line), 60)
        self.assertLess(named, 11)
        self.assertIn(f"+{11 - named} quiet", line)

    def test_a_working_root_is_named_before_a_merged_one(self) -> None:
        summaries = (
            "PR #1 @ 1111111 MERGED",
            "app @ 2222222",
            "PR #2 @ 3333333 MERGED",
        )
        activity = ("inactive", "working", "inactive")

        line = render_root_summaries(summaries, activity, 32, False)

        self.assertIn("app @ 2222222", line)
        self.assertIn("+2 quiet", line)

    def test_a_summary_line_that_fits_keeps_every_root_and_says_nothing_more(
        self,
    ) -> None:
        summaries = ("main @ 1111111", "PR #2 @ 2222222 OPEN CLEAN")
        activity = ("working", "unknown")

        line = render_root_summaries(summaries, activity, 80, False)

        self.assertEqual(line, " main @ 1111111 · PR #2 @ 2222222 OPEN CLEAN")
        self.assertNotIn("quiet", line)

    def test_labels_cannot_collide_with_a_natural_suffixed_name(self) -> None:
        states = [
            root_state(Path("/a/x"), [], branch="main"),
            root_state(Path("/b/x"), [], branch="main"),
            root_state(Path("/c/x:2"), []),
        ]

        labels = watch_root_labels(states)

        self.assertEqual(labels, ["x", "x:2", "x:2:2"])
        self.assertEqual(len(labels), len(set(labels)))

    def test_root_activity_state_uses_working_then_inactive_then_unknown(self) -> None:
        state = root_state(Path("/tmp/main"), [], branch="main")
        self.assertEqual(watch_root_activity_state(state), "unknown")

        state.identities = {
            "idle": {
                "agent": "codex",
                "root": "/tmp/main",
                "pane_id": "one",
                "status": "idle",
            },
            "done": {
                "agent": "claude-code",
                "root": "/tmp/main",
                "pane_id": "two",
                "status": "done",
            },
            "foreign-working": {
                "agent": "codex",
                "root": "/tmp/other",
                "pane_id": "foreign",
                "status": "working",
            },
        }
        self.assertEqual(watch_root_activity_state(state), "inactive")

        state.identities["working"] = {
            "agent": "codex",
            "root": "/tmp/main",
            "pane_id": "three",
            "status": "working",
        }
        self.assertEqual(watch_root_activity_state(state), "working")

        state.identities = {
            "unknown": {
                "agent": "codex",
                "pane_id": "four",
                "status": "unknown",
            }
        }
        self.assertEqual(watch_root_activity_state(state), "unknown")

    def test_agent_subdirectory_normalizes_to_its_git_worktree_root(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            git_worktree_root(os.fspath(repo_root / "side_dog")),
            os.fspath(repo_root),
        )

    def test_root_summary_emphasis_is_header_only_and_color_optional(self) -> None:
        summaries = ("main @ 1234567", "issue-13 @ 7654321", "unknown")
        states = ("inactive", "working", "unknown")

        colored = render_root_summaries(summaries, states, 100, True)

        self.assertIn(f"{ANSI['dim']}main @ 1234567{ANSI['reset']}", colored)
        self.assertIn(f"{ANSI['bold']}issue-13 @ 7654321{ANSI['reset']}", colored)
        self.assertNotIn(f"{ANSI['dim']}unknown", colored)
        self.assertNotIn(f"{ANSI['bold']}unknown", colored)

        plain = render_root_summaries(summaries, states, 100, False)
        self.assertEqual(
            plain,
            " main @ 1234567 · issue-13 @ 7654321 · unknown",
        )
        self.assertNotIn("\x1b", plain)

        screen = render(
            [activity(int(time.time() * 1000), "main.py")],
            Path("/tmp/main"),
            width=100,
            height=20,
            color=True,
            expanded_history=True,
            root_count=3,
            root_summaries=summaries,
            root_activity_states=states,
        )
        event_line = next(line for line in screen.splitlines() if "main.py" in line)
        self.assertNotIn(f"{ANSI['bold']}[", event_line)
        self.assertNotIn(f"{ANSI['dim']}[", event_line)

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
            root_summaries=("main", "review"),
            root_summary_color_indexes=(0, 1),
        )

        self.assertIn("[main]", screen)
        self.assertIn("[review]", screen)
        self.assertIn(
            f"{root_color(0)}{ROOT_NAME_INK}{ANSI['bold']}[main]", screen
        )
        self.assertIn(
            f"{root_color(1)}{ROOT_NAME_INK}{ANSI['bold']}[review]", screen
        )

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
        )

        self.assertIn("SIDE DOG  several folders · columns", screen)
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
        )

        paused_headers = [line for line in screen.splitlines() if "PAUSED" in line]
        self.assertEqual(len(paused_headers), 1)
        self.assertIn("3 new", paused_headers[0][:50])
        self.assertIn("0 new", paused_headers[0][50:])

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

        self.assertIn("Watching 2 folders · 1 agent", screen)
        self.assertIn("gpt-example · high", screen)

    def test_columns_attach_root_colors_to_names_without_detached_strips(self) -> None:
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
        )

        # One badge plus the tinted left edge on each of that root's lines.
        self.assertGreaterEqual(screen.count(root_color(0)), 1)
        self.assertGreaterEqual(screen.count(root_color(1)), 1)
        self.assertIn(
            f"{root_color(0)}{ROOT_NAME_INK}{ANSI['bold']}main", screen
        )
        self.assertIn(
            f"{root_color(1)}{ROOT_NAME_INK}{ANSI['bold']}review", screen
        )
        self.assertNotIn(f"{root_color(0)} {ANSI['reset']}", screen)
        self.assertNotIn(f"{root_color(1)} {ANSI['reset']}", screen)
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
        self.assertIn("Claude · Issue 2107 review · fable-5 · high · idle", lines)
        self.assertIn("Claude · Local CI runners · opus-5 · xhigh · idle", lines)

    def test_render_combines_roots_with_header_summaries_and_source_labels(
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
            root_summaries=tuple(
                watch_root_summary(state, label)
                for state, label in zip(states, labels, strict=True)
            ),
        )

        self.assertIn("SIDE DOG  several folders", screen)
        self.assertIn("Watching 2 folders · 0 agents", screen)
        self.assertIn("main @ 1234567", screen)
        self.assertIn("PR #9 @ 1234567 OPEN CLEAN", screen)
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
            root_summaries=tuple(
                watch_root_summary(state, label)
                for state, label in zip(states, labels, strict=True)
            ),
            root_summary_color_indexes=(0, 1),
        )

        self.assertGreaterEqual(screen.count(root_color(0)), 2)
        self.assertGreaterEqual(screen.count(root_color(1)), 2)
        self.assertIn(
            f"{root_color(0)}{ROOT_NAME_INK}{ANSI['bold']}main", screen
        )
        self.assertIn(
            f"{root_color(1)}{ROOT_NAME_INK}{ANSI['bold']}review", screen
        )
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
            root_summaries=("main", "review"),
            root_summary_color_indexes=(0, 1),
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
            height=24,
            color=False,
            show_help=True,
            root_count=2,
            focused_root_label="PR #9",
            root_summaries=(watch_root_summary(states[1], labels[1]),),
        )

        self.assertNotIn("main.py", repr(focused))
        self.assertIn("review.py", repr(focused))
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
