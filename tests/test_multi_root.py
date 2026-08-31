import time
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    SOURCE_KEY,
    SOURCE_LABEL,
    WatchRootState,
    aggregate_watch_identities,
    aggregate_watch_records,
    build_parser,
    canonical_watch_roots,
    coalesce_operations,
    identity_for_event,
    poll_watch_root,
    render,
    render_milestone_card,
    root_focus_for_key,
    watch_root_labels,
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
