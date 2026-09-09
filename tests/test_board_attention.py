"""Board's shared masthead, Attention mode, and the session brief."""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog import __version__
from side_dog.board import (
    BRIEF_FIELDS,
    CUE_ADDRESS_REVIEW,
    CUE_CHECK_STALLED,
    CUE_FIX_CHECKS,
    CUE_INVESTIGATE_TESTS,
    CUE_MERGE_PR,
    CUE_NONE,
    CUE_RESOLVE_CONFLICT,
    CUE_REVIEW_PR,
    CUE_UNBLOCK,
    STALE_WORK_SECONDS,
    BoardRow,
    BoardRowWire,
    BoardSource,
    LinkedIssue,
    SessionBrief,
    attention_empty_lines,
    attention_reason,
    attention_reasons,
    attention_rows,
    board_rows_payload,
    board_scope_label,
    brief_lines,
    detect_conflicts,
    next_mode,
    render_board,
    rows_from_sources,
    session_brief,
    sort_rows,
)
from side_dog.cli import (
    STATE_ENV,
    BoardRootState,
    TerminalViewSwitch,
    board_hints,
    board_history_scan,
    board_session_brief,
    board_source,
    events_path,
    main,
    render_board_help,
    status_bar,
    style_status_bar,
    terminal_view_switch_for_key,
)
from side_dog.integrations import AgentStatus

NOW_MS = 1_800_000_000_000


def github(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "url": "https://github.com/o/side-dog/pull/151",
        "number": 151,
        "state": "OPEN",
        "title": "Add the board",
        "checks_total": 2,
        "checks_passed": 2,
        "checks_pending": 0,
        "checks_failed": 0,
        "review": "REVIEW_REQUIRED",
    }
    base.update(overrides)
    return base


def row(
    key: str = "codex:a",
    *,
    status: str = "working",
    age: float | None = 5.0,
    pr: dict[str, object] | None = None,
    surface: str = "terminal",
    working_root: str = "/work/side-dog",
    branch: str = "feat/board",
    last_test: str = "",
    issues: tuple[LinkedIssue, ...] = (),
) -> BoardRow:
    agent, _, session_id = key.partition(":")
    return BoardRow(
        key=key,
        agent=agent,
        surface=surface,
        repository="side-dog",
        repository_key="/work/side-dog/.git",
        branch=branch,
        root="/work/side-dog",
        working_root=working_root,
        status=AgentStatus.from_wire(status),
        age_seconds=age,
        session_id=session_id,
        github=pr,
        github_repository="github.com/o/side-dog",
        issues=issues,
        last_test=last_test,
    )


def event(kind: str, status: str, age_seconds: int, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "agent": "codex",
        "session_id": "a",
        "kind": kind,
        "status": status,
        "epoch_ms": NOW_MS - age_seconds * 1000,
        "title": "/Users/q/secret/prompt.txt",
        "detail": "rm -rf /work/private && cat ~/.ssh/id_rsa",
    }
    record.update(extra)
    return record


class MastheadTest(TestCase):
    """Issue 216: Board keeps the same SIDE DOG header as Watch."""

    def test_the_board_masthead_names_the_view_and_degrades_like_watch(self) -> None:
        rows = [row(), row("codex:b", status="idle")]
        scope = board_scope_label(rows)
        self.assertEqual(scope, "Board · 2 sessions · 1 repo")
        wide = status_bar(__version__, scope, 1, 100, "10:33:58")
        self.assertTrue(wide.startswith(f"SIDE DOG v{__version__} · Board · 2 sessions · 1 repo · 1 working ╱"))
        self.assertTrue(wide.endswith(" 10:33:58"))
        self.assertEqual(len(wide), 100)
        # A long scope in a narrow pane drops the working count, then the
        # scope, then the version, and never the product name or the clock.
        long_scope = board_scope_label([row(f"codex:{i}") for i in range(12)], mode="attention")
        for width in (60, 40, 30, 24, 12):
            line = status_bar(__version__, long_scope, 12, width, "10:33:58")
            self.assertLessEqual(len(line), width, line)
            self.assertTrue(line.startswith("SIDE DOG") or line.endswith("10:33:58"), line)
        self.assertNotIn("\x1b[", style_status_bar(wide, False))
        self.assertIn("SIDE DOG", style_status_bar(wide, True))

    def test_the_masthead_survives_short_and_narrow_frames_in_both_modes(self) -> None:
        rows = [row(), row("codex:b", status="blocked", surface="kitty", working_root="/work/wt-b", branch="fix/b")]
        conflicts = detect_conflicts(rows)
        reasons = attention_reasons(rows, conflicts)
        for mode in ("all", "attention"):
            shown = attention_rows(rows, conflicts) if mode == "attention" else rows
            scope = board_scope_label(rows, shown, mode)
            for width, height in ((120, 24), (80, 12), (40, 6), (28, 4), (20, 4)):
                with self.subTest(mode=mode, width=width, height=height):
                    masthead = status_bar(__version__, scope, 1, width, "10:33:58")
                    screen = render_board(
                        shown,
                        width,
                        height,
                        False,
                        masthead=masthead,
                        selected=shown[0].key,
                        warnings=["side-dog folder terminal and kitty"],
                        detail=["│ 10:33 ✎ edited"],
                        detail_heading="Codex · terminal",
                        hints=board_hints(False),
                        reasons=reasons if mode == "attention" else None,
                        brief=session_brief(shown[0], [], NOW_MS),
                    ).splitlines()
                    self.assertEqual(screen[0], masthead)
                    self.assertLessEqual(len(screen), height)
                    self.assertTrue(any(line.startswith("▸ ") for line in screen), screen)
                    for line in screen:
                        self.assertLessEqual(len(line), width)
        self.assertIn("Attention · 1 of 2 sessions", board_scope_label(rows, attention_rows(rows, conflicts), "attention"))

    def test_direct_launch_prints_the_shared_masthead(self) -> None:
        with TemporaryDirectory() as tmp, patch.dict("os.environ", {STATE_ENV: tmp}):
            root = Path(tmp) / "empty"
            root.mkdir()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["board", str(root), "--once", "--width", "70", "--no-color"])
        self.assertEqual(code, 0)
        first = stdout.getvalue().splitlines()[0]
        self.assertTrue(first.startswith(f"SIDE DOG v{__version__} · Board · 0 sessions"), first)
        self.assertNotIn("\x1b[", first)

    def test_switching_views_remembers_the_board_mode(self) -> None:
        switch = terminal_view_switch_for_key(
            "board", b"w", board_group="repo", board_show_detail=True, board_mode="attention"
        )
        self.assertEqual(switch, TerminalViewSwitch("watch", board_group="repo", board_show_detail=True, board_mode="attention"))
        with self.assertRaises(ValueError):
            TerminalViewSwitch("watch", board_mode="contributions")
        calls: list[dict[str, object]] = []

        def fake_watch(projects: object, **kwargs: object) -> TerminalViewSwitch:
            return TerminalViewSwitch("board")

        def fake_board(**kwargs: object) -> int | TerminalViewSwitch:
            calls.append(kwargs)
            if len(calls) == 1:
                return TerminalViewSwitch("watch", board_mode="attention")
            return 0

        with (
            patch("side_dog.cli.watch", side_effect=fake_watch),
            patch("side_dog.cli.board", side_effect=fake_board),
            patch("side_dog.cli.load_config", return_value={}),
        ):
            self.assertEqual(main(["watch", ".", "--no-color"]), 0)
        self.assertEqual(calls[0]["mode"], "all")
        self.assertEqual(calls[1]["mode"], "attention")


class AttentionPredicateTest(TestCase):
    """Issue 225: which sessions need a person, and why."""

    def test_healthy_rows_need_no_attention(self) -> None:
        self.assertEqual(attention_reason(row()), "")
        self.assertEqual(attention_reason(row(status="idle", age=None)), "")
        self.assertEqual(attention_reason(row(pr=github())), "")
        self.assertEqual(attention_reason(row(status="done", pr=github(review="APPROVED"))), "")
        self.assertEqual(attention_reason(row(last_test="success")), "")

    def test_each_condition_has_a_reason(self) -> None:
        self.assertEqual(attention_reason(row(status="blocked")), "blocked")
        self.assertEqual(attention_reason(row(last_test="failed")), "tests failed")
        self.assertEqual(attention_reason(row(pr=github(checks_failed=1, checks_passed=1))), "checks failed")
        self.assertEqual(attention_reason(row(pr=github(review="CHANGES_REQUESTED"))), "changes requested")
        self.assertEqual(attention_reason(row(age=STALE_WORK_SECONDS + 300)), "stale 20m")
        # A merged or closed pull request's checks are history.
        self.assertEqual(attention_reason(row(pr=github(state="MERGED", checks_failed=1))), "")
        # Only working sessions go stale; an idle one that old is just idle.
        self.assertEqual(attention_reason(row(status="idle", age=STALE_WORK_SECONDS + 300)), "")

    def test_conflicts_name_the_partner_surface_and_reasons_combine(self) -> None:
        first = row("codex:a", surface="Herdr · pane p3", status="blocked")
        second = row("claude-code:b", surface="Codex Desktop", pr=github(checks_failed=1))
        rows = [first, second]
        conflicts = detect_conflicts(rows)
        self.assertEqual(attention_reason(first, conflicts, rows), "folder conflict · blocked")
        self.assertEqual(attention_reason(second, conflicts, rows), "folder conflict · checks failed")
        reasons = attention_reasons(rows, conflicts)
        self.assertEqual(list(reasons), ["codex:a", "claude-code:b"])

    def test_attention_rows_keep_the_roster_order(self) -> None:
        rows = sort_rows(
            [
                row("codex:a", status="idle", age=30.0, working_root="/work/a", branch="a"),
                row("codex:b", status="blocked", age=10.0, working_root="/work/b", branch="b"),
                row("codex:c", age=STALE_WORK_SECONDS + 60, working_root="/work/c", branch="c"),
                row("codex:d", status="done", age=20.0, working_root="/work/d", branch="d"),
                row("codex:e", age=2.0, pr=github(review="CHANGES_REQUESTED"), working_root="/work/e", branch="e"),
            ]
        )
        kept = attention_rows(rows, detect_conflicts(rows))
        self.assertEqual([item.key for item in kept], ["codex:e", "codex:c", "codex:b"])
        # Conflicts pair a shown row with a hidden healthy one and still show.
        self.assertEqual(next_mode("all"), "attention")
        self.assertEqual(next_mode("attention"), "all")
        self.assertEqual(next_mode("anything"), "attention")

    def test_the_newest_test_status_reaches_the_row_from_the_history_tail(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            records = [
                {"agent": "codex", "session_id": "a", "kind": "test", "status": "success", "epoch_ms": 10},
                {"agent": "codex", "session_id": "a", "kind": "test", "status": "failed", "epoch_ms": 20, "detail": "/private"},
                {"agent": "codex", "session_id": "b", "kind": "file", "status": "success", "epoch_ms": 30},
                {"agent": "codex", "session_id": "c", "kind": "test", "status": "bogus", "epoch_ms": 40},
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            scanned = board_history_scan(path, None, {}, {}, {})
            self.assertEqual(scanned.tests, {"codex:a": (20, "failed")})
            self.assertEqual(scanned.activity["codex:b"], 30)
            again = board_history_scan(path, scanned.stamp, scanned.activity, scanned.issues, scanned.tests)
            self.assertEqual(again.tests, scanned.tests)
        source = BoardSource(
            root="/work/side-dog",
            repository="side-dog",
            identities={"codex:a": {"agent": "codex", "session_id": "a", "status": "idle", "working_root": "/work/side-dog"}},
            activity={"codex:a": NOW_MS - 1000},
            test_outcomes={"codex:a": (NOW_MS - 1000, "failed")},
        )
        [built] = rows_from_sources([source], NOW_MS)
        self.assertEqual(built.last_test, "failed")
        self.assertEqual(attention_reason(built), "tests failed")


class AttentionRenderTest(TestCase):
    def test_attention_column_shows_every_reason_and_narrow_frames_keep_it(self) -> None:
        rows = sort_rows(
            [
                row("codex:a", status="blocked", surface="Herdr · pane p3"),
                row("claude-code:b", surface="Codex Desktop", pr=github(checks_failed=1), working_root="/work/other", branch="fix/y"),
            ]
        )
        reasons = attention_reasons(rows, detect_conflicts(rows))
        screen = render_board(rows, 120, 12, False, reasons=reasons, selected=rows[0].key).splitlines()
        self.assertIn("ATTENTION", screen[1])
        for text in ("blocked", "checks failed"):
            self.assertTrue(any(text in line for line in screen[2:]), (text, screen))
        for width in (60, 40, 28, 20):
            with self.subTest(width=width):
                narrow = render_board(rows, width, 12, False, reasons=reasons).splitlines()
                self.assertTrue(all(len(line) <= width for line in narrow))
                body = "\n".join(narrow[2:])
                self.assertTrue("blocked" in body or "checks" in body or "STATUS" in narrow[1], narrow)
        colored = render_board(rows, 120, 12, True, reasons=reasons)
        self.assertIn("checks failed", colored)

    def test_an_empty_attention_roster_explains_itself(self) -> None:
        lines = attention_empty_lines(4)
        self.assertEqual(lines[0], "No sessions currently need attention.")
        self.assertIn("4 sessions", lines[1])
        self.assertIn("A shows the full roster", lines[1])
        screen = render_board([], 60, 8, False, empty_lines=lines, hints=board_hints(False))
        self.assertIn("No sessions currently need attention.", screen)
        self.assertNotIn("No coding-agent sessions found.", screen)
        self.assertIn("A attention", board_hints(False))
        help_screen = render_board_help(screen, 100, 30, False, group="repo", show_detail=True, mode="attention")
        self.assertIn("attention · detail shown", help_screen)
        self.assertIn("A            show only sessions needing attention", help_screen)

    def test_the_a_key_toggles_the_mode_and_selection_stays_where_it_can(self) -> None:
        from side_dog.cli import board

        rows = sort_rows([
            row("codex:a", status="blocked"),
            row("codex:b", status="idle", surface="kitty", working_root="/work/wt-b", branch="fix/b"),
        ])
        keys = iter([b"j", b"A", b"A", b"q"])
        frames: list[str] = []

        def fake_render(*args: object, **kwargs: object) -> str:
            frames.append(f"{kwargs.get('masthead')}\n{[item.key for item in args[0]]}\n{kwargs.get('selected')}")
            return "frame"

        terminal = io.StringIO()
        terminal.isatty = lambda: True  # type: ignore[method-assign]
        with (
            patch("side_dog.cli.sys.stdout", terminal),
            patch("side_dog.cli.sys.stdin") as stdin,
            patch("side_dog.cli.termios.tcgetattr", return_value=[]),
            patch("side_dog.cli.termios.tcsetattr"),
            patch("side_dog.cli.tty.setcbreak"),
            patch("side_dog.cli.select.select", return_value=([0], [], [])),
            patch("side_dog.cli.read_terminal_key", side_effect=lambda descriptor: next(keys)),
            patch("side_dog.cli.rows_from_sources", return_value=list(rows)),
            patch("side_dog.cli.discovered_watch_roots", return_value=()),
            patch("side_dog.cli.load_config", return_value={}),
            patch("side_dog.cli.board_frame_size", return_value=(100, 20)),
            patch("side_dog.cli.render_board", side_effect=fake_render),
            patch("side_dog.cli.board_detail_lines", return_value=[]),
            patch("side_dog.cli.board_session_brief", return_value=session_brief(rows[0], [], NOW_MS)),
        ):
            stdin.fileno.return_value = 0
            code = board(width=100, poll=0.01, github_poll=0, group="none", once=False, no_color=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(frames), 4)
        self.assertIn("Board · 2 sessions", frames[0])
        self.assertIn("['codex:a', 'codex:b']", frames[0])
        # j moved to the idle row; A hides it, so the selection falls back
        # to the first row left, and the masthead says how much is hidden.
        self.assertIn("Board · 2 sessions", frames[1])
        self.assertTrue(frames[1].endswith("codex:b"), frames[1])
        self.assertIn("Attention · 1 of 2 sessions", frames[2])
        self.assertIn("['codex:a']", frames[2])
        self.assertTrue(frames[2].endswith("codex:a"), frames[2])
        self.assertIn("Board · 2 sessions", frames[3])
        self.assertIn("['codex:a', 'codex:b']", frames[3])

    def test_reasons_never_carry_paths_or_event_text(self) -> None:
        rows = [
            row("codex:a", status="blocked", surface="Herdr · pane p3", working_root="/Users/q/secret", last_test="failed",
                pr=github(checks_failed=1, review="CHANGES_REQUESTED", title="/Users/q/secret/prompt.txt")),
            row("claude-code:b", surface="kitty", working_root="/Users/q/secret", age=STALE_WORK_SECONDS + 1),
        ]
        conflicts = detect_conflicts(rows)
        for reason in attention_reasons(rows, conflicts).values():
            self.assertNotIn("/", reason)
            self.assertNotIn("secret", reason)
            self.assertNotIn("prompt", reason)


class SessionBriefTest(TestCase):
    """Issue 226: a decision-ready brief in place of a bare event list."""

    def test_a_session_without_events_is_still_informative(self) -> None:
        brief = session_brief(row(status="idle", age=None), [], NOW_MS)
        self.assertEqual(brief.status, "○ idle")
        self.assertEqual(brief.work, "no confirmed issue or PR")
        self.assertEqual(brief.milestone, "no observed activity")
        self.assertEqual(brief.evidence, "no events recorded for this session")
        self.assertEqual(brief.cue, CUE_NONE)
        self.assertEqual(brief.cue_evidence, "")
        lines = brief_lines(brief)
        self.assertEqual(lines[0], "status  ○ idle")
        self.assertEqual(lines[-1], f"cue     {CUE_NONE}")
        screen = render_board([row(status="idle", age=None)], 80, 30, False, selected="codex:a", detail=[], detail_heading="Codex", brief=brief)
        self.assertIn("no observed activity", screen)
        self.assertIn("no events recorded for this session", screen)
        self.assertNotIn("no recent events for this session", screen)

    def test_a_successful_session_counts_its_evidence(self) -> None:
        events = [
            event("file", "success", 300),
            event("file", "success", 240),
            event("test", "success", 180),
            event("commit", "success", 120),
            event("push", "success", 60),
            event("pr", "success", 30),
        ]
        confirmed = LinkedIssue("github.com/o/side-dog", 139, True)
        brief = session_brief(row(pr=github(), issues=(confirmed,)), events, NOW_MS)
        self.assertEqual(brief.work, "issue #139 · PR #151 ✓ci ○rev")
        self.assertEqual(brief.milestone, "pull request succeeded · 30s ago")
        self.assertEqual(brief.evidence, "2 edits · 1 test · 1 commit · 1 PR update")
        self.assertEqual(brief.cue, CUE_REVIEW_PR)
        self.assertEqual(brief.cue_evidence, "PR #151 checks passed, no approval yet")
        # An inferred issue is not evidence.
        inferred = session_brief(row(issues=(LinkedIssue("github.com/o/side-dog", 7, False),)), events[:1], NOW_MS)
        self.assertEqual(inferred.work, "no confirmed issue or PR")
        self.assertEqual(inferred.milestone, "no milestone yet")

    def test_each_cue_maps_to_visible_evidence(self) -> None:
        failed = session_brief(row(), [event("test", "success", 90), event("test", "failed", 20)], NOW_MS)
        self.assertEqual(failed.milestone, "tests failed · 20s ago")
        self.assertEqual(failed.evidence, "0 edits · 2 tests (1 failed) · 0 commits · 0 PR updates")
        self.assertEqual((failed.cue, failed.cue_evidence), (CUE_INVESTIGATE_TESTS, "the newest recorded test run failed"))
        # A later passing run clears the cue even when the tail said failed.
        passed = session_brief(row(last_test="failed"), [event("test", "failed", 90), event("test", "success", 20)], NOW_MS)
        self.assertEqual(passed.cue, CUE_NONE)
        blocked = session_brief(row(status="blocked"), [], NOW_MS)
        self.assertEqual((blocked.cue, blocked.cue_evidence), (CUE_UNBLOCK, "the session is waiting on a person"))
        checks = session_brief(row(pr=github(checks_failed=1, checks_passed=1)), [], NOW_MS)
        self.assertEqual((checks.cue, checks.cue_evidence), (CUE_FIX_CHECKS, "PR #151 has failing checks"))
        review = session_brief(row(pr=github(review="CHANGES_REQUESTED")), [], NOW_MS)
        self.assertEqual((review.cue, review.cue_evidence), (CUE_ADDRESS_REVIEW, "PR #151 review requested changes"))
        merge = session_brief(row(status="done", pr=github(review="APPROVED")), [], NOW_MS)
        self.assertEqual((merge.cue, merge.cue_evidence), (CUE_MERGE_PR, "PR #151 checks passed and approved"))
        stale = session_brief(row(age=STALE_WORK_SECONDS + 100), [], NOW_MS)
        self.assertEqual((stale.cue, stale.cue_evidence), (CUE_CHECK_STALLED, "working, but quiet for more than 15m"))
        # Without a clock the brief carries no age, so the browser feed
        # sends the same brief until something actually changes.
        undated = session_brief(row(age=STALE_WORK_SECONDS + 100), [event("test", "failed", 20)], None)
        self.assertEqual(undated.status, "● working")
        self.assertEqual(undated.milestone, "tests failed")
        self.assertEqual(undated.cue, CUE_INVESTIGATE_TESTS)
        unknown = session_brief(row(status="unknown", age=None), [event("search", "unknown", 5)], NOW_MS)
        self.assertEqual(unknown.status, "? unknown")
        self.assertEqual(unknown.cue, CUE_NONE)
        rows = [row("codex:a", surface="Herdr · pane p3"), row("claude-code:b", surface="kitty")]
        conflict = session_brief(rows[0], [], NOW_MS, conflicts=detect_conflicts(rows), rows=rows)
        self.assertEqual((conflict.cue, conflict.cue_evidence), (CUE_RESOLVE_CONFLICT, "same folder as kitty"))
        # A conflict outranks a failing check: two agents undoing each other
        # is the thing to stop first.
        both = session_brief(row("codex:a", surface="Herdr · pane p3", pr=github(checks_failed=1)), [], NOW_MS, conflicts=detect_conflicts(rows), rows=rows)
        self.assertEqual(both.cue, CUE_RESOLVE_CONFLICT)

    def test_the_brief_quotes_no_event_text_and_no_path(self) -> None:
        events = [event("file", "success", 10), event("test", "failed", 5), event("commit", "success", 2)]
        brief = session_brief(row(working_root="/Users/q/secret", pr=github(title="/Users/q/secret/prompt.txt")), events, NOW_MS)
        for value in brief:
            self.assertNotIn("/", value)
            self.assertNotIn("secret", value)
            self.assertNotIn("rm -rf", value)
            self.assertNotIn("id_rsa", value)

    def test_the_pane_keeps_the_cue_first_when_short(self) -> None:
        brief = SessionBrief("● working 3m", "PR #151", "tests failed · 3m ago", "1 edit", CUE_INVESTIGATE_TESTS, "the newest recorded test run failed")
        self.assertEqual(brief_lines(brief, 1), [f"cue     {CUE_INVESTIGATE_TESTS} — the newest recorded test run failed"])
        self.assertEqual([line.split()[0] for line in brief_lines(brief, 2)], ["status", "cue"])
        self.assertEqual([line.split()[0] for line in brief_lines(brief)], ["status", "work", "latest", "events", "cue"])
        rows = [row()]
        detail = [f"│ event {i}" for i in range(10)]
        tall = render_board(rows, 100, 30, False, selected="codex:a", detail=detail, detail_heading="h", brief=brief).splitlines()
        self.assertIn("cue     investigate failed tests — the newest recorded test run failed", tall)
        self.assertIn("│ event 9", tall)
        self.assertIn("status  ● working 3m", tall)
        short = render_board(rows, 100, 9, False, selected="codex:a", detail=detail, detail_heading="h", brief=brief).splitlines()
        self.assertTrue(any(line.startswith("cue ") for line in short), short)
        self.assertIn("│ event 9", short)
        self.assertLessEqual(len(short), 9)

    def test_the_browser_gets_the_same_fields_and_nothing_else(self) -> None:
        brief = session_brief(row(pr=github(checks_failed=1)), [event("test", "failed", 5)], NOW_MS)
        message = board_rows_payload([row(pr=github(checks_failed=1))], [], {"codex:a": brief})
        self.assertEqual(message.rows[0].brief["status"], brief.status)
        wire = message.to_wire()["rows"][0]["brief"]
        self.assertEqual(set(wire), set(BRIEF_FIELDS))
        self.assertEqual(wire["cue"], CUE_INVESTIGATE_TESTS)
        self.assertEqual(wire["status"], brief.status)
        roundtrip = BoardRowWire.from_wire(message.to_wire()["rows"][0])
        self.assertEqual(roundtrip.brief, wire)
        without = board_rows_payload([row()], []).to_wire()["rows"][0]
        self.assertIsNone(without["brief"])
        bad = dict(message.to_wire()["rows"][0])
        bad["brief"] = {"cue": "x", "prompt": "never"}
        with self.assertRaises(ValueError):
            BoardRowWire.from_wire(bad)
        bad["brief"] = {name: "x" for name in BRIEF_FIELDS}
        bad["brief"]["cue"] = "a\x1b[31m"
        with self.assertRaises(ValueError):
            BoardRowWire.from_wire(bad)

    def test_the_feed_brief_reads_the_sessions_own_validated_events(self) -> None:
        from side_dog.cli import append_event

        with TemporaryDirectory() as tmp, patch.dict("os.environ", {STATE_ENV: tmp}):
            root = (Path(tmp) / "side-dog").resolve()
            root.mkdir()
            state = BoardRootState(root=root)
            state.identities = {
                "codex:a": {"agent": "codex", "session_id": "a", "status": "working", "working_root": str(root), "root": str(root)},
            }
            (root / "x.py").write_text("")
            for kind, status, title, session, detail in (
                ("file", "success", "File changed", "a", "x.py"),
                ("test", "failed", "Tests failed", "a", ""),
                ("test", "success", "Tests passed", "zz", ""),
            ):
                append_event(root, {
                    "agent": "codex", "session_id": session, "kind": kind,
                    "status": status, "title": title, "detail": detail,
                })
            now_ms = int(time.time() * 1000)
            [built] = rows_from_sources([board_source(state)], now_ms)
            brief = board_session_brief(built, {root: state}, now_ms, rows=[built])
        self.assertEqual(brief.evidence, "1 edit · 1 test (1 failed) · 0 commits · 0 PR updates")
        self.assertEqual(brief.cue, CUE_INVESTIGATE_TESTS)
        self.assertNotIn("x.py", " ".join(brief))
