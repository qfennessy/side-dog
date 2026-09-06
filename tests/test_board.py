import io
import json
import os
import re
import time
from concurrent.futures import Future
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog import surfaces
from side_dog.board import (
    ISSUE_COMMAND_WINDOW_MS,
    BoardRow,
    BoardSource,
    IssueCommand,
    LinkedIssue,
    branch_issue_numbers,
    format_age,
    issue_cell,
    linked_issues,
    next_group,
    pr_cell,
    render_board,
    repository_from_remote,
    repository_from_web_url,
    row_key,
    rows_from_sources,
    sort_rows,
    surface_label,
    title_issue_numbers,
)
from side_dog.cli import (
    BOARD_TAIL_BYTES,
    CLAUDE_SURFACE_NAMES,
    GITHUB_PR_FIELDS,
    STATE_ENV,
    BoardGithubRequest,
    BoardRootState,
    attribute_board_surfaces,
    board_activity_tail,
    board_history_tail,
    board_source,
    codex_surface,
    collect_board_github,
    discovered_watch_roots,
    events_path,
    main,
    mark_unfinished_board_github,
    normalized_tool_events,
    refresh_board_root,
)
from side_dog.integrations import AgentIdentity, AgentStatus
from side_dog.model import normalize_github_pr
from side_dog.privacy import safe_event


NOW_MS = 1_800_000_000_000


def identity(**overrides: str) -> dict[str, str]:
    base = {
        "agent": "claude-code",
        "root": "/work/side-dog",
        "pane_id": "",
        "workspace_id": "",
        "tab_id": "",
        "working_root": "/work/side-dog",
        "session_id": "",
        "status": "working",
        "label": "",
        "surface": "",
    }
    base.update(overrides)
    return base


def github(number: int = 151, **overrides: object) -> dict[str, object]:
    status: dict[str, object] = {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": "Add board",
        "state": "OPEN",
        "draft": False,
        "branch": "feat/board",
        "review": "",
        "merge_state": "CLEAN",
        "mergeable": "MERGEABLE",
        "ci": "CI 3/3",
        "checks_total": 3,
        "checks_passed": 3,
        "checks_pending": 0,
        "checks_failed": 0,
    }
    status.update(overrides)
    return status


def mixed_sources() -> list[BoardSource]:
    """Four surfaces on two repositories, the situation the board is for."""
    side_dog = BoardSource(
        root="/work/side-dog",
        repository="side-dog",
        branch="feat/board",
        github=github(),
        identities={
            "claude-code:c1": identity(
                session_id="c1",
                pane_id="w1:p3",
                workspace_id="side-dog",
                surface="terminal",
            ),
            "pane:w1:p5": identity(
                agent="codex", pane_id="w1:p5", workspace_id="side-dog", status="idle"
            ),
            "codex:d1": identity(
                agent="codex",
                session_id="d1",
                working_root="/Users/q/.codex/worktrees/abc/side-dog",
                surface="Codex Desktop",
                status="idle",
            ),
            "pi:p1": identity(agent="pi", session_id="p1", status="done"),
            "git:x": identity(agent="git", session_id="x"),
        },
        branches={"/Users/q/.codex/worktrees/abc/side-dog": "codex/issue-139"},
        activity={
            "claude-code:c1": NOW_MS - 4_000,
            "codex:d1": NOW_MS - 360_000,
            "pi:p1": NOW_MS - 30_000,
        },
    )
    herdr = BoardSource(
        root="/work/herdr",
        repository="herdr",
        branch="main",
        github=None,
        identities={
            "claude-code:c2": identity(
                session_id="c2",
                root="/work/herdr",
                working_root="/work/herdr",
                surface="Claude Desktop",
                status="blocked",
            ),
        },
        activity={"claude-code:c2": NOW_MS - 120_000},
    )
    return [side_dog, herdr]


class RowIdentityTest(TestCase):
    def test_session_id_makes_a_session_key(self) -> None:
        self.assertEqual(row_key(identity(session_id="abc")), "claude-code:abc")

    def test_two_panes_without_session_ids_stay_two_rows(self) -> None:
        first = row_key(identity(pane_id="w1:p1"))
        second = row_key(identity(pane_id="w1:p2"))
        self.assertEqual(first, "pane:w1:p1")
        self.assertNotEqual(first, second)

    def test_unknown_session_id_does_not_fold_panes_together(self) -> None:
        self.assertEqual(
            row_key(identity(session_id="unknown", pane_id="w1:p9")), "pane:w1:p9"
        )

    def test_agent_identity_carries_a_surface_field(self) -> None:
        wire = AgentIdentity.from_wire(identity(session_id="s", surface="VS Code")).to_wire()
        self.assertEqual(wire["surface"], "VS Code")


class SurfaceTest(TestCase):
    def test_herdr_pane_wins_over_a_loader_surface(self) -> None:
        label = surface_label(
            identity(pane_id="w1:p3", workspace_id="side-dog", surface="terminal")
        )
        self.assertEqual(label, "Herdr · side-dog · pane w1:p3")

    def test_loader_surface_is_used_without_a_pane(self) -> None:
        self.assertEqual(surface_label(identity(surface="Claude Desktop")), "Claude Desktop")

    def test_nobody_knowing_is_said_plainly(self) -> None:
        self.assertEqual(surface_label(identity()), "unknown")

    def test_claude_entrypoints_map_to_surfaces(self) -> None:
        self.assertEqual(CLAUDE_SURFACE_NAMES["cli"], "terminal")
        self.assertEqual(CLAUDE_SURFACE_NAMES["claude-desktop"], "Claude Desktop")
        self.assertEqual(CLAUDE_SURFACE_NAMES["remote_desktop"], "Claude remote")
        self.assertNotIn("something-new", CLAUDE_SURFACE_NAMES)

    def test_codex_originators_map_to_surfaces(self) -> None:
        self.assertEqual(codex_surface("Codex Desktop 1.2"), "Codex Desktop")
        self.assertEqual(codex_surface("codex-tui"), "terminal")
        self.assertEqual(codex_surface("codex_cli_rs"), "terminal")
        self.assertEqual(codex_surface("codex_vscode"), "VS Code")
        self.assertEqual(codex_surface("something-new"), "")


class RowsTest(TestCase):
    def test_one_row_per_coding_session_and_none_for_git(self) -> None:
        rows = rows_from_sources(mixed_sources(), NOW_MS)
        self.assertEqual(
            sorted(row.key for row in rows),
            ["claude-code:c1", "claude-code:c2", "codex:d1", "pane:w1:p5", "pi:p1"],
        )

    def test_rows_carry_surface_repository_branch_status_and_age(self) -> None:
        rows = {row.key: row for row in rows_from_sources(mixed_sources(), NOW_MS)}
        claude = rows["claude-code:c1"]
        self.assertEqual(claude.surface, "Herdr · side-dog · pane w1:p3")
        self.assertEqual((claude.repository, claude.branch), ("side-dog", "feat/board"))
        self.assertIs(claude.status, AgentStatus.WORKING)
        self.assertEqual(claude.age_seconds, 4.0)
        self.assertEqual(claude.github["number"], 151)
        desktop = rows["claude-code:c2"]
        self.assertEqual(desktop.surface, "Claude Desktop")
        self.assertIs(desktop.status, AgentStatus.BLOCKED)
        self.assertIsNone(desktop.github)

    def test_another_worktree_uses_its_own_branch_and_not_the_roots_pr(self) -> None:
        rows = {row.key: row for row in rows_from_sources(mixed_sources(), NOW_MS)}
        codex_desktop = rows["codex:d1"]
        self.assertEqual(codex_desktop.surface, "Codex Desktop")
        self.assertEqual(codex_desktop.branch, "codex/issue-139")
        self.assertIsNone(codex_desktop.github)

    def test_another_worktree_on_the_pr_branch_shares_the_pr(self) -> None:
        source = mixed_sources()[0]
        shared = BoardSource(
            root=source.root,
            repository=source.repository,
            branch=source.branch,
            github=source.github,
            identities=source.identities,
            branches={"/Users/q/.codex/worktrees/abc/side-dog": "feat/board"},
            activity=source.activity,
        )
        rows = {row.key: row for row in rows_from_sources([shared], NOW_MS)}
        self.assertEqual(rows["codex:d1"].github["number"], 151)

    def test_a_pane_without_a_session_has_no_age(self) -> None:
        rows = {row.key: row for row in rows_from_sources(mixed_sources(), NOW_MS)}
        self.assertIsNone(rows["pane:w1:p5"].age_seconds)

    def test_a_session_seen_under_two_folders_sits_in_the_one_containing_it(self) -> None:
        shared = identity(session_id="s", working_root="/work/side-dog/sub")
        outer = BoardSource(root="/work", repository="work", identities={"a": shared})
        inner = BoardSource(root="/work/side-dog", repository="side-dog", identities={"a": shared})
        rows = rows_from_sources([outer, inner], NOW_MS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].repository, "side-dog")

    def test_age_comes_from_whichever_folder_recorded_the_session(self) -> None:
        shared = identity(session_id="s", working_root="/work/side-dog/sub", status="done")
        outer = BoardSource(
            root="/work",
            repository="work",
            identities={"a": shared},
            activity={"claude-code:s": NOW_MS - 901_000},
        )
        inner = BoardSource(root="/work/side-dog", repository="side-dog", identities={"a": shared})
        self.assertEqual(rows_from_sources([outer, inner], NOW_MS), [])
        outer_fresh = BoardSource(
            root="/work",
            repository="work",
            identities={"a": shared},
            activity={"claude-code:s": NOW_MS - 5_000},
        )
        rows = rows_from_sources([outer_fresh, inner], NOW_MS)
        self.assertEqual(rows[0].age_seconds, 5.0)

    def test_two_agents_sharing_an_external_session_id_keep_their_own_ages(self) -> None:
        source = BoardSource(
            root="/work/x",
            identities={
                "claude": identity(session_id="same"),
                "codex": identity(agent="codex", session_id="same"),
            },
            activity={"claude-code:same": NOW_MS - 2_000, "codex:same": NOW_MS - 200_000},
        )
        rows = {row.key: row for row in rows_from_sources([source], NOW_MS)}
        self.assertEqual(rows["claude-code:same"].age_seconds, 2.0)
        self.assertEqual(rows["codex:same"].age_seconds, 200.0)

    def test_done_sessions_age_out_after_fifteen_minutes(self) -> None:
        fresh = BoardSource(
            root="/work/x",
            identities={"a": identity(session_id="a", status="done")},
            activity={"claude-code:a": NOW_MS - 60_000},
        )
        stale = BoardSource(
            root="/work/x",
            identities={"a": identity(session_id="a", status="done")},
            activity={"claude-code:a": NOW_MS - 901_000},
        )
        self.assertEqual(len(rows_from_sources([fresh], NOW_MS)), 1)
        self.assertEqual(rows_from_sources([stale], NOW_MS), [])

    def test_a_session_with_no_worktree_still_appears(self) -> None:
        source = BoardSource(
            root="/Users/q",
            repository="",
            branch="",
            identities={"a": identity(session_id="a", root="/Users/q", working_root="/Users/q")},
        )
        rows = rows_from_sources([source], NOW_MS)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].repository, "")


class SortTest(TestCase):
    def test_working_then_blocked_idle_done_newest_first(self) -> None:
        rows = sort_rows(rows_from_sources(mixed_sources(), NOW_MS))
        self.assertEqual(
            [row.key for row in rows],
            ["claude-code:c1", "claude-code:c2", "codex:d1", "pane:w1:p5", "pi:p1"],
        )

    def test_grouping_by_repository_reorders(self) -> None:
        rows = sort_rows(rows_from_sources(mixed_sources(), NOW_MS), group="repo")
        self.assertEqual(rows[0].repository, "herdr")

    def test_group_cycles(self) -> None:
        self.assertEqual(next_group("none"), "surface")
        self.assertEqual(next_group("surface"), "repo")
        self.assertEqual(next_group("repo"), "none")


class CellTest(TestCase):
    def test_pr_cell_shows_number_ci_and_review(self) -> None:
        self.assertEqual(pr_cell(github()), "#151 ✓ci ○rev")
        self.assertEqual(
            pr_cell(github(checks_failed=1, review="CHANGES_REQUESTED")), "#151 ✗ci ✗rev"
        )
        self.assertEqual(
            pr_cell(github(checks_pending=1, review="APPROVED")), "#151 …ci ✓rev"
        )
        self.assertEqual(pr_cell(github(state="MERGED")), "#151 merged")
        self.assertEqual(pr_cell(github(state="CLOSED")), "#151 closed")
        self.assertEqual(pr_cell(None), "—")

    def test_partial_coverage_and_drafts_are_marked(self) -> None:
        self.assertEqual(pr_cell(github(draft=True, coverage="PARTIAL")), "#151 ✓ci ○rev draft ?")

    def test_age_is_compact(self) -> None:
        self.assertEqual(format_age(None), "")
        self.assertEqual(format_age(4.2), "4s")
        self.assertEqual(format_age(360), "6m")
        self.assertEqual(format_age(7200), "2h")
        self.assertEqual(format_age(200_000), "2d")


OWN = "github.com/o/r"


def link(**overrides: object) -> tuple[LinkedIssue, ...]:
    values: dict[str, object] = {
        "repository": OWN,
        "github": None,
        "commands": (),
        "branch": "",
        "now_ms": NOW_MS,
    }
    values.update(overrides)
    return linked_issues(**values)  # type: ignore[arg-type]


class IssueLinkageTest(TestCase):
    def test_closing_issues_confirm_and_take_the_prs_repository(self) -> None:
        issues = link(github=github(closing_issues=(142, 139)))
        self.assertEqual(
            issues,
            (LinkedIssue(OWN, 139, True), LinkedIssue(OWN, 142, True)),
        )
        # The PR's own URL names the repository even when the row has none.
        self.assertEqual(
            link(repository="", github=github(closing_issues=(7,))),
            (LinkedIssue(OWN, 7, True),),
        )

    def test_readback_reduces_closing_references_to_numbers(self) -> None:
        self.assertIn("closingIssuesReferences", GITHUB_PR_FIELDS.split(","))
        status = normalize_github_pr(
            {
                "number": 151,
                "url": "https://github.com/o/r/pull/151",
                "closingIssuesReferences": [
                    {"number": 139, "title": "private title", "url": "https://x/y"},
                    {"number": 142, "body": "private body"},
                    {"number": 139},
                    {"number": 0},
                    {"number": "7"},
                    "junk",
                ],
            }
        )
        self.assertEqual(status["closing_issues"], (139, 142))
        self.assertNotIn("private", json.dumps(status))
        self.assertEqual(normalize_github_pr({"number": 1})["closing_issues"], ())

    def test_a_recent_successful_issue_command_confirms(self) -> None:
        recent = IssueCommand(NOW_MS - 1_000, 12, "")
        self.assertEqual(link(commands=(recent,)), (LinkedIssue(OWN, 12, True),))
        stale = IssueCommand(NOW_MS - 3_600_001, 12, "")
        self.assertEqual(link(commands=(stale,)), ())
        future = IssueCommand(NOW_MS + 60_000, 12, "")
        self.assertEqual(link(commands=(future,)), ())

    def test_a_command_scoped_to_another_repository_links_that_repository(self) -> None:
        other = IssueCommand(NOW_MS, 12, "https://github.com/org/other/issues/12")
        self.assertEqual(
            link(commands=(other,)), (LinkedIssue("github.com/org/other", 12, True),)
        )

    def test_branch_names_infer_with_a_question_mark(self) -> None:
        self.assertEqual(link(branch="codex/issue-139"), (LinkedIssue(OWN, 139, False),))
        self.assertEqual(branch_issue_numbers("139-fix-thing"), (139,))
        self.assertEqual(branch_issue_numbers("issue-139"), (139,))
        self.assertEqual(branch_issue_numbers("codex/issue/139"), (139,))
        self.assertEqual(branch_issue_numbers("fix/thing-42"), (42,))
        self.assertEqual(branch_issue_numbers("fix/42"), (42,))
        self.assertEqual(branch_issue_numbers("fix/#88-thing"), (88,))
        self.assertEqual(branch_issue_numbers("feat/board"), ())
        self.assertEqual(branch_issue_numbers("chore/release-1.1.0"), ())
        self.assertEqual(branch_issue_numbers("main"), ())
        self.assertEqual(branch_issue_numbers(""), ())

    def test_pr_titles_infer_and_the_body_is_never_read(self) -> None:
        issues = link(github=github(title="Fix #12 and close /issues/13", body="#99"))
        self.assertEqual(issues, (LinkedIssue(OWN, 12, False), LinkedIssue(OWN, 13, False)))
        self.assertEqual(title_issue_numbers("Board phase 2 (#170)"), (170,))
        self.assertEqual(
            title_issue_numbers("see https://github.com/o/r/issues/5"), (5,)
        )
        self.assertEqual(title_issue_numbers("Version 2.0"), ())
        # Ordinary punctuation after a mention is not a reason to miss it.
        for title in (
            "Fix (https://github.com/o/r/issues/12).",
            "Fix https://github.com/o/r/issues/12, then more",
            "Fix https://github.com/o/r/issues/12; also #12",
            "Fix (#12).",
            "Fix #12, #13; and #14.",
        ):
            with self.subTest(title=title):
                self.assertEqual(title_issue_numbers(title)[0], 12)
        self.assertEqual(title_issue_numbers("Fix #12, #13; and #14."), (12, 13, 14))

    def test_confirmed_wins_over_inferred_and_sorts_first(self) -> None:
        issues = link(
            github=github(title="Fix #5 and #200", closing_issues=(200,)),
            branch="issue-5-and-9",
            commands=(IssueCommand(NOW_MS, 9, ""),),
        )
        self.assertEqual(
            issues,
            (
                LinkedIssue(OWN, 9, True),
                LinkedIssue(OWN, 200, True),
                LinkedIssue(OWN, 5, False),
            ),
        )

    def test_the_same_number_in_two_repositories_is_distinct(self) -> None:
        issues = link(
            branch="fix/12",
            commands=(IssueCommand(NOW_MS, 12, "https://github.com/org/other/issues/12"),),
        )
        self.assertEqual(
            issues,
            (LinkedIssue("github.com/org/other", 12, True), LinkedIssue(OWN, 12, False)),
        )
        self.assertNotEqual(issues[0][:2], issues[1][:2])

    def test_rows_carry_their_issues(self) -> None:
        source = mixed_sources()[0]
        seeded = BoardSource(
            root=source.root,
            repository=source.repository,
            branch=source.branch,
            github=github(closing_issues=(142, 139, 150)),
            identities=source.identities,
            branches=source.branches,
            activity=source.activity,
            github_repository=OWN,
            issue_commands={"claude-code:c1": (IssueCommand(NOW_MS - 5_000, 7, ""),)},
        )
        rows = {row.key: row for row in rows_from_sources([seeded], NOW_MS)}
        self.assertEqual(
            [issue.number for issue in rows["claude-code:c1"].issues], [7, 139, 142, 150]
        )
        # Commands recorded under another folder that reported the session
        # count too, and a different agent sharing the id does not.
        elsewhere = BoardSource(
            root="/work",
            repository="work",
            identities={"claude-code:c1": seeded.identities["claude-code:c1"]},
            issue_commands={
                "claude-code:c1": (IssueCommand(NOW_MS - 2_000, 8, ""),),
                "codex:c1": (IssueCommand(NOW_MS - 2_000, 9, ""),),
            },
        )
        rows = {row.key: row for row in rows_from_sources([elsewhere, seeded], NOW_MS)}
        self.assertEqual(
            [issue.number for issue in rows["claude-code:c1"].issues], [7, 8, 139, 142, 150]
        )
        self.assertTrue(all(issue.confirmed for issue in rows["claude-code:c1"].issues))
        self.assertEqual(rows["claude-code:c1"].github_repository, OWN)
        # The Codex Desktop worktree is on its own branch: inferred only, and
        # the folder's PR does not reach it.
        self.assertEqual(rows["codex:d1"].issues, (LinkedIssue(OWN, 139, False),))
        # A pane without a session id shares the folder's PR but has no
        # command history of its own to draw on.
        self.assertEqual(
            [issue.number for issue in rows["pane:w1:p5"].issues], [139, 142, 150]
        )

    def test_repository_parsers(self) -> None:
        self.assertEqual(repository_from_web_url("https://github.com/o/r/pull/151"), OWN)
        self.assertEqual(repository_from_web_url("https://WWW.github.com/o/r/issues/1"), OWN)
        self.assertEqual(
            repository_from_web_url("https://ghe.example.com/o/r/issues/1?x=1"),
            "ghe.example.com/o/r",
        )
        self.assertEqual(repository_from_web_url("https://github.com/o/r"), "")
        self.assertEqual(repository_from_web_url(""), "")
        self.assertEqual(repository_from_remote("git@github.com:o/r.git"), OWN)
        self.assertEqual(repository_from_remote("https://github.com/o/r"), OWN)
        self.assertEqual(repository_from_remote("https://alice@github.com/o/r.git"), OWN)
        self.assertEqual(
            repository_from_remote("ssh://git@ghe.example.com:2222/o/r.git"),
            "ghe.example.com/o/r",
        )
        self.assertEqual(repository_from_remote("/srv/git/r.git"), "")
        self.assertEqual(repository_from_remote(""), "")


class IssueCellTest(TestCase):
    @staticmethod
    def row(issues: tuple[LinkedIssue, ...]) -> BoardRow:
        return BoardRow(
            key="claude-code:a",
            agent="claude-code",
            surface="terminal",
            repository="r",
            branch="main",
            root="/w",
            working_root="/w",
            status=AgentStatus.WORKING,
            age_seconds=1.0,
            github_repository=OWN,
            issues=issues,
        )

    def test_first_issue_and_a_count(self) -> None:
        self.assertEqual(issue_cell(self.row(())), "—")
        self.assertEqual(issue_cell(self.row((LinkedIssue(OWN, 139, True),))), "#139")
        self.assertEqual(issue_cell(self.row((LinkedIssue(OWN, 139, False),))), "#139?")
        three = tuple(LinkedIssue(OWN, number, True) for number in (139, 142, 150))
        self.assertEqual(issue_cell(self.row(three)), "#139 +2")

    def test_a_foreign_repository_is_named_and_github_com_is_implied(self) -> None:
        self.assertEqual(
            issue_cell(self.row((LinkedIssue("github.com/org/other", 12, True),))),
            "org/other#12",
        )
        self.assertEqual(
            issue_cell(self.row((LinkedIssue("ghe.example.com/org/other", 12, False),))),
            "ghe.example.com/org/other#12?",
        )

    def test_only_the_exact_github_host_is_trimmed(self) -> None:
        self.assertEqual(
            issue_cell(self.row((LinkedIssue("github.com/owner/name", 1, True),))),
            "owner/name#1",
        )
        for repository in (
            "evil-github.com/owner/name",
            "github.com.evil/owner/name",
            "notgithub.com/owner/name",
            "GitHub.com/owner/name",
        ):
            with self.subTest(repository=repository):
                self.assertEqual(
                    issue_cell(self.row((LinkedIssue(repository, 1, True),))),
                    f"{repository}#1",
                )
        # A bare host with nothing after it is kept whole rather than emptied.
        self.assertEqual(
            issue_cell(self.row((LinkedIssue("github.com/", 1, True),))), "github.com/#1"
        )


class RenderTest(TestCase):
    def test_wide_frame_has_every_column(self) -> None:
        rows = rows_from_sources(mixed_sources(), NOW_MS)
        screen = render_board(rows, 110, 20, False, clock="14:32:07")
        lines = screen.splitlines()
        self.assertTrue(lines[0].startswith("SIDE DOG board · 5 sessions · 2 repos"))
        self.assertTrue(lines[0].endswith("14:32:07"))
        self.assertIn("AGENT", lines[1])
        self.assertIn("SURFACE", lines[1])
        self.assertIn("REPO / BRANCH", lines[1])
        self.assertIn("ISSUE", lines[1])
        self.assertIn("PR", lines[1])
        self.assertIn("STATUS", lines[1])
        self.assertLess(lines[1].index("REPO / BRANCH"), lines[1].index("ISSUE"))
        self.assertLess(lines[1].index("ISSUE"), lines[1].index("PR"))
        self.assertIn("Herdr · side-dog · pane w1:p3", lines[2])
        self.assertIn("side-dog  feat/board", lines[2])
        self.assertIn("#151 ✓ci ○rev", lines[2])
        self.assertIn("● working 4s", lines[2])
        self.assertIn("Claude Desktop", lines[3])
        self.assertIn("◌ blocked 2m", lines[3])
        self.assertIn("Codex Desktop", lines[4])
        self.assertIn("codex/issue-139", lines[4])
        self.assertIn("#139?", lines[4])
        for line in lines:
            self.assertLessEqual(len(line), 110)

    def test_issue_cells_render_with_markers_and_counts(self) -> None:
        source = mixed_sources()[0]
        seeded = BoardSource(
            root=source.root,
            repository=source.repository,
            branch=source.branch,
            github=github(closing_issues=(142, 139, 150)),
            identities=source.identities,
            branches=source.branches,
            activity=source.activity,
            github_repository=OWN,
            issue_commands={
                "codex:d1": (
                    IssueCommand(NOW_MS - 1_000, 12, "https://github.com/org/other/issues/12"),
                )
            },
        )
        rows = rows_from_sources([seeded], NOW_MS)
        lines = render_board(rows, 120, 20, False).splitlines()
        self.assertIn("#139 +2", lines[2])
        codex_line = next(line for line in lines if "Codex Desktop" in line)
        self.assertIn("org/other#12 +1", codex_line)
        for line in render_board(rows, 120, 20, True).splitlines():
            self.assertLessEqual(len(re.sub(r"\x1b\[[0-9;]*m", "", line)), 120)

    def test_narrow_frame_drops_pr_then_issue_then_surface(self) -> None:
        rows = rows_from_sources(mixed_sources(), NOW_MS)
        roomy = render_board(rows, 82, 20, False).splitlines()[1]
        self.assertIn("SURFACE", roomy)
        self.assertIn("ISSUE", roomy)
        self.assertNotIn("PR", roomy.replace("REPO", ""))
        medium = render_board(rows, 70, 20, False).splitlines()[1]
        self.assertIn("SURFACE", medium)
        self.assertNotIn("ISSUE", medium)
        self.assertNotIn("PR", medium.replace("REPO", ""))
        narrow = render_board(rows, 48, 20, False).splitlines()[1]
        self.assertNotIn("SURFACE", narrow)
        self.assertIn("REPO / BRANCH", narrow)
        self.assertIn("STATUS", narrow)
        for width in (82, 70, 48):
            for line in render_board(rows, width, 20, True).splitlines():
                self.assertLessEqual(len(re.sub(r"\x1b\[[0-9;]*m", "", line)), width)

    def test_very_narrow_frames_never_exceed_the_width_and_keep_status(self) -> None:
        rows = rows_from_sources(mixed_sources(), NOW_MS)
        for width in (40, 30, 24, 20):
            with self.subTest(width=width):
                plain = render_board(rows, width, 20, False).splitlines()
                self.assertIn("STATUS", plain[1])
                self.assertIn("working", plain[2])
                for line in plain:
                    self.assertLessEqual(len(line), width, line)
                colored = render_board(rows, width, 20, True).splitlines()
                for line in colored:
                    stripped = re.sub(r"\x1b\[[0-9;]*m", "", line)
                    self.assertLessEqual(len(stripped), width, stripped)

    def test_grouped_frame_has_headers_and_drops_the_grouped_column(self) -> None:
        rows = rows_from_sources(mixed_sources(), NOW_MS)
        screen = render_board(rows, 100, 20, False, group="repo")
        lines = screen.splitlines()
        self.assertIn("BRANCH", lines[1])
        self.assertNotIn("REPO", lines[1])
        self.assertEqual(lines[2], "herdr")
        self.assertEqual(lines[4], "side-dog")
        by_surface = render_board(rows, 100, 20, False, group="surface").splitlines()
        self.assertNotIn("SURFACE", by_surface[1])
        self.assertEqual(by_surface[2], "Claude Desktop")

    def test_repositories_sharing_a_name_stay_apart(self) -> None:
        first = BoardSource(
            root="/org-a/api",
            repository="api",
            repository_key="/org-a/api/.git",
            branch="main",
            identities={"a": identity(session_id="a", root="/org-a/api", working_root="/org-a/api")},
        )
        second = BoardSource(
            root="/org-b/api",
            repository="api",
            repository_key="/org-b/api/.git",
            branch="dev",
            identities={"b": identity(session_id="b", root="/org-b/api", working_root="/org-b/api")},
        )
        rows = rows_from_sources([first, second], NOW_MS)
        lines = render_board(rows, 100, 20, False, group="repo").splitlines()
        self.assertIn("2 repos", lines[0])
        self.assertEqual(lines[2], "api (org-a)")
        self.assertEqual(lines[4], "api (org-b)")
        same = BoardSource(
            root="/org-a/api-worktree",
            repository="api",
            repository_key="/org-a/api/.git",
            branch="feat",
            identities={"c": identity(session_id="c", root="/org-a/api-worktree", working_root="/org-a/api-worktree")},
        )
        rows = rows_from_sources([first, same], NOW_MS)
        lines = render_board(rows, 100, 20, False, group="repo").splitlines()
        self.assertIn("1 repo", lines[0])
        self.assertEqual(lines[2], "api")

    def test_short_frame_says_how_many_rows_are_hidden(self) -> None:
        rows = rows_from_sources(mixed_sources(), NOW_MS)
        lines = render_board(rows, 100, 5, False, hints="q quit").splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[-2], "… 4 more")
        self.assertEqual(lines[-1], "q quit")

    def test_empty_board_explains_itself(self) -> None:
        screen = render_board([], 80, 10, False, clock="09:00:00")
        self.assertIn("0 sessions", screen)
        self.assertIn("No coding-agent sessions found.", screen)

    def test_empty_board_fits_a_narrow_pane_in_color_too(self) -> None:
        for color in (False, True):
            for line in render_board([], 40, 10, color, clock="09:00:00").splitlines():
                stripped = re.sub(r"\x1b\[[0-9;]*m", "", line)
                self.assertLessEqual(len(stripped), 40, stripped)

    def test_color_frame_carries_escapes_and_plain_does_not(self) -> None:
        rows = rows_from_sources(mixed_sources(), NOW_MS)
        self.assertIn("\x1b[", render_board(rows, 100, 20, True))
        self.assertNotIn("\x1b[", render_board(rows, 100, 20, False))


class DiscoveryCapTest(TestCase):
    def test_board_discovery_is_uncapped_and_watch_discovery_is_not(self) -> None:
        folders = {Path(f"/work/repo-{index}"): True for index in range(9)}
        with patch("side_dog.cli.agent_working_folders", return_value=folders), patch(
            "side_dog.cli.root_is_missing", return_value=False
        ), patch("side_dog.cli.pinned_folders", return_value=[]):
            capped = discovered_watch_roots({}, None)
            uncapped = discovered_watch_roots({}, None, uncapped=True)
        self.assertEqual(len(capped), 8)
        self.assertEqual(len(uncapped), 9)


class ActivityTailTest(TestCase):
    def test_newest_epoch_per_session_from_the_file_tail(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            lines = [
                {"agent": "claude-code", "session_id": "a", "epoch_ms": 10},
                {"agent": "claude-code", "session_id": "a", "epoch_ms": 30},
                {"agent": "codex", "session_id": "a", "epoch_ms": 25},
                {"agent": "codex", "session_id": "b", "epoch_ms": 20},
                {"kind": "file"},
                "not json",
            ]
            path.write_text(
                "\n".join(json.dumps(line) if isinstance(line, dict) else line for line in lines)
                + "\n"
            )
            activity, stamp = board_activity_tail(path, None, {})
            self.assertEqual(activity, {"claude-code:a": 30, "codex:a": 25, "codex:b": 20})
            again, same = board_activity_tail(path, stamp, {"cached": 1})
            self.assertEqual(again, {"cached": 1})
            self.assertEqual(same, stamp)

    def test_a_session_that_slid_out_of_the_tail_keeps_its_time(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                json.dumps({"agent": "codex", "session_id": "new", "epoch_ms": 50}) + "\n"
            )
            previous = {"claude-code:old": 40, "codex:new": 45}
            activity, _ = board_activity_tail(path, (1, 1), previous)
            self.assertEqual(activity, {"claude-code:old": 40, "codex:new": 50})
            self.assertEqual(previous, {"claude-code:old": 40, "codex:new": 45})

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(board_activity_tail(Path("/nonexistent/x.jsonl"), None, {}), ({}, None))

    def test_successful_issue_views_are_collected_per_session(self) -> None:
        def record(session: str, epoch: int, **fields: object) -> dict[str, object]:
            base: dict[str, object] = {
                "agent": "codex",
                "session_id": session,
                "epoch_ms": epoch,
                "kind": "issue",
                "status": "success",
                "title": "Viewed issue",
                "detail": "issue #12",
                "github": {"number": 12, "url": "https://github.com/org/other/issues/12"},
            }
            base.update(fields)
            return base

        lines = [
            record("a", 10),
            record("a", 20, title="Branched from issue", github={"number": 13}),
            # A failed view, a closed issue, a compound command (no github),
            # and a running view do not confirm anything.
            record("a", 30, status="failed"),
            record("a", 40, title="Closed issue"),
            record("a", 50, github=None),
            record("a", 60, status="running"),
            record("b", 70, github={"number": "12"}),
            record("b", 80, github={"number": 8, "url": 5}),
            {"agent": "codex", "session_id": "b", "epoch_ms": 90, "kind": "file"},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
            activity, issues, stamp = board_history_tail(path, None, {}, {})
            self.assertEqual(activity, {"codex:a": 60, "codex:b": 90})
            self.assertEqual(
                issues,
                {
                    "codex:a": (
                        IssueCommand(10, 12, "https://github.com/org/other/issues/12"),
                        IssueCommand(20, 13, ""),
                    ),
                    "codex:b": (IssueCommand(80, 8, ""),),
                },
            )
            cached = {"y": (IssueCommand(1, 2, ""),)}
            again = board_history_tail(path, stamp, {"x": 1}, cached)
            self.assertEqual(again, ({"x": 1}, cached, stamp))
            # A changed file re-reads: previous entries stay, the tail's are
            # added, duplicates collapse, and the inputs are left alone.
            previous_issues = {
                "codex:a": (IssueCommand(1, 99, ""), IssueCommand(20, 13, "")),
                "claude-code:gone": (IssueCommand(2, 5, ""),),
            }
            _, merged, _ = board_history_tail(path, (1, 1), {}, previous_issues)
            self.assertEqual(
                [command.number for command in merged["codex:a"]], [99, 12, 13]
            )
            self.assertEqual(merged["claude-code:gone"], (IssueCommand(2, 5, ""),))
            self.assertEqual(
                previous_issues["codex:a"], (IssueCommand(1, 99, ""), IssueCommand(20, 13, ""))
            )
            # With a clock, entries older than the window are pruned, on a
            # re-read and on an unchanged file alike, so the map stays bounded.
            window = ISSUE_COMMAND_WINDOW_MS
            _, pruned, _ = board_history_tail(
                path, (1, 1), {}, previous_issues, now_ms=2 + window
            )
            self.assertEqual(
                [command.epoch_ms for command in pruned["codex:a"]], [10, 20]
            )
            self.assertEqual(pruned["claude-code:gone"], (IssueCommand(2, 5, ""),))
            _, pruned, _ = board_history_tail(
                path, stamp, {}, previous_issues, now_ms=21 + window
            )
            self.assertEqual(pruned, {})

    def test_a_command_that_slid_out_of_the_tail_confirms_until_the_hour_ends(self) -> None:
        viewed = {
            "agent": "codex",
            "session_id": "s",
            "epoch_ms": 1_000,
            "kind": "issue",
            "status": "success",
            "title": "Viewed issue",
            "detail": "issue #12",
            "github": {"number": 12},
        }
        filler = {"agent": "codex", "session_id": "s", "epoch_ms": 2_000, "kind": "file"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(json.dumps(viewed) + "\n")
            activity, issues, stamp = board_history_tail(path, None, {}, {}, now_ms=5_000)
            self.assertEqual(issues, {"codex:s": (IssueCommand(1_000, 12, ""),)})
            with path.open("a") as handle:
                line = json.dumps(filler) + "\n"
                for _ in range(BOARD_TAIL_BYTES // len(line) + 2):
                    handle.write(line)
            self.assertGreater(path.stat().st_size, BOARD_TAIL_BYTES + len(json.dumps(viewed)))
            # Still inside the hour: the command outlives its bytes.
            activity, issues, _ = board_history_tail(
                path, stamp, activity, issues, now_ms=1_000 + ISSUE_COMMAND_WINDOW_MS
            )
            self.assertEqual(issues, {"codex:s": (IssueCommand(1_000, 12, ""),)})
            self.assertEqual(activity, {"codex:s": 2_000})
            # Past the hour it is gone, even though nothing else changed.
            _, issues, _ = board_history_tail(
                path, stamp, activity, issues, now_ms=1_001 + ISSUE_COMMAND_WINDOW_MS
            )
            self.assertEqual(issues, {})
            # And a fresh reader that never saw the record cannot find it.
            _, unseen, _ = board_history_tail(path, None, {}, {}, now_ms=5_000)
            self.assertEqual(unseen, {})
        self.assertEqual(
            board_history_tail(Path("/nonexistent/x.jsonl"), None, {}, {}), ({}, {}, None)
        )

    def test_a_failed_view_written_by_the_normalizer_does_not_confirm(self) -> None:
        root = Path("/work/side-dog")
        with TemporaryDirectory() as directory, patch(
            "side_dog.cli.gh_known_hosts", return_value=()
        ):
            path = Path(directory) / "events.jsonl"
            records = []
            for command, status in (
                ("gh issue view 12", "failed"),
                ("gh issue view 123 || gh issue view 456", "success"),
                ("gh issue develop -R org/other 7", "success"),
            ):
                [event] = normalized_tool_events(
                    {
                        "agent": "codex",
                        "session_id": "s",
                        "tool_use_id": command,
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                    root,
                    status=status,
                )
                records.append(safe_event(root, event).to_wire())
            path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
            _, issues, _ = board_history_tail(path, None, {}, {})
        self.assertEqual([command.number for command in issues["codex:s"]], [7])
        self.assertEqual(issues["codex:s"][0].url, "https://github.com/org/other/issues/7")


class BoardRootRefreshTest(TestCase):
    def test_a_folder_outside_git_has_no_repository(self) -> None:
        state = BoardRootState(root=Path("/home/alice"), git_status=None)
        self.assertEqual(board_source(state).repository, "")
        state.git_status = {"branch": "main", "repository": "alice-tools"}
        with patch("side_dog.cli.origin_repository", return_value=""):
            self.assertEqual(board_source(state).repository, "alice-tools")

    def test_github_repository_comes_from_the_pr_then_origin(self) -> None:
        state = BoardRootState(root=Path("/work/side-dog"), git_status=None)
        with patch("side_dog.cli.origin_repository", return_value=OWN) as origin:
            # Outside Git there is no remote to ask.
            self.assertEqual(board_source(state).github_repository, "")
            origin.assert_not_called()
            state.git_status = {"branch": "main", "repository": "side-dog"}
            self.assertEqual(board_source(state).github_repository, OWN)
            origin.assert_called_once_with("/work/side-dog")
            state.github_status = github(url="https://github.com/fork/r/pull/3")
            source = board_source(state)
            self.assertEqual(source.github_repository, "github.com/fork/r")
            # The remote is asked once per frame and keeps naming the origin,
            # so conflicts keyed on it do not move when the readback lands.
            self.assertEqual(source.remote_repository, OWN)
            self.assertEqual(origin.call_count, 2)
        state.issue_commands = {"s": (IssueCommand(1, 2, ""),)}
        self.assertEqual(board_source(state).issue_commands, {"s": (IssueCommand(1, 2, ""),)})

    def test_a_branch_switch_forgets_the_old_pr_and_asks_again(self) -> None:
        root = Path("/work/side-dog")
        state = BoardRootState(
            root=root,
            git_status={"branch": "feat/a", "repository": "side-dog"},
            github_status=github(branch="feat/a"),
            last_git_refresh=0.0,
            last_github_refresh=100.0,
        )
        pending: dict = {}

        class Never:
            def done(self) -> bool:
                return False

        pending[root] = BoardGithubRequest(Never(), "feat/a")  # type: ignore[arg-type]

        class Executor:
            def __init__(self) -> None:
                self.calls = 0
                self.args: list[tuple] = []

            def submit(self, function, *args):
                self.calls += 1
                self.args.append(args)
                future = Future()
                future.set_result((None, "no pull requests found for branch"))
                return future

        executor = Executor()
        with patch(
            "side_dog.cli.load_git_state",
            return_value={"branch": "feat/b", "repository": "side-dog"},
        ), patch("side_dog.cli.load_agent_identities", return_value={}):
            refresh_board_root(
                state, 100.0, github_poll=60.0, executor=executor, pending=pending  # type: ignore[arg-type]
            )
        self.assertIsNone(state.github_status)
        self.assertEqual(executor.calls, 1)
        # gh is asked about the branch by name, never about "the checkout".
        self.assertEqual(executor.args, [(root, "feat/b")])
        self.assertEqual(pending[root].branch, "feat/b")
        collect_board_github({root: state}, pending)
        self.assertEqual(state.github_refresh_status, "complete")
        self.assertEqual(pending, {})

    def test_a_detached_checkout_asks_github_nothing(self) -> None:
        root = Path("/work/side-dog")
        state = BoardRootState(root=root, last_git_refresh=0.0)

        class Executor:
            calls = 0

            def submit(self, function, *args):
                Executor.calls += 1
                return Future()

        with patch(
            "side_dog.cli.load_git_state", return_value={"branch": "detached", "repository": "x"}
        ), patch("side_dog.cli.load_agent_identities", return_value={}):
            refresh_board_root(
                state, 10.0, github_poll=60.0, executor=Executor(), pending={}  # type: ignore[arg-type]
            )
        self.assertEqual(Executor.calls, 0)

    def test_gh_is_invoked_with_the_branch_when_given(self) -> None:
        calls: list[list[str]] = []

        class Completed:
            returncode = 1
            stdout = ""
            stderr = 'no pull requests found for branch "feat/a"'

        def fake_run(args, **kwargs):
            calls.append(list(args))
            return Completed()

        with patch("side_dog.cli.shutil.which", return_value="/usr/bin/gh"), patch(
            "side_dog.cli.subprocess.run", side_effect=fake_run
        ):
            from side_dog.cli import load_github_pr

            result, error = load_github_pr(Path("/work/x"), "feat/a")
            load_github_pr(Path("/work/x"))
        self.assertIsNone(result)
        self.assertIn("no pull requests found", error)
        self.assertEqual(calls[0][:4], ["gh", "pr", "view", "feat/a"])
        self.assertEqual(calls[1][:4], ["gh", "pr", "view", "--json"])

    def test_unfinished_readbacks_are_marked_rather_than_shown_as_no_pr(self) -> None:
        root = Path("/work/side-dog")
        state = BoardRootState(root=root, git_status={"branch": "feat/a", "repository": "x"})
        pending = {root: BoardGithubRequest(Future(), "feat/a")}
        mark_unfinished_board_github({root: state}, pending)
        self.assertEqual(state.github_status["coverage"], "PARTIAL")
        self.assertEqual(pr_cell(state.github_status), "PR ?")

    def test_a_readback_answering_for_another_branch_is_ignored_and_retried(self) -> None:
        root = Path("/work/side-dog")
        state = BoardRootState(
            root=root,
            git_status={"branch": "feat/a", "repository": "x"},
            last_github_refresh=100.0,
        )
        future = Future()
        future.set_result((github(branch="feat/b"), None))
        pending = {root: BoardGithubRequest(future, "feat/a")}
        collect_board_github({root: state}, pending)
        self.assertIsNone(state.github_status)
        self.assertEqual(pending, {})
        self.assertLess(state.last_github_refresh, 0)

    def test_a_failed_first_readback_shows_a_question_not_a_dash(self) -> None:
        root = Path("/work/side-dog")
        state = BoardRootState(root=root, git_status={"branch": "feat/a", "repository": "x"})
        future = Future()
        future.set_result((None, "gh is not installed"))
        collect_board_github({root: state}, {root: BoardGithubRequest(future, "feat/a")})
        self.assertEqual(state.github_refresh_status, "unavailable")
        self.assertEqual(pr_cell(board_source(state).github), "PR ?")
        definitive = Future()
        definitive.set_result((None, "no pull requests found for branch"))
        collect_board_github({root: state}, {root: BoardGithubRequest(definitive, "feat/a")})
        self.assertEqual(pr_cell(board_source(state).github), "—")

    def test_a_readback_for_a_left_branch_is_ignored(self) -> None:
        root = Path("/work/side-dog")
        state = BoardRootState(root=root, git_status={"branch": "feat/b", "repository": "x"})
        future = Future()
        future.set_result((github(branch="feat/a"), None))
        pending = {root: BoardGithubRequest(future, "feat/a")}
        collect_board_github({root: state}, pending)
        self.assertIsNone(state.github_status)
        self.assertEqual(pending, {})


class AncestrySurfaceTest(TestCase):
    """The board names the terminal behind a session Herdr did not open."""

    # One Claude under Ghostty; one Codex under Herdr and another under the
    # same Ghostty shell, both working in the watched folder.
    PS = (
        "    1     0 Sat Sep  6 09:00:00 2026 /sbin/launchd\n"
        "  400     1 Sat Sep  6 09:00:01 2026 /Applications/Ghostty.app/Contents/MacOS/ghostty\n"
        "  420   400 Sat Sep  6 09:00:02 2026 zsh\n"
        "  500   420 Sat Sep  6 09:00:05 2026 claude\n"
        "  600     1 Sat Sep  6 09:01:00 2026 /Applications/Herdr.app/Contents/MacOS/Herdr\n"
        "  610   600 Sat Sep  6 09:01:01 2026 zsh\n"
        "  620   610 Sat Sep  6 09:01:05 2026 codex\n"
        "  700   420 Sat Sep  6 09:02:00 2026 codex\n"
    )
    ROLLOUTS = {
        Path("/codex/sessions/rollout-x1.jsonl"): {"id": "x1", "cwd": "/work/side-dog"},
        Path("/codex/sessions/rollout-x2.jsonl"): {"id": "x2", "cwd": "/work/side-dog"},
    }
    REGISTRY = [
        {"sessionId": "c1", "pid": 500, "cwd": "/work/side-dog"},
        {"sessionId": "c2", "pid": 999, "cwd": "/work/side-dog"},
        {"sessionId": "c3", "pid": 999, "cwd": "/work/side-dog"},
    ]

    def setUp(self) -> None:
        surfaces.reset_caches()
        self.addCleanup(surfaces.reset_caches)

    def patches(self, stack: ExitStack, *, holders=None, cwds=None) -> None:
        holders = holders or {}
        cwds = cwds or {}

        def fake_run(args):
            self.assertEqual(args[0], "ps")
            self.assertNotIn("args", " ".join(args))
            return self.PS

        def identity_of(path):
            return (1, sum(ord(character) for character in path))

        stack.enter_context(patch.object(surfaces, "DARWIN", False))
        stack.enter_context(patch.object(surfaces, "_run", side_effect=fake_run))
        stack.enter_context(patch.object(surfaces, "_file_identity", side_effect=identity_of))
        stack.enter_context(
            patch.object(
                surfaces,
                "_proc_fd_identities",
                side_effect=lambda pid: frozenset(identity_of(p) for p in holders.get(pid, [])),
            )
        )
        stack.enter_context(
            patch.object(surfaces, "_proc_link", side_effect=lambda pid, name: cwds.get(pid))
        )
        stack.enter_context(
            patch("side_dog.cli.claude_session_registry", return_value=list(self.REGISTRY))
        )
        stack.enter_context(
            patch(
                "side_dog.cli.codex_recent_sessions",
                return_value=[(path, 0.0) for path in self.ROLLOUTS],
            )
        )
        stack.enter_context(
            patch("side_dog.cli.codex_session_header", side_effect=lambda path: self.ROLLOUTS[path])
        )

    def test_refresh_names_a_bare_terminal_claude_and_an_ambiguous_codex_pair(self) -> None:
        root = Path("/work/side-dog")
        identities = {
            "claude-code:c1": identity(session_id="c1", surface="terminal"),
            "codex:x1": identity(agent="codex", session_id="x1", surface="terminal"),
            "codex:x2": identity(agent="codex", session_id="x2", surface="terminal"),
            "claude-code:c2": identity(
                session_id="c2", pane_id="w1:p3", workspace_id="side-dog", surface="terminal"
            ),
            "claude-code:c3": identity(session_id="c3", surface="Claude Desktop"),
        }

        class Executor:
            def submit(self, function, *args):
                future = Future()
                future.set_result((None, "no pull requests found for branch"))
                return future

        state = BoardRootState(root=root)
        with ExitStack() as stack, TemporaryDirectory() as state_dir:
            stack.enter_context(patch.dict(os.environ, {STATE_ENV: state_dir}))
            # Neither Codex holds its rollout open and both sit in the folder.
            self.patches(stack, cwds={620: "/work/side-dog", 700: "/work/side-dog"})
            stack.enter_context(
                patch("side_dog.cli.load_agent_identities", return_value=dict(identities))
            )
            stack.enter_context(
                patch(
                    "side_dog.cli.load_git_state",
                    return_value={"branch": "feat/board", "repository": "side-dog"},
                )
            )
            stack.enter_context(
                patch("side_dog.cli.canonical_root", side_effect=lambda value: Path(value))
            )
            refresh_board_root(
                state, 100.0, github_poll=60.0, executor=Executor(), pending={}  # type: ignore[arg-type]
            )
        rows = {row.key: row for row in rows_from_sources([board_source(state)], NOW_MS)}
        self.assertEqual(rows["claude-code:c1"].surface, "Ghostty")
        self.assertEqual(rows["codex:x1"].surface, "unknown")
        self.assertEqual(rows["codex:x2"].surface, "unknown")
        self.assertEqual(rows["claude-code:c2"].surface, "Herdr · side-dog · pane w1:p3")
        self.assertEqual(rows["claude-code:c3"].surface, "Claude Desktop")
        # The loader's own dicts were replaced, not mutated.
        self.assertEqual(identities["claude-code:c1"]["surface"], "terminal")

    def test_a_codex_holding_its_rollout_open_is_named(self) -> None:
        identities = {
            "codex:x1": identity(agent="codex", session_id="x1", surface="terminal"),
            "codex:x2": identity(agent="codex", session_id="x2", surface=""),
        }
        holders = {
            620: ["/dev/null", "/codex/sessions/rollout-x1.jsonl"],
            700: ["/codex/sessions/rollout-x2.jsonl"],
        }
        with ExitStack() as stack:
            self.patches(stack, holders=holders, cwds={620: "/work/side-dog", 700: "/work/side-dog"})
            attribute_board_surfaces(identities)
        self.assertEqual(identities["codex:x1"]["surface"], "Herdr")
        self.assertEqual(identities["codex:x2"]["surface"], "Ghostty")

    def test_settled_surfaces_and_panes_are_not_probed(self) -> None:
        identities = {
            "pane:w1:p5": identity(agent="codex", pane_id="w1:p5", surface="terminal"),
            "claude-code:c3": identity(session_id="c3", surface="Claude Desktop"),
            "codex:d1": identity(agent="codex", session_id="d1", surface="Codex Desktop"),
            "pi:p1": identity(agent="pi", session_id="p1", surface=""),
        }
        before = {key: dict(value) for key, value in identities.items()}
        with patch.object(surfaces, "_run", side_effect=AssertionError("probed")), patch(
            "side_dog.cli.claude_session_registry", side_effect=AssertionError("read")
        ), patch("side_dog.cli.codex_recent_sessions", side_effect=AssertionError("read")):
            attribute_board_surfaces(identities)
        self.assertEqual(identities, before)

    def test_a_failing_probe_keeps_the_loaders_value(self) -> None:
        identities = {
            "claude-code:c1": identity(session_id="c1", surface="terminal"),
            "codex:x1": identity(agent="codex", session_id="x1", surface=""),
        }
        with patch.object(surfaces, "_run", side_effect=OSError("no ps")), patch(
            "side_dog.cli.claude_session_registry", return_value=list(self.REGISTRY)
        ), patch(
            "side_dog.cli.codex_recent_sessions",
            return_value=[(path, 0.0) for path in self.ROLLOUTS],
        ), patch("side_dog.cli.codex_session_header", side_effect=lambda path: self.ROLLOUTS[path]):
            attribute_board_surfaces(identities)
        self.assertEqual(identities["claude-code:c1"]["surface"], "terminal")
        self.assertEqual(identities["codex:x1"]["surface"], "")
        self.assertEqual(surface_label(identities["codex:x1"]), "unknown")


class OnceCommandTest(TestCase):
    def test_once_prints_one_stable_frame(self) -> None:
        roots = [Path("/work/side-dog"), Path("/work/herdr")]
        identities = {
            os.fspath(roots[0]): {
                "claude-code:c1": identity(
                    session_id="c1", pane_id="w1:p3", workspace_id="side-dog"
                ),
                "codex:d1": identity(
                    agent="codex",
                    session_id="d1",
                    working_root="/Users/q/.codex/worktrees/abc/side-dog",
                    surface="Codex Desktop",
                    status="idle",
                ),
            },
            os.fspath(roots[1]): {
                "claude-code:c2": identity(
                    session_id="c2",
                    root="/work/herdr",
                    working_root="/work/herdr",
                    surface="Claude Desktop",
                )
            },
        }
        git_states = {
            "/work/side-dog": {"branch": "feat/board", "repository": "side-dog"},
            "/work/herdr": {"branch": "main", "repository": "herdr"},
            "/Users/q/.codex/worktrees/abc/side-dog": {
                "branch": "codex/issue-139",
                "repository": "side-dog",
            },
        }

        def fake_github(root: Path, branch: str | None = None):
            if root == roots[0]:
                self.assertEqual(branch, "feat/board")
                return github(closing_issues=(142,)), None
            return None, "no pull requests found for branch"

        with TemporaryDirectory() as state_dir, patch.dict(
            os.environ, {STATE_ENV: state_dir}
        ), patch("side_dog.cli.discovered_watch_roots", return_value=roots), patch(
            "side_dog.cli.load_agent_identities",
            side_effect=lambda root: identities[os.fspath(root)],
        ), patch(
            "side_dog.cli.load_git_state",
            side_effect=lambda root: dict(git_states.get(os.fspath(root), {})) or None,
        ), patch("side_dog.cli.load_github_pr", side_effect=fake_github), patch(
            "side_dog.cli.canonical_root", side_effect=lambda value: Path(value)
        ), patch(
            "side_dog.cli.origin_repository",
            side_effect=lambda root: {"/work/herdr": "github.com/o/herdr"}.get(root, ""),
        ), patch("side_dog.cli.gh_known_hosts", return_value=()):
            # The herdr session viewed an issue a moment ago; the history the
            # collectors wrote is what the board reads back.
            [viewed] = normalized_tool_events(
                {
                    "agent": "claude-code",
                    "session_id": "c2",
                    "tool_use_id": "call-1",
                    "tool_name": "Bash",
                    "tool_input": {"command": "gh issue view 12"},
                },
                roots[1],
                status="success",
            )
            history = events_path(roots[1])
            history.parent.mkdir(parents=True, exist_ok=True)
            wire = safe_event(roots[1], viewed).to_wire()
            wire["epoch_ms"] = int(time.time() * 1000) - 1_000
            history.write_text(json.dumps(wire) + "\n")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["board", "--once", "--width", "110", "--no-color"])
        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertIn("3 sessions · 2 repos", lines[0])
        self.assertIn("ISSUE", lines[1])
        body = "\n".join(lines[2:])
        self.assertIn("Herdr · side-dog · pane w1:p3", body)
        self.assertIn("#151 ✓ci ○rev", body)
        self.assertIn("#142", body)
        self.assertIn("Codex Desktop", body)
        self.assertIn("codex/issue-139", body)
        self.assertIn("#139?", body)
        self.assertIn("Claude Desktop", body)
        herdr_line = next(line for line in lines if "Claude Desktop" in line)
        self.assertIn("#12", herdr_line)
        self.assertNotIn("#12?", herdr_line)
        self.assertNotIn("\x1b[", stdout.getvalue())

    def test_group_repo_reorders_the_frame(self) -> None:
        roots = [Path("/work/side-dog"), Path("/work/herdr")]
        identities = {
            "/work/side-dog": {"a": identity(session_id="a")},
            "/work/herdr": {
                "b": identity(session_id="b", root="/work/herdr", working_root="/work/herdr")
            },
        }
        git_states = {
            "/work/side-dog": {"branch": "feat/board", "repository": "side-dog"},
            "/work/herdr": {"branch": "main", "repository": "herdr"},
        }
        with TemporaryDirectory() as state_dir, patch.dict(
            os.environ, {STATE_ENV: state_dir}
        ), patch("side_dog.cli.discovered_watch_roots", return_value=roots), patch(
            "side_dog.cli.load_agent_identities",
            side_effect=lambda root: identities[os.fspath(root)],
        ), patch(
            "side_dog.cli.load_git_state",
            side_effect=lambda root: dict(git_states[os.fspath(root)]),
        ), patch(
            "side_dog.cli.load_github_pr", return_value=(None, "no pull requests found for branch")
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["board", "--once", "--width", "90", "--no-color", "--group", "repo"])
        self.assertEqual(code, 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[2], "herdr")
        self.assertIn("main", lines[3])
        self.assertEqual(lines[4], "side-dog")


class Phase4Fixtures:
    @staticmethod
    def rows_with_conflicts() -> list:
        from side_dog.board import LinkedIssue

        shared_issue = LinkedIssue("github.com/o/side-dog", 139, True)
        return [
            _row("claude-code:a", "Herdr · pane p3", "/work/side-dog", "fix/x", issues=(shared_issue,)),
            _row("codex:b", "Codex Desktop", "/Users/q/.codex/worktrees/abc/side-dog", "codex/issue-139", issues=(shared_issue,)),
            _row("codex:c", "Herdr · pane p5", "/work/side-dog", "fix/x"),
            _row("pi:d", "terminal", "/work/other", "main", repository="other", repository_key="/work/other/.git"),
            _row("claude-code:e", "VS Code", "/work/side-dog-wt2", "fix/x"),
            _row("claude-code:f", "kitty", "/work/side-dog", "fix/x", status="done"),
        ]


def _row(key, surface, working_root, branch, *, issues=(), repository="side-dog", repository_key="/work/side-dog/.git", status="working"):
    from side_dog.board import BoardRow

    agent, _, session_id = key.partition(":")
    return BoardRow(
        key=key,
        agent=agent,
        surface=surface,
        repository=repository,
        branch=branch,
        root="/work/side-dog",
        working_root=working_root,
        status=AgentStatus.from_wire(status),
        age_seconds=1.0,
        repository_key=repository_key,
        session_id=session_id,
        github={"url": "https://github.com/o/side-dog/pull/151", "number": 151, "state": "OPEN", "checks_total": 1, "checks_passed": 1, "checks_pending": 0, "checks_failed": 0} if key == "claude-code:a" else None,
        github_repository="github.com/o/side-dog",
        issues=tuple(issues),
    )


class DetailPredicateTest(TestCase):
    def test_a_session_row_matches_only_its_provider_qualified_events(self) -> None:
        from side_dog.board import event_belongs_to_row

        row = _row("codex:abc", "terminal", "/work/side-dog", "main")
        self.assertTrue(event_belongs_to_row({"agent": "codex", "session_id": "abc"}, row))
        self.assertFalse(event_belongs_to_row({"agent": "claude-code", "session_id": "abc"}, row))
        self.assertFalse(event_belongs_to_row({"agent": "codex", "session_id": "abcd"}, row))
        self.assertFalse(event_belongs_to_row({"agent": "codex"}, row))

    def test_a_pane_row_matches_its_pane_and_not_a_longer_one(self) -> None:
        from side_dog.board import BoardRow, event_belongs_to_row

        row = BoardRow(
            key="pane:w1:p1", agent="codex", surface="Herdr · pane w1:p1", repository="x",
            branch="main", root="/w", working_root="/w", status=AgentStatus.IDLE,
            age_seconds=None, pane_id="w1:p1",
        )
        self.assertTrue(event_belongs_to_row({"herdr_pane_id": "w1:p1"}, row))
        self.assertFalse(event_belongs_to_row({"herdr_pane_id": "w1:p10"}, row))
        self.assertFalse(event_belongs_to_row({"session_id": "w1:p1"}, row))


class ConflictTest(TestCase):
    def test_the_three_conflict_kinds_are_named_with_both_surfaces(self) -> None:
        from side_dog.board import conflicts

        rows = Phase4Fixtures.rows_with_conflicts()
        found = conflicts(rows)
        # a+c share a worktree; a+e and c+e share a branch across worktrees;
        # a+b share an issue. Four pairs, so the strip is capped at three.
        self.assertEqual(len(found), 3)
        self.assertEqual(found[0], "two sessions in side-dog: Herdr · pane p3 and Herdr · pane p5")
        self.assertTrue(found[1].startswith("two sessions on side-dog fix/x:"), found)
        self.assertIn("VS Code", found[1])
        self.assertEqual(found[2], "… 2 more conflicts")
        # Without the branch-sharing worktree, all three kinds show at once.
        trimmed = conflicts([row for row in rows if row.key != "claude-code:e"])
        self.assertEqual(
            trimmed,
            [
                "two sessions in side-dog: Herdr · pane p3 and Herdr · pane p5",
                "two sessions on side-dog#139: Herdr · pane p3 (fix/x) and Codex Desktop (codex/issue-139)",
            ],
        )

    def test_done_rows_and_other_repositories_do_not_conflict(self) -> None:
        from side_dog.board import conflicts

        rows = Phase4Fixtures.rows_with_conflicts()
        done = rows[5]
        self.assertTrue(all("kitty" not in text for text in conflicts(rows)))
        self.assertEqual(conflicts([rows[0], done]), [])
        self.assertEqual(conflicts([rows[3], rows[0]]), [])

    def test_the_same_issue_number_in_two_repositories_is_not_a_conflict(self) -> None:
        from side_dog.board import LinkedIssue, conflicts

        first = _row("claude-code:a", "A", "/work/a", "main", issues=(LinkedIssue("github.com/o/a", 7, True),), repository="a", repository_key="/work/a/.git")
        second = _row("codex:b", "B", "/work/b", "main", issues=(LinkedIssue("github.com/o/b", 7, True),), repository="b", repository_key="/work/b/.git")
        self.assertEqual(conflicts([first, second]), [])

    def test_a_bare_issue_number_conflicts_only_inside_one_repository(self) -> None:
        from side_dog.board import LinkedIssue, conflicts

        bare = (LinkedIssue("", 12, False),)
        first = _row("claude-code:a", "A", "/work/a", "fix/12", issues=bare, repository="a", repository_key="/work/a/.git")
        second = _row("codex:b", "B", "/work/b", "fix/12", issues=bare, repository="b", repository_key="/work/b/.git")
        self.assertEqual(conflicts([first, second]), [])
        sibling = _row("codex:c", "C", "/work/a-wt", "12-followup", issues=bare, repository="a", repository_key="/work/a/.git")
        found = conflicts([first, sibling])
        self.assertEqual(len(found), 1)
        self.assertIn("#12", found[0])

    def test_many_conflicts_are_capped_at_three_lines(self) -> None:
        from side_dog.board import conflicts

        rows = [_row(f"codex:{i}", f"S{i}", "/work/side-dog", "main") for i in range(5)]
        found = conflicts(rows)
        self.assertEqual(len(found), 3)
        self.assertTrue(found[-1].startswith("… "))
        self.assertIn("more conflicts", found[-1])


class SelectionTest(TestCase):
    def test_selection_follows_the_key_and_clamps_at_the_ends(self) -> None:
        from side_dog.board import move_selection, selected_index, sort_rows

        rows = sort_rows(rows_from_sources(mixed_sources(), NOW_MS))
        self.assertEqual(selected_index(rows, None), 0)
        self.assertEqual(selected_index(rows, "codex:d1"), [r.key for r in rows].index("codex:d1"))
        self.assertEqual(selected_index(rows, "gone:x"), 0)
        self.assertEqual(move_selection(rows, None, 1), rows[1].key)
        self.assertEqual(move_selection(rows, rows[0].key, -1), rows[0].key)
        self.assertEqual(move_selection(rows, rows[-1].key, 1), rows[-1].key)
        self.assertIsNone(move_selection([], None, 1))

    def test_urls_and_the_detail_heading(self) -> None:
        from side_dog.board import LinkedIssue, detail_title, issue_url, pr_url

        row = _row("claude-code:a", "Herdr · pane p3", "/work/side-dog", "fix/x", issues=(LinkedIssue("github.com/o/side-dog", 139, True), LinkedIssue("github.com/o/side-dog", 7, False)))
        self.assertEqual(pr_url(row), "https://github.com/o/side-dog/pull/151")
        self.assertEqual(pr_url(_row("codex:b", "x", "/w", "b")), "")
        self.assertEqual(issue_url(row.issues[0]), "https://github.com/o/side-dog/issues/139")
        self.assertEqual(issue_url(LinkedIssue("", 3, False)), "")
        self.assertEqual(
            detail_title(row), "Claude · Herdr · pane p3 · side-dog fix/x · #139, #7?"
        )


class Phase4RenderTest(TestCase):
    def test_selection_mark_conflict_strip_and_detail_pane(self) -> None:
        rows = Phase4Fixtures.rows_with_conflicts()
        from side_dog.board import conflicts

        screen = render_board(
            rows, 120, 24, False,
            selected="codex:c",
            warnings=conflicts(rows),
            detail=["│ 14:31:52 × test failed", "│ 14:31:40 ✎ edited cli.py"],
            detail_heading="Codex · Herdr · pane p5 · side-dog fix/x",
            hints="q quit",
        ).splitlines()
        marked = [line for line in screen if line.startswith("▸ ")]
        self.assertEqual(len(marked), 1)
        self.assertIn("Herdr · pane p5", marked[0])
        self.assertTrue(screen[1].startswith("  AGENT"))
        strip = [line for line in screen if line.startswith("⚠ ")]
        self.assertEqual(len(strip), 3)
        self.assertIn("Codex · Herdr · pane p5 · side-dog fix/x", screen)
        self.assertIn("│ 14:31:40 ✎ edited cli.py", screen)
        self.assertEqual(screen[-1], "q quit")
        self.assertEqual(len(screen), 24)
        for line in screen:
            self.assertLessEqual(len(line), 120)

    def test_detail_pane_takes_at_most_a_third_and_says_when_empty(self) -> None:
        rows = Phase4Fixtures.rows_with_conflicts()
        detail = [f"│ line {i}" for i in range(40)]
        screen = render_board(rows, 100, 18, False, selected="codex:c", detail=detail, detail_heading="h").splitlines()
        shown = [line for line in screen if line.startswith("│ line")]
        self.assertLessEqual(len(shown), 5)
        self.assertEqual(shown[-1], "│ line 39")
        empty = render_board(rows, 100, 18, False, selected="codex:c", detail=[], detail_heading="h")
        self.assertIn("no recent events for this session", empty)

    def test_once_frames_have_no_gutter_but_keep_the_strip(self) -> None:
        rows = Phase4Fixtures.rows_with_conflicts()
        from side_dog.board import conflicts

        screen = render_board(rows, 100, 20, False, warnings=conflicts(rows)).splitlines()
        self.assertTrue(screen[1].startswith("AGENT"))
        self.assertTrue(any(line.startswith("⚠ ") for line in screen))

    def test_a_short_frame_keeps_a_roster_row_before_its_extras(self) -> None:
        rows = Phase4Fixtures.rows_with_conflicts()
        from side_dog.board import conflicts

        screen = render_board(
            rows, 100, 8, False, selected="codex:c", warnings=conflicts(rows),
            detail=["│ one event"], detail_heading="h", hints="q quit",
        ).splitlines()
        self.assertEqual(len(screen), 8)
        self.assertTrue(any(line.startswith("▸ ") for line in screen), screen)
        self.assertNotIn("│ one event", screen)
        tiny = render_board(
            rows, 100, 4, False, selected="codex:c", warnings=conflicts(rows),
            detail=["│ one event"], detail_heading="h", hints="q quit",
        ).splitlines()
        self.assertTrue(any(line.startswith("▸ ") for line in tiny), tiny)

    def test_a_selected_row_below_the_fold_scrolls_into_view(self) -> None:
        rows = [_row(f"codex:{i:02d}", f"S{i}", f"/work/r{i}", "main", repository=f"r{i}", repository_key=f"/w/{i}") for i in range(30)]
        last = sort_rows(rows)[-1].key
        screen = render_board(rows, 100, 12, False, selected=last).splitlines()
        self.assertTrue(any(line.startswith("▸ ") for line in screen))


    def test_a_scrolled_grouped_row_keeps_its_group_header(self) -> None:
        rows = [_row(f"codex:{i:02d}", f"S{i}", f"/work/wt{i}", f"b{i}") for i in range(30)]
        last = sort_rows(rows, "repo")[-1].key
        screen = render_board(rows, 100, 12, False, group="repo", selected=last).splitlines()
        self.assertTrue(any(line.startswith("▸ ") for line in screen))
        self.assertIn("side-dog", [line.strip() for line in screen])


class DetachedConflictTest(TestCase):
    def test_two_detached_worktrees_do_not_share_a_branch(self) -> None:
        from side_dog.board import conflicts

        first = _row("claude-code:a", "A", "/work/a", "detached")
        second = _row("codex:b", "B", "/work/b", "detached")
        self.assertEqual(conflicts([first, second]), [])
        third = _row("codex:c", "C", "/work/c", "main")
        fourth = _row("codex:d", "D", "/work/d", "main")
        self.assertEqual(len(conflicts([third, fourth])), 1)


class DetailLinesTest(TestCase):
    def test_only_folders_that_reported_the_row_are_opened(self) -> None:
        from side_dog.cli import BoardRootState, board_detail_lines

        row = _row("codex:abc", "terminal", "/work/side-dog", "main")
        own = BoardRootState(root=Path("/work/side-dog"))
        reporter = BoardRootState(
            root=Path("/work/repo-root"),
            identities={"x": identity(agent="codex", session_id="abc", root="/work/repo-root")},
        )
        stranger = BoardRootState(root=Path("/work/other"))
        opened: list[Path] = []

        def fake_records(state):
            opened.append(state.root)
            return []

        with patch("side_dog.cli.board_detail_records", side_effect=fake_records):
            board_detail_lines(row, {s.root: s for s in (own, reporter, stranger)}, 80, False, NOW_MS)
        self.assertEqual(sorted(opened), [Path("/work/repo-root"), Path("/work/side-dog")])

    def test_records_are_read_from_the_tail_and_older_ones_are_kept(self) -> None:
        from side_dog.cli import BoardRootState, append_event, board_detail_records

        def event(session: str, detail: str) -> dict:
            return {
                "agent": "codex", "session_id": session, "kind": "file",
                "status": "success", "title": "Edited", "detail": detail,
            }

        with TemporaryDirectory() as directory, patch("side_dog.cli.BOARD_TAIL_BYTES", 700):
            root = (Path(directory) / "repo").resolve()
            root.mkdir()
            with patch.dict(os.environ, {STATE_ENV: os.fspath(Path(directory) / "state")}):
                append_event(root, event("old", "first.py"))
                state = BoardRootState(root=root)
                first = board_detail_records(state)
                self.assertEqual([r["session_id"] for r in first], ["old"])
                for index in range(12):
                    append_event(root, event("new", f"file-{index}.py"))
                second = board_detail_records(state)
                sessions = [r["session_id"] for r in second]
                self.assertEqual(sessions[0], "old")
                self.assertLess(len(sessions), 13)
                self.assertTrue(all(name == "new" for name in sessions[1:]))
                self.assertEqual(board_detail_records(state), second)

    def test_retention_is_per_live_session_not_per_folder(self) -> None:
        from side_dog.cli import BoardRootState, board_detail_records

        def record(session: str, epoch: int) -> dict:
            return {"agent": "codex", "session_id": session, "epoch_ms": epoch, "kind": "file", "title": "Edited", "detail": f"{session}-{epoch}"}

        state = BoardRootState(
            root=Path("/work/x"),
            identities={"quiet": identity(agent="codex", session_id="quiet")},
            detail_records=[record("quiet", 1), record("gone", 2)],
            detail_stamp=(1, 1),
        )
        fresh = [record("busy", epoch) for epoch in range(10, 260)]

        class Stat:
            st_mtime_ns = 2
            st_size = 5

        with patch("side_dog.cli.events_path", return_value=Path("/work/x/events.jsonl")), patch(
            "side_dog.cli.Path.stat", return_value=Stat()
        ), patch("side_dog.cli._board_tail_position", return_value=0), patch(
            "side_dog.cli.read_new_events", return_value=(fresh, 0)
        ):
            merged = board_detail_records(state)
        sessions = [r["session_id"] for r in merged]
        self.assertEqual(sessions[0], "quiet")
        self.assertNotIn("gone", sessions)
        self.assertEqual(sessions.count("busy"), 250)

    def test_retention_keeps_pane_records_with_unknown_session(self) -> None:
        from side_dog.cli import BoardRootState, _board_record_row_key, board_detail_records

        record = {
            "agent": "codex", "session_id": "unknown", "herdr_pane_id": "w1:p3",
            "epoch_ms": 1, "kind": "file", "title": "Edited", "detail": "old",
        }
        self.assertEqual(_board_record_row_key(record), "pane:w1:p3")
        state = BoardRootState(
            root=Path("/work/x"),
            identities={"pane": identity(agent="codex", session_id="unknown", pane_id="w1:p3")},
            detail_records=[record],
            detail_stamp=(1, 1),
        )

        class Stat:
            st_mtime_ns = 2
            st_size = 5

        with patch("side_dog.cli.events_path", return_value=Path("/work/x/events.jsonl")), patch(
            "side_dog.cli.Path.stat", return_value=Stat()
        ), patch("side_dog.cli._board_tail_position", return_value=0), patch(
            "side_dog.cli.read_new_events", return_value=([], 0)
        ):
            merged = board_detail_records(state)
        self.assertEqual(merged, [record])

    def test_detail_lines_come_only_from_the_selected_session(self) -> None:
        from side_dog.cli import BoardRootState, board_detail_lines

        row = _row("codex:abc", "terminal", "/work/side-dog", "main")
        state = BoardRootState(root=Path("/work/side-dog"))
        events = [
            {"agent": "codex", "session_id": "abc", "epoch_ms": NOW_MS - 5000, "kind": "file", "status": "success", "title": "Edited", "detail": "cli.py", "timestamp": "2027-01-15T10:00:00Z"},
            {"agent": "codex", "session_id": "abcd", "epoch_ms": NOW_MS - 4000, "kind": "test", "status": "failed", "title": "Tests failed", "detail": "x", "timestamp": "2027-01-15T10:00:01Z"},
            {"agent": "claude-code", "session_id": "abc", "epoch_ms": NOW_MS - 3000, "kind": "commit", "status": "success", "title": "Committed", "detail": "y", "timestamp": "2027-01-15T10:00:02Z"},
        ]
        with patch("side_dog.cli.board_detail_records", return_value=events):
            lines = board_detail_lines(row, {state.root: state}, 100, False, NOW_MS)
        self.assertEqual(len(lines), 1)
        self.assertIn("Edited", lines[0])
        self.assertNotIn("Tests failed", "\n".join(lines))


# Phase 6: notifications on board transitions.


def _github(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "url": "https://github.com/o/side-dog/pull/151",
        "number": 151,
        "state": "OPEN",
        "review": "",
        "checks_total": 2,
        "checks_passed": 2,
        "checks_pending": 0,
        "checks_failed": 0,
    }
    base.update(overrides)
    return base


def _pr_row(status: str = "working", key: str = "claude-code:a", **github: object) -> BoardRow:
    from dataclasses import replace

    return replace(
        _row(key, "Herdr · pane p3", "/work/side-dog", "fix/x", status=status),
        github=_github(**github),
        issues=(LinkedIssue("github.com/o/side-dog", 139, True),),
    )


class TransitionTest(TestCase):
    def transitions(self, previous, current, before=(), after=()):
        from side_dog.board import board_transitions

        return board_transitions(previous, current, list(before), list(after))

    def test_ci_passing_while_the_row_idles_notifies_with_board_facts_only(self) -> None:
        from side_dog.board import TRANSITION_CI_PASSED

        before = [_pr_row("idle", checks_passed=1, checks_pending=1)]
        after = [_pr_row("idle")]
        [found] = self.transitions(before, after)
        self.assertEqual(found.key, ("claude-code:a", TRANSITION_CI_PASSED))
        self.assertEqual(found.title, "PR #151 checks passed")
        self.assertEqual(
            found.body, "Claude · Herdr · pane p3 · side-dog fix/x · PR #151 · #139 is idle"
        )

    def test_a_row_resting_with_green_checks_notifies_when_it_stops_working(self) -> None:
        [found] = self.transitions([_pr_row("working")], [_pr_row("done")])
        self.assertEqual(found.title, "PR #151 checks passed")
        self.assertTrue(found.body.endswith(" is finished"), found.body)

    def test_green_checks_while_the_row_works_do_not_notify(self) -> None:
        before = [_pr_row("working", checks_passed=1, checks_pending=1)]
        self.assertEqual(self.transitions(before, [_pr_row("working")]), [])

    def test_an_approved_review_on_an_idle_row_notifies(self) -> None:
        from side_dog.board import TRANSITION_APPROVED

        before = [_pr_row("idle", checks_passed=0, checks_pending=2)]
        after = [_pr_row("idle", checks_passed=0, checks_pending=2, review="APPROVED")]
        [found] = self.transitions(before, after)
        self.assertEqual(found.key, ("claude-code:a", TRANSITION_APPROVED))
        self.assertEqual(found.title, "PR #151 approved")

    def test_a_merged_or_closed_pull_request_is_not_news(self) -> None:
        before = [_pr_row("idle", checks_passed=1, checks_pending=1)]
        self.assertEqual(self.transitions(before, [_pr_row("idle", state="MERGED")]), [])
        self.assertEqual(self.transitions(before, [_pr_row("idle", state="CLOSED")]), [])

    def test_the_first_readback_of_a_pull_request_is_catching_up_not_news(self) -> None:
        from dataclasses import replace

        before = [replace(_pr_row("idle"), github=None)]
        self.assertEqual(self.transitions(before, [_pr_row("idle")]), [])

    def test_a_failed_readback_placeholder_is_not_a_prior_reading_either(self) -> None:
        from dataclasses import replace

        # ``apply_board_github`` keeps this shape when gh could not answer and
        # nothing was known before; the cell reads ``PR ?``.
        placeholder = {"state": "UNKNOWN", "coverage": "PARTIAL", "error": "gh: timeout"}
        before = [replace(_pr_row("idle"), github=placeholder)]
        self.assertEqual(self.transitions(before, [_pr_row("idle")]), [])
        self.assertEqual(
            self.transitions(before, [_pr_row("idle", review="APPROVED")]), []
        )

    def test_reopening_a_pull_request_with_the_checks_it_closed_with_is_quiet(self) -> None:
        closed = [_pr_row("idle", state="CLOSED", review="APPROVED")]
        reopened = [_pr_row("idle", review="APPROVED")]
        self.assertEqual(self.transitions(closed, reopened), [])
        merged = [_pr_row("idle", state="MERGED")]
        self.assertEqual(self.transitions(merged, [_pr_row("idle")]), [])
        # Open and pending, then open and green, is still news.
        pending = [_pr_row("idle", checks_passed=1, checks_pending=1)]
        [found] = self.transitions(pending, [_pr_row("idle")])
        self.assertEqual(found.title, "PR #151 checks passed")

    def test_a_different_pull_request_number_is_a_new_request_not_a_transition(
        self,
    ) -> None:
        before = [_pr_row("idle", number=150, checks_passed=1, checks_pending=1)]
        self.assertEqual(self.transitions(before, [_pr_row("idle", number=151)]), [])
        # The same request going green is still news.
        before = [_pr_row("idle", number=151, checks_passed=1, checks_pending=1)]
        self.assertEqual(len(self.transitions(before, [_pr_row("idle", number=151)])), 1)

    def test_a_row_that_was_not_on_the_previous_frame_does_not_notify(self) -> None:
        self.assertEqual(self.transitions([], [_pr_row("idle")]), [])
        blocked = _row("codex:b", "Codex Desktop", "/work/side-dog", "fix/y", status="blocked")
        self.assertEqual(self.transitions([], [blocked]), [])

    def test_a_row_blocking_alone_in_its_repository_notifies(self) -> None:
        from side_dog.board import TRANSITION_BLOCKED

        working = _row("codex:b", "Codex Desktop", "/work/side-dog", "fix/y")
        blocked = _row("codex:b", "Codex Desktop", "/work/side-dog", "fix/y", status="blocked")
        [found] = self.transitions([working], [blocked])
        self.assertEqual(found.key, ("codex:b", TRANSITION_BLOCKED))
        self.assertEqual(found.title, "Codex is blocked")
        self.assertEqual(
            found.body,
            "Codex · Codex Desktop · side-dog fix/y; nothing else is working in side-dog",
        )

    def test_a_blocked_row_stays_quiet_while_another_row_works_in_the_same_repository(
        self,
    ) -> None:
        working = _row("codex:b", "Codex Desktop", "/work/side-dog", "fix/y")
        blocked = _row("codex:b", "Codex Desktop", "/work/side-dog", "fix/y", status="blocked")
        other = _row("claude-code:a", "Herdr · pane p3", "/work/side-dog", "fix/x")
        self.assertEqual(self.transitions([working, other], [blocked, other]), [])
        # A worker in a different repository does not count.
        elsewhere = _row(
            "pi:d", "terminal", "/work/other", "main",
            repository="other", repository_key="/work/other/.git",
        )
        [found] = self.transitions([working, elsewhere], [blocked, elsewhere])
        self.assertEqual(found.title, "Codex is blocked")
        # And when the other worker finishes, the blocked row becomes the news
        # (alongside the finisher's own green pull request).
        idle = _row("claude-code:a", "Herdr · pane p3", "/work/side-dog", "fix/x", status="idle")
        found = self.transitions([blocked, other], [blocked, idle])
        self.assertEqual(
            [n.key for n in found],
            [("codex:b", "blocked"), ("claude-code:a", "ci-passed")],
        )

    def test_a_new_conflict_notifies_once_with_its_line(self) -> None:
        from side_dog.board import TRANSITION_CONFLICT, detect_conflicts, shown_conflicts

        rows = Phase4Fixtures.rows_with_conflicts()
        details = detect_conflicts(rows)
        self.assertEqual(len(details), 4)
        self.assertEqual(len(shown_conflicts(details)), 2)
        first, second, third, fourth = details
        self.assertEqual(first.identity, "worktree:claude-code:a+codex:c")
        [found] = self.transitions(rows, rows, [], [first])
        self.assertEqual(found.key, (first.identity, TRANSITION_CONFLICT))
        self.assertEqual(found.title, "Board conflict")
        self.assertEqual(
            found.body, "two sessions in side-dog: Herdr · pane p3 and Herdr · pane p5"
        )
        self.assertEqual(self.transitions(rows, rows, [first], [first]), [])
        [found] = self.transitions(rows, rows, [first], [first, second])
        self.assertEqual(found.key[0], second.identity)
        # A conflict the strip hides behind its overflow line is still news:
        # it is the one the person cannot see.
        found = self.transitions(rows, rows, [first, second], details)
        self.assertEqual([n.key[0] for n in found], [third.identity, fourth.identity])

    def test_the_same_pair_moving_to_another_issue_or_branch_is_a_new_conflict(
        self,
    ) -> None:
        from side_dog.board import detect_conflicts

        def pair(issue: int) -> list[BoardRow]:
            linked = (LinkedIssue("github.com/o/side-dog", issue, True),)
            return [
                _row("claude-code:a", "Herdr · pane p3", "/work/side-dog", "fix/a", issues=linked),
                _row("codex:b", "Codex Desktop", "/work/wt-b", "fix/b", issues=linked),
            ]

        seven = detect_conflicts(pair(7))
        nine = detect_conflicts(pair(9))
        self.assertEqual(seven[0].identity, "issue:side-dog#7:claude-code:a+codex:b")
        self.assertEqual(nine[0].identity, "issue:side-dog#9:claude-code:a+codex:b")
        [found] = self.transitions(pair(7), pair(9), seven, nine)
        self.assertEqual(found.body, "two sessions on side-dog#9: Herdr · pane p3 (fix/a) and Codex Desktop (fix/b)")
        self.assertEqual(self.transitions(pair(7), pair(7), seven, seven), [])

        def on_branch(branch: str) -> list[BoardRow]:
            return [
                _row("claude-code:a", "Herdr · pane p3", "/work/side-dog", branch),
                _row("codex:b", "Codex Desktop", "/work/wt-b", branch),
            ]

        x = detect_conflicts(on_branch("fix/x"))
        y = detect_conflicts(on_branch("fix/y"))
        self.assertEqual(x[0].identity, "branch:side-dog:fix/x:claude-code:a+codex:b")
        [found] = self.transitions(on_branch("fix/x"), on_branch("fix/y"), x, y)
        self.assertEqual(found.key[0], y[0].identity)

    def test_a_conflict_keeps_the_full_repository_so_two_apis_do_not_collide(self) -> None:
        from dataclasses import replace

        from side_dog.board import detect_conflicts

        def pair(owner: str, issue: bool) -> list[BoardRow]:
            linked = (LinkedIssue(f"github.com/{owner}/api", 7, True),) if issue else ()
            rows = [
                _row("claude-code:a", "Herdr · pane p3", "/work/api", "fix/a" if issue else "main", issues=linked, repository="api"),
                _row("codex:b", "Codex Desktop", "/work/wt-b", "fix/b" if issue else "main", issues=linked, repository="api"),
            ]
            remote = f"github.com/{owner}/api"
            return [replace(row, github_repository=remote, remote_repository=remote) for row in rows]

        first = detect_conflicts(pair("owner-a", True))
        second = detect_conflicts(pair("owner-b", True))
        # The strip line is the same in both frames; only the identity tells.
        self.assertEqual(first[0].text, second[0].text)
        self.assertEqual(first[0].text, "two sessions on api#7: Herdr · pane p3 (fix/a) and Codex Desktop (fix/b)")
        self.assertEqual(first[0].repository, "github.com/owner-a/api")
        self.assertEqual(first[0].identity, "issue:github.com/owner-a/api#7:claude-code:a+codex:b")
        [found] = self.transitions(pair("owner-a", True), pair("owner-b", True), first, second)
        self.assertEqual(found.key[0], second[0].identity)

        a = detect_conflicts(pair("owner-a", False))
        b = detect_conflicts(pair("owner-b", False))
        self.assertEqual(a[0].text, b[0].text)
        self.assertEqual(a[0].identity, "branch:github.com/owner-a/api:main:claude-code:a+codex:b")
        [found] = self.transitions(pair("owner-a", False), pair("owner-b", False), a, b)
        self.assertEqual(found.key[0], b[0].identity)
        # No conflict field carries a path.
        for conflict in (*first, *a):
            for value in (conflict.repository, conflict.branch, conflict.text):
                self.assertNotIn("/work", value)

    def test_the_pull_request_readback_renaming_the_repository_is_not_a_new_conflict(
        self,
    ) -> None:
        from dataclasses import replace

        from side_dog.board import BoardNotifier, detect_conflicts

        fork, upstream = "github.com/me/api", "github.com/owner/api"

        def frame(after_readback: bool) -> list[BoardRow]:
            # Before the readback the folder's repository is its origin remote,
            # a fork, and #7 is inferred from the branch there. The PR readback
            # names upstream and confirms #7 as one of its closing issues.
            issues = (LinkedIssue(upstream if after_readback else fork, 7, after_readback),)
            github = (
                {"url": "https://github.com/owner/api/pull/9", "number": 9, "state": "OPEN"}
                if after_readback
                else None
            )
            rows = [
                _row("claude-code:a", "Herdr · pane p3", "/work/api", "fix/7", issues=issues, repository="api"),
                _row("codex:b", "Codex Desktop", "/work/wt-b", "fix/7", issues=issues, repository="api"),
            ]
            return [
                replace(
                    row,
                    github=github,
                    github_repository=upstream if after_readback else fork,
                    remote_repository=fork,
                )
                for row in rows
            ]

        before, after = frame(False), frame(True)
        self.assertNotEqual(before[0].github_repository, after[0].github_repository)
        self.assertNotEqual(before[0].issues, after[0].issues)
        # Same branch across two worktrees, and the same issue: one conflict,
        # reported for the branch, keyed on the remote in both frames.
        [b] = detect_conflicts(before)
        [a] = detect_conflicts(after)
        self.assertEqual(b.identity, "branch:github.com/me/api:fix/7:claude-code:a+codex:b")
        self.assertEqual(a.identity, b.identity)
        notifier = BoardNotifier()
        notifier.tick(before, [b])
        self.assertEqual(notifier.tick(after, [a]), [])

        # The same holds when only the issue is shared.
        def issue_only(rows: list[BoardRow]) -> list[BoardRow]:
            return [replace(row, branch=f"topic-{i}") for i, row in enumerate(rows)]

        [b] = detect_conflicts(issue_only(before))
        [a] = detect_conflicts(issue_only(after))
        self.assertEqual(b.identity, "issue:github.com/me/api#7:claude-code:a+codex:b")
        self.assertEqual(a.identity, b.identity)
        self.assertEqual(self.transitions(issue_only(before), issue_only(after), [b], [a]), [])

    def test_a_conflict_hidden_by_the_overflow_line_is_not_new_when_it_resurfaces(
        self,
    ) -> None:
        from side_dog.board import BoardNotifier, conflict_lines, detect_conflicts

        def session(i: int, worktree: int) -> BoardRow:
            return _row(
                f"codex:{i}", f"S{i}", f"/work/wt{worktree}", "main",
                repository=f"r{i}", repository_key=f"/work/wt{i}/.git",
            )

        # Three worktree conflicts: (0,1), (2,3), (4,5); all three fit the strip.
        three = [session(i, i - i % 2) for i in range(6)]
        # A seventh session in the first worktree adds two more pairs, so the
        # strip shows two conflicts and hides the third behind the overflow.
        four = [*three, session(6, 0)]
        self.assertEqual(len(detect_conflicts(three)), 3)
        self.assertEqual(len(detect_conflicts(four)), 5)
        self.assertTrue(conflict_lines(detect_conflicts(four))[-1].startswith("… "))
        notifier = BoardNotifier()
        self.assertEqual(notifier.tick(three, detect_conflicts(three)), [])
        newly = notifier.tick(four, detect_conflicts(four))
        original = {c.identity for c in detect_conflicts(three)}
        self.assertEqual(len(newly), 2)
        self.assertTrue(all(n.key[0] not in original for n in newly))
        # The seventh session leaves; the third conflict is back in the strip
        # but never lapsed, so nothing is announced.
        self.assertEqual(notifier.tick(three, detect_conflicts(three)), [])

    def test_a_conflict_keeps_its_identity_when_its_sessions_trade_places(self) -> None:
        from dataclasses import replace

        from side_dog.board import detect_conflicts

        first = _row("claude-code:a", "Herdr · pane p3", "/work/side-dog", "fix/x")
        second = _row("codex:c", "Herdr · pane p5", "/work/side-dog", "fix/x", status="idle")
        before = detect_conflicts([first, second])
        after = detect_conflicts(
            [replace(first, status=AgentStatus.IDLE), replace(second, status=AgentStatus.WORKING)]
        )
        # The strip line now names them the other way round...
        self.assertEqual(before[0].text, "two sessions in side-dog: Herdr · pane p3 and Herdr · pane p5")
        self.assertEqual(after[0].text, "two sessions in side-dog: Herdr · pane p5 and Herdr · pane p3")
        # ...but the conflict never lapsed, so nothing is announced again.
        self.assertEqual(before[0].identity, after[0].identity)
        self.assertEqual(self.transitions([first, second], [first, second], before, after), [])

    def test_bodies_name_no_folder(self) -> None:
        from side_dog.board import board_conditions, detect_conflicts

        rows = [
            _pr_row("idle"),
            _row("codex:b", "Codex Desktop", "/Users/q/.codex/worktrees/abc/side-dog", "fix/y", status="blocked"),
        ]
        found = board_conditions(rows, detect_conflicts(rows))
        self.assertEqual(sorted(kind for _, kind in found), ["blocked", "ci-passed"])
        for notification in found.values():
            text = f"{notification.title} {notification.body}"
            for row in rows:
                self.assertNotIn(row.root, text)
                self.assertNotIn(row.working_root, text)
            self.assertIsNone(re.search(r"(^|\s)/", text), text)


class NotifierTest(TestCase):
    def test_the_first_tick_is_a_baseline_and_unchanged_frames_stay_quiet(self) -> None:
        from side_dog.board import BoardNotifier

        from side_dog.board import Conflict

        notifier = BoardNotifier()
        rows = [_pr_row("idle")]
        line = Conflict("worktree", ("a", "b"), "side-dog", "", None, "two sessions in side-dog: A and B")
        self.assertEqual(notifier.tick(rows, [line]), [])
        self.assertEqual(notifier.tick(rows, [line]), [])
        self.assertEqual(notifier.tick(rows, [line]), [])

    def test_a_condition_notifies_once_until_it_lapses_and_returns(self) -> None:
        from side_dog.board import BoardNotifier

        notifier = BoardNotifier()
        pending = [_pr_row("idle", checks_passed=1, checks_pending=1)]
        green = [_pr_row("idle")]
        red = [_pr_row("idle", checks_passed=1, checks_failed=1)]
        notifier.tick(pending, [])
        self.assertEqual([n.title for n in notifier.tick(green, [])], ["PR #151 checks passed"])
        self.assertEqual(notifier.tick(green, []), [])
        self.assertEqual(notifier.tick(green, []), [])
        self.assertEqual(notifier.tick(red, []), [])
        self.assertEqual([n.title for n in notifier.tick(green, [])], ["PR #151 checks passed"])
        # Starting to work again also resets the condition.
        self.assertEqual(notifier.tick([_pr_row("working")], []), [])
        self.assertEqual([n.title for n in notifier.tick(green, [])], ["PR #151 checks passed"])

    def test_a_conflict_that_clears_and_returns_is_news_both_times(self) -> None:
        from side_dog.board import BoardNotifier, detect_conflicts

        notifier = BoardNotifier()
        rows = Phase4Fixtures.rows_with_conflicts()
        line = detect_conflicts(rows)[0]
        notifier.tick(rows, [])
        self.assertEqual(len(notifier.tick(rows, [line])), 1)
        self.assertEqual(notifier.tick(rows, [line]), [])
        self.assertEqual(notifier.tick(rows, []), [])
        self.assertEqual(len(notifier.tick(rows, [line])), 1)


class NotificationDeliveryTest(TestCase):
    def test_the_board_command_passes_no_notify_through(self) -> None:
        with patch("side_dog.cli.board", return_value=0) as run:
            self.assertEqual(main(["board", "--no-notify"]), 0)
            self.assertIs(run.call_args.kwargs["no_notify"], True)
            self.assertEqual(main(["board"]), 0)
            self.assertIs(run.call_args.kwargs["no_notify"], False)

    def test_the_flag_and_the_config_switch_both_turn_notifications_off(self) -> None:
        from side_dog.cli import board_notifications_enabled

        self.assertTrue(board_notifications_enabled({}, False))
        self.assertFalse(board_notifications_enabled({}, True))
        self.assertFalse(board_notifications_enabled({"notify": {"enabled": False}}, False))
        self.assertTrue(board_notifications_enabled({"notify": {"enabled": "yes"}}, False))

    def test_a_disabled_delivery_never_calls_the_notifier(self) -> None:
        from side_dog.cli import BoardNotificationDelivery

        delivery = BoardNotificationDelivery(enabled=False)
        with patch("side_dog.cli.notify_for_board") as send:
            delivery.frame([_pr_row("idle", checks_pending=1, checks_passed=1)], [], 10.0)
            delivery.frame([_pr_row("idle")], [], 12.0)
            delivery.frame([_pr_row("idle")], [], 14.0)
        send.assert_not_called()

    def test_an_enabled_delivery_sends_one_message_per_second_at_most(self) -> None:
        from side_dog.cli import BoardNotificationDelivery

        delivery = BoardNotificationDelivery(enabled=True)
        pending = _pr_row("idle", checks_pending=2, checks_passed=0)
        green = _pr_row("idle", review="APPROVED")
        with patch("side_dog.cli.notify_for_board") as send:
            delivery.frame([pending], [], 10.0)
            # Two transitions at once: checks passed and review approved.
            delivery.frame([green], [], 10.75)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0], "PR #151 checks passed")
            delivery.frame([green], [], 11.5)
            self.assertEqual(send.call_count, 1)
            delivery.frame([green], [], 11.75)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(send.call_args.args[0], "PR #151 approved")
            delivery.frame([green], [], 30.0)
            self.assertEqual(send.call_count, 2)

    def test_a_burst_beyond_the_backlog_is_dropped_rather_than_delivered_late(self) -> None:
        from side_dog.cli import BOARD_NOTIFY_BACKLOG, BoardNotificationDelivery

        delivery = BoardNotificationDelivery(enabled=True)
        rows = [_row(f"codex:{i}", f"S{i}", "/work/side-dog", "main") for i in range(40)]
        delivery.frame(rows, [], 0.0)
        from side_dog.board import Conflict

        lines = [
            Conflict("worktree", (f"codex:{i}", f"codex:{i + 1}"), "side-dog", "main", None, f"two sessions in side-dog: S{i} and S{i + 1}")
            for i in range(40)
        ]
        with patch("side_dog.cli.notify_for_board") as send:
            delivery.frame(rows, lines, 1.0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(delivery.backlog), BOARD_NOTIFY_BACKLOG - 1)
