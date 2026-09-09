from unittest import TestCase
from unittest.mock import patch

from side_dog.contributions import attributed_events
from side_dog.model import actor_label, build_activity_units, identity_for_event

NOW = 2_000_000_000_000


def event(model="model-one", session="session-one", pr=1, repo="owner/repo", **extra):
    return {
        "project": "/project",
        "epoch_ms": NOW - 1000,
        "agent": "codex",
        "session_id": session,
        "model": model,
        "kind": "commit",
        "title": "Commit created",
        "status": "success",
        "operation_id": "op",
        "github": {"number": pr, "url": f"https://github.com/{repo}/pull/{pr}"},
        **extra,
    }


class AttributionTests(TestCase):
    def test_explicit_board_folders_do_not_discover_unrelated_roots(self):
        from side_dog.cli import main
        from pathlib import Path
        from contextlib import redirect_stdout
        from io import StringIO

        root = Path("/selected")
        with (
            patch("side_dog.cli.canonical_root", return_value=root),
            patch("side_dog.cli.discovered_watch_roots") as discover,
            patch("side_dog.cli.load_agent_identities", return_value={}) as identities,
            patch("side_dog.cli.load_git_state", return_value=None),
            patch("side_dog.cli.load_github_pr", return_value=(None, None)),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["board", "--once", "--no-color", "/selected"]), 0)
        discover.assert_not_called()
        identities.assert_called_once_with(root)

    def test_empty_explicit_board_space_does_not_fall_back_to_discovery(self):
        from side_dog.cli import main
        from contextlib import redirect_stdout
        from io import StringIO

        with (
            patch("side_dog.cli.space_folders", return_value=[]),
            patch("side_dog.cli.discovered_watch_roots") as discover,
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(main(["board", "--once", "--no-color", "@missing"]), 0)
        discover.assert_not_called()

    def test_board_has_no_recorded_contributions_mode(self):
        from side_dog.cli import build_parser, render_board_help

        parser = build_parser()
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parser.parse_args(["board", "--activity"])
        help_text = render_board_help(
            "", 100, 30, False, group="repo", show_detail=True,
            notify_enabled=True, notify_locked=False,
        )
        self.assertNotIn("contribution", help_text.casefold())

    def test_model_switch_uses_event_time_model(self):
        record = event()
        identities = {"codex:session-one": {"agent": "codex", "model": "new-model"}}
        self.assertEqual(identity_for_event(record, identities)["model"], "new-model")
        self.assertIn("model-one", actor_label(record, identities))
        self.assertNotIn("new-model", actor_label(record, identities))

    def test_reference_scope_is_turn_local_and_ambiguity_remains_unknown(self):
        commit = event(turn_id="turn")
        edit = {**commit, "kind": "file", "github": None, "operation_id": "edit"}
        self.assertEqual(attributed_events([commit, edit])[1]["_contribution_work"], "PR #1")
        second = event(pr=2, turn_id="turn")
        self.assertEqual(
            attributed_events([commit, edit, second])[1]["_contribution_work"],
            "unlinked work",
        )

    def test_polling_context_cannot_assign_unlinked_agent_work(self):
        poll = event(turn_id="old-turn")
        poll.update(kind="github", title="PR #1 confirmed")
        edit = {**poll, "kind": "file", "github": None, "operation_id": "edit"}
        self.assertEqual(attributed_events([poll, edit])[1]["_contribution_work"], "unlinked work")

    def test_exact_pr_head_links_commit_without_supplying_its_model(self):
        oid = "a" * 40
        poll = event(model="observer-model")
        poll.update(kind="github", github={
            "number": 1,
            "head_oid": oid,
            "url": "https://github.com/owner/repo/pull/1",
        })
        commit = event(turn_id="turn", github=None, git_oid=oid)
        records = attributed_events([poll, commit])
        self.assertEqual(records[1]["_contribution_work"], "PR #1")
        self.assertEqual(records[1]["model"], "model-one")

    def test_pipeline_preserves_distinct_models(self):
        first = event(turn_id="turn")
        second = {**first, "model": "model-two"}
        self.assertEqual(len(build_activity_units([first, second], expanded_history=False)), 2)
