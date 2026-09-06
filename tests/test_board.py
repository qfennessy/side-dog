import io
import json
import os
import re
import time
from concurrent.futures import Future
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.board import (
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
    CLAUDE_SURFACE_NAMES,
    GITHUB_PR_FIELDS,
    STATE_ENV,
    BoardGithubRequest,
    BoardRootState,
    board_activity_tail,
    board_history_tail,
    board_source,
    codex_surface,
    collect_board_github,
    discovered_watch_roots,
    events_path,
    main,
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
            again = board_history_tail(path, stamp, {"x": 1}, {"y": ()})
            self.assertEqual(again, ({"x": 1}, {"y": ()}, stamp))
            # A changed file re-reads: sessions whose records slid out keep
            # their previous entries, sessions in the tail take the tail's.
            previous_issues = {
                "codex:a": (IssueCommand(1, 99, ""),),
                "claude-code:gone": (IssueCommand(2, 5, ""),),
            }
            _, merged, _ = board_history_tail(path, (1, 1), {}, previous_issues)
            self.assertEqual(merged["codex:a"][0].number, 12)
            self.assertEqual(merged["claude-code:gone"], (IssueCommand(2, 5, ""),))
            self.assertEqual(previous_issues["codex:a"], (IssueCommand(1, 99, ""),))
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
            self.assertEqual(board_source(state).github_repository, "github.com/fork/r")
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

            def submit(self, function, *args):
                self.calls += 1
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
        self.assertEqual(pending[root].branch, "feat/b")
        collect_board_github({root: state}, pending)
        self.assertEqual(state.github_refresh_status, "complete")
        self.assertEqual(pending, {})

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

    def test_a_readback_for_a_left_branch_is_ignored(self) -> None:
        root = Path("/work/side-dog")
        state = BoardRootState(root=root, git_status={"branch": "feat/b", "repository": "x"})
        future = Future()
        future.set_result((github(branch="feat/a"), None))
        pending = {root: BoardGithubRequest(future, "feat/a")}
        collect_board_github({root: state}, pending)
        self.assertIsNone(state.github_status)
        self.assertEqual(pending, {})


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

        def fake_github(root: Path):
            if root == roots[0]:
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
