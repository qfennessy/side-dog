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
    SOURCE_KEY,
    SOURCE_LABEL,
    WatchRootExternalRefresh,
    WatchRootState,
    apply_completed_watch_root_refreshes,
    apply_watch_root_external_refresh,
    aggregate_watch_identities,
    aggregate_watch_records,
    build_parser,
    canonical_watch_roots,
    coalesce_operations,
    identity_for_event,
    load_claude_metadata,
    poll_watch_root,
    render,
    render_context_banners,
    render_milestone_card,
    render_root_summaries,
    root_focus_for_key,
    schedule_watch_root_refreshes,
    wait_for_watch_root_refreshes,
    watch_root_labels,
    watch_root_activity_state,
    watch_root_summary,
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
        self.assertEqual(parser.parse_args(["watch", "one"]).projects, ["one"])
        self.assertEqual(
            parser.parse_args(["watch", "one", "two"]).projects,
            ["one", "two"],
        )

    def test_canonical_roots_reject_missing_and_duplicate_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(canonical_watch_roots([root]), [root.resolve()])
            with self.assertRaisesRegex(SystemExit, "duplicate project root"):
                canonical_watch_roots([root, root / "."])
            with self.assertRaisesRegex(SystemExit, "project does not exist"):
                canonical_watch_roots([root / "missing"])

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
        self.assertIn(
            "Claude · Issue 2107 review · claude-fable-5 · high · idle", lines
        )
        self.assertIn("Claude · Local CI runners · claude-opus-5 · xhigh · idle", lines)

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

        self.assertIn("SIDE DOG  multi-root", screen)
        self.assertIn("Watching 2 roots · 0 agents", screen)
        self.assertIn("main @ 1234567", screen)
        self.assertIn("PR #9 @ 1234567 OPEN CLEAN", screen)
        self.assertIn("[main]", screen)
        self.assertIn("[PR #9]", screen)
        self.assertLess(screen.index("review tests"), screen.index("main.py"))

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
        self.assertIn("Watching PR #9 · 1 of 2 roots", screen)
        self.assertIn("a       show all watched roots", screen)
        self.assertIn("Tab     cycle the focused root", screen)
        self.assertIn("1-2     focus a root by position", screen)
