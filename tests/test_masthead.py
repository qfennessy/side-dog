"""The masthead and the status dots that start agent and pull-request rows."""

from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    ANSI,
    ANSI_ESCAPE,
    SEMANTIC_ANSI,
    SOURCE_KEY,
    agent_status_display,
    agent_status_dot,
    render_agent_context_text,
    render_agent_roster,
    github_status_dot,
    render_github_banner,
    status_bar,
    style_status_bar,
    terminal_cell_width,
)


class MastheadTest(TestCase):
    def test_masthead_fills_the_gap_with_stripes_and_keeps_width(self) -> None:
        line = status_bar("1.1.0", "side-dog", 2, 80, "10:33:58")

        self.assertTrue(line.startswith("SIDE DOG v1.1.0 · side-dog · 2 working ╱"))
        self.assertTrue(line.endswith("╱ 10:33:58"))
        self.assertEqual(terminal_cell_width(line), 80)

    def test_narrow_masthead_has_no_stripes(self) -> None:
        line = status_bar("1.1.0", "side-dog", 2, 17, "10:33:58")

        self.assertEqual(line, "SIDE DOG 10:33:58")

    def test_masthead_never_leaves_a_lone_stripe(self) -> None:
        # Three spare cells: not enough for a stripe with breathing room.
        line = status_bar("1.1.0", "side-dog", None, 26, "10:33:58")

        self.assertEqual(line, "SIDE DOG v1.1.0   10:33:58")

    def test_color_paints_the_name_the_stripes_and_nothing_else_differently(
        self,
    ) -> None:
        line = status_bar("1.1.0", "side-dog", 2, 80, "10:33:58")

        self.assertEqual(style_status_bar(line, False), line)
        colored = style_status_bar(line, True)
        self.assertTrue(
            colored.startswith(
                f"{SEMANTIC_ANSI['identity']}{ANSI['bold']}SIDE DOG{ANSI['reset']}"
                f"{ANSI['bold']}{ANSI['blue']} v1.1.0"
            )
        )
        self.assertIn("\x1b[38;5;171m╱", colored)
        self.assertIn("\x1b[38;5;81m╱", colored)
        self.assertTrue(colored.endswith(f" 10:33:58{ANSI['reset']}"))
        self.assertEqual(ANSI_ESCAPE.sub("", colored), line)


class StatusDotTest(TestCase):
    def test_dot_is_filled_for_activity_and_hollow_when_nothing_happens(self) -> None:
        for status in ("working", "running", "completed", "failed", "blocked"):
            self.assertEqual(agent_status_dot(status), "●", status)
        for status in ("idle", "", None, "unexpected"):
            self.assertEqual(agent_status_dot(status), "○", status)

    def test_status_words_carry_the_meaning_without_glyphs(self) -> None:
        self.assertEqual(agent_status_display("working"), ("running", "working"))
        self.assertEqual(agent_status_display("blocked"), ("failure", "blocked"))
        self.assertEqual(agent_status_display("done"), ("success", "completed"))
        self.assertEqual(agent_status_display("idle"), ("idle", "idle"))
        self.assertEqual(agent_status_display(None), ("unknown", "unknown"))

    def test_context_text_leaves_an_unknown_runtime_out(self) -> None:
        line = render_agent_context_text(
            {"agent": "codex", "label": "side-dog", "status": "working"}, 80
        )

        self.assertEqual(line, " ● Codex · side-dog · working")

    def test_context_text_keeps_the_model_when_only_effort_is_unknown(self) -> None:
        line = render_agent_context_text(
            {"agent": "claude-code", "model": "claude-fable-5-1", "status": "idle"},
            80,
        )

        self.assertEqual(line, " ○ Claude · fable-5-1 · idle")

    def test_github_banner_starts_with_a_dot_that_follows_the_pr_state(self) -> None:
        open_banner = render_github_banner(
            {
                "number": 3,
                "title": "Feature",
                "state": "OPEN",
                "ci": "CI —",
                "merge_state": "CLEAN",
            },
            100,
            False,
        )
        closed_banner = render_github_banner(
            {"number": 3, "title": "Old", "state": "CLOSED"}, 100, False
        )

        self.assertTrue(open_banner.startswith(" ● PR #3 "), open_banner)
        self.assertTrue(closed_banner.startswith(" ○ PR #3 "), closed_banner)

    def test_roster_rows_start_with_a_dot_in_the_state_color(self) -> None:
        root = "/tmp/side-dog"
        identities = {
            status: {
                "agent": "codex",
                "pane_id": status,
                "label": f"task {status}",
                "working_root": root,
                "status": status,
                SOURCE_KEY: root,
            }
            for status in ("working", "idle")
        }

        plain = "\n".join(
            render_agent_roster(
                identities,
                [],
                80,
                False,
                show_idle_agents=True,
                roots=({"key": root, "name": "side-dog"},),
            )
        )
        colored = "\n".join(
            render_agent_roster(
                identities,
                [],
                80,
                True,
                show_idle_agents=True,
                roots=({"key": root, "name": "side-dog", "color_index": 0},),
            )
        )

        self.assertIn("│ ● Codex", plain)
        self.assertIn("│ ○ Codex", plain)
        self.assertIn(
            f"{SEMANTIC_ANSI['running']}●{ANSI['reset']} "
            f"{SEMANTIC_ANSI['identity']}{ANSI['bold']}Codex",
            colored,
        )
        self.assertIn(f"{SEMANTIC_ANSI['idle']}○{ANSI['reset']} ", colored)

    def test_closed_pr_dot_is_hollow_even_when_its_checks_failed(self) -> None:
        status = {
            "number": 3,
            "title": "Abandoned",
            "state": "CLOSED",
            "ci": "CI 1 failed",
            "checks_failed": 1,
        }

        self.assertEqual(github_status_dot(status), "○")
        plain = render_github_banner(status, 100, False)
        colored = render_github_banner(status, 100, True)
        self.assertTrue(plain.startswith(" ○ PR #3 "), plain)
        # Hollow for the lifecycle, red for the detail: both survive together.
        self.assertIn(f"{SEMANTIC_ANSI['failure']} ○ PR #3", colored)
        self.assertEqual(github_status_dot({**status, "state": "MERGED"}), "●")

    def test_unknown_github_state_is_hollow(self) -> None:
        # A failed refresh leaves {"state": "UNKNOWN"}; that is quiet, not active.
        self.assertEqual(github_status_dot({"state": "UNKNOWN"}), "○")
        self.assertEqual(github_status_dot({}), "○")
        banner = render_github_banner({"state": "UNKNOWN"}, 60, False)
        self.assertTrue(banner.startswith(" ○ GitHub "), banner)

    def test_worktree_rows_color_the_dot_that_sits_before_the_label(self) -> None:
        now_ms = 2_000_000_000_000
        roots = [
            {
                "key": "/tmp/worktrees/2276-main",
                "name": "2276-main",
                "git": {
                    "repository": "cocos-story",
                    "common_dir": "/tmp/cocos-story/.git",
                    "branch": "main",
                },
            },
            {
                "key": "/tmp/worktrees/9abc-review",
                "name": "9abc-review",
                "git": {
                    "repository": "cocos-story",
                    "common_dir": "/tmp/cocos-story/.git",
                    "branch": "codex/issue-124",
                },
            },
        ]
        identities = {
            "main": {
                "agent": "codex",
                "session_id": "main-session",
                "working_root": roots[0]["key"],
                "label": "Main review",
                "status": "working",
                "epoch_ms": now_ms - 60_000,
                "branch": "main",
                SOURCE_KEY: roots[0]["key"],
            },
            "review": {
                "agent": "claude-code",
                "session_id": "review-session",
                "working_root": roots[1]["key"],
                "label": "Header polish",
                "status": "idle",
                "epoch_ms": now_ms,
                "branch": "codex/issue-124",
                SOURCE_KEY: roots[1]["key"],
            },
        }

        with patch("side_dog.cli.time.time", return_value=now_ms / 1000):
            lines = render_agent_roster(
                identities, [], 120, True, show_idle_agents=True, roots=roots
            )

        working = next(line for line in lines if "Main review" in line)
        idle = next(line for line in lines if "Header polish" in line)
        self.assertTrue(
            working.startswith(f"│ {SEMANTIC_ANSI['running']}●{ANSI['reset']} main"),
            working,
        )
        self.assertIn(f"{SEMANTIC_ANSI['identity']}{ANSI['bold']}Codex", working)
        self.assertTrue(
            idle.startswith(f"│ {SEMANTIC_ANSI['idle']}○{ANSI['reset']} codex/issue-124"),
            idle,
        )
