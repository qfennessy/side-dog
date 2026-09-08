from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    ContributionHistory,
    aggregate_watch_records,
    build_parser,
    normalized_tool_events,
    render_event_line,
    render_milestone_card,
    append_event,
    events_path,
)
from side_dog.contributions import (
    attributed_events,
    contributions,
    render_contributions,
    WINDOW_MS,
)
from side_dog.model import actor_label, identity_for_event, build_activity_units
from side_dog.manual import manual_pages

NOW = 2_000_000_000_000


def event(model="model-one", session="session-one", pr=1, repo="owner/repo", **extra):
    return dict(
        project="/project",
        epoch_ms=NOW - 1000,
        agent="codex",
        session_id=session,
        model=model,
        kind="commit",
        title="Commit created",
        status="success",
        operation_id="op",
        github={"number": pr, "url": f"https://github.com/{repo}/pull/{pr}"},
        **extra,
    )


class ContributionTests(TestCase):
    def test_two_prs_models_and_cross_repository_numbers_stay_distinct(self):
        rows = contributions(
            [event(), event("model-two", "session-two", 2), event(repo="other/repo")],
            NOW,
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual({row.model for row in rows}, {"model-one", "model-two"})
        self.assertEqual(len({(row.repository, row.work) for row in rows}), 3)

    def test_several_models_one_pr_and_one_session_several_prs(self):
        a, b, c = event(), event("model-two"), event(pr=2)
        b["operation_id"], c["operation_id"] = "op2", "op3"
        rows = contributions([a, b, c], NOW)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum("1 commits" == row.summary for row in rows), 3)

    def test_model_switch_never_uses_current_roster(self):
        a = event()
        identities = {"codex:session-one": {"agent": "codex", "model": "new-model"}}
        self.assertEqual(identity_for_event(a, identities)["model"], "new-model")
        self.assertIn("model-one", actor_label(a, identities))
        self.assertNotIn("new-model", actor_label(a, identities))
        a["model"] = ""
        self.assertIn("model unknown", actor_label(a, identities))

    def test_polling_is_never_effort_even_with_triggering_identity(self):
        observed = event()
        observed.update(kind="github", title="PR #1 confirmed")
        observed["github"]["ci"] = "CI 1/1"
        rows = contributions([observed, observed, event()], NOW)
        observer = next(row for row in rows if row.agent == "observation")
        self.assertNotIn("commits", observer.summary)
        self.assertEqual(observer.model, "unknown")
        self.assertEqual(len(observer.events), 1)

    def test_completed_contributors_survive_without_roster(self):
        a = event()
        ended = event()
        ended.update(kind="session", title="Agent turn completed", operation_id="end")
        row = contributions([a, ended], NOW)[0]
        self.assertEqual(len(row.events), 2)
        self.assertEqual(row.summary, "1 commits")
        self.assertEqual(contributions([a], NOW + WINDOW_MS), [])

    def test_operation_start_finish_and_repeated_tests(self):
        started = event()
        started.update(kind="test", status="running", title="Running tests")
        ended = {**started, "status": "failed", "epoch_ms": NOW - 500}
        another = {**ended, "operation_id": "retry", "status": "success"}
        row = contributions([started, ended, another], NOW)[0]
        self.assertEqual(row.summary, "1 tests failed · 1 tests success")

    def test_reference_scope_is_turn_local_and_ambiguity_remains_unknown(self):
        a = event(turn_id="turn")
        edit = {**a, "kind": "file", "github": None, "operation_id": "edit"}
        linked = attributed_events([a, edit])
        self.assertEqual(linked[1]["_contribution_work"], "PR #1")
        second = event(pr=2, turn_id="turn")
        self.assertEqual(
            attributed_events([a, edit, second])[1]["_contribution_work"],
            "unlinked work",
        )
        edit["session_id"] = "someone-else"
        self.assertEqual(
            attributed_events([a, edit])[1]["_contribution_work"], "unlinked work"
        )

    def test_polling_context_cannot_assign_unlinked_agent_work(self):
        poll = event(turn_id="old-turn")
        poll.update(kind="github", title="PR #1 confirmed")
        edit = {**poll, "kind": "file", "github": None, "operation_id": "edit"}
        result = attributed_events([poll, edit])
        self.assertEqual(result[1]["_contribution_work"], "unlinked work")

    def test_exact_pr_head_links_commit_but_never_supplies_its_model(self):
        from side_dog.model import normalize_github_pr, github_fingerprint
        from side_dog.integrations import SafeEvent

        oid = "a" * 40
        raw = {
            "number": 1,
            "headRefOid": oid,
            "url": "https://github.com/owner/repo/pull/1",
        }
        metadata = normalize_github_pr(raw)
        self.assertEqual(metadata["head_oid"], oid)
        self.assertNotEqual(
            github_fingerprint(metadata),
            github_fingerprint({**metadata, "head_oid": "b" * 40}),
        )
        poll = event(model="observer-model")
        poll.update(kind="github", github=metadata)
        commit = event(turn_id="turn")
        commit.update(github=None, git_oid=oid)
        edit = {**commit, "kind": "file", "git_oid": "", "operation_id": "edit"}
        records = attributed_events([poll, commit, edit])
        self.assertEqual(records[1]["_contribution_work"], "PR #1")
        self.assertEqual(records[2]["_contribution_work"], "PR #1")
        self.assertEqual(records[1]["model"], "model-one")
        self.assertIsNone(commit["github"])
        # A shared head across two PRs or another folder proves no unique link.
        second = {
            **poll,
            "github": {
                **metadata,
                "number": 2,
                "url": "https://github.com/owner/repo/pull/2",
            },
        }
        self.assertEqual(
            attributed_events([poll, second, commit])[-1]["_contribution_work"],
            "unlinked work",
        )
        self.assertEqual(
            attributed_events([{**poll, "project": "/elsewhere"}, commit])[-1][
                "_contribution_work"
            ],
            "unlinked work",
        )
        with self.assertRaises(ValueError):
            SafeEvent(kind="github", github={"head_oid": "not-an-oid"})

    def test_pipeline_does_not_collapse_models_or_observers(self):
        a = event(turn_id="turn")
        b = {**a, "model": "model-two"}
        units = build_activity_units([a, b], expanded_history=False)
        self.assertEqual(len(units), 2)

    def test_watch_and_board_use_same_link_and_model(self):
        a = event(turn_id="turn")
        test = {
            **a,
            "kind": "test",
            "title": "Tests passed",
            "github": None,
            "operation_id": "test",
        }
        records = attributed_events([a, test])
        watch = render_event_line(records[1], 120, False, NOW, {})
        board = render_contributions(contributions(records, NOW), 120, 30, NOW)
        for value in ("PR #1", "model-one", "session-"):
            self.assertIn(value, watch)
            self.assertIn(value, board)

    def test_narrow_no_color_identity_and_work(self):
        a = event()
        for width in (28, 42, 100):
            screen = render_contributions(contributions([a], NOW), width, 30, NOW)
            self.assertIn("PR #1", screen)
            self.assertIn("model-one", screen)
            self.assertNotIn("\x1b", screen)
            self.assertTrue(all(len(line) <= width for line in screen.splitlines()))
            milestone = "\n".join(render_milestone_card(a, width, False, NOW, {}))
            self.assertIn("model-one", milestone)

    def test_merge_value_flags_never_become_pr_numbers(self):
        from side_dog.cli import gh_pr_merge_link_metadata

        for options in (
            "--body 123",
            "-b123",
            "-mb123",
            "--body=123",
            "--subject 123",
            "-t123",
            "--match-head-commit 123",
            "-F123",
            "--author-email 123",
        ):
            result = gh_pr_merge_link_metadata(
                f"gh pr merge {options} 42 -R owner/repo --merge"
            )
            self.assertEqual(result["number"], 42, options)
        self.assertIsNone(
            gh_pr_merge_link_metadata("gh pr merge --unrecognized 123 42")
        )
        self.assertIsNone(gh_pr_merge_link_metadata("echo gh pr merge 42"))

    def test_event_metadata_does_not_erase_current_roster(self):
        from side_dog.cli import display_identities

        identities = {
            "codex:session-one": {
                "agent": "codex",
                "session_id": "session-one",
                "model": "new-model",
                "effort": "high",
            }
        }
        old = event(model="old-model")
        missing = event(model="")
        result = display_identities([old, missing], identities)
        self.assertEqual(result["codex:session-one"]["model"], "new-model")
        self.assertEqual(result["codex:session-one"]["effort"], "high")
        self.assertIn("old-model", actor_label(old, identities))
        self.assertIn("model unknown", actor_label(missing, identities))

    def test_same_named_folders_keep_distinct_event_badges(self):
        from types import SimpleNamespace
        from side_dog.model import SOURCE_LABEL

        states = [
            SimpleNamespace(root=Path(root), git_status=None, records=[event()])
            for root in ("/client/project", "/server/project")
        ]
        records = aggregate_watch_records(states, ["PR #1", "PR #2"], None, None)
        self.assertEqual(
            {record[SOURCE_LABEL] for record in records},
            {"client/project", "server/project"},
        )

    def test_merge_operand_is_structured_and_compound_refused(self):
        payload = {
            "agent": "codex",
            "session_id": "s",
            "tool_name": "Bash",
            "tool_input": {"command": "gh pr merge 12 --repo owner/repo --merge"},
        }
        result = normalized_tool_events(payload, Path("/tmp"), status="success")
        self.assertEqual(
            result[0]["github"]["url"], "https://github.com/owner/repo/pull/12"
        )
        payload["tool_input"]["command"] += " || true"
        self.assertNotIn(
            "github", normalized_tool_events(payload, Path("/tmp"), status="success")[0]
        )

    def test_history_includes_departed_folders_and_explicit_scope(self):
        with (
            TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"SIDE_DOG_STATE_DIR": tmp + "/state"}),
        ):
            root = (Path(tmp) / "project").resolve()
            root.mkdir()
            a = event()
            a["project"] = str(root)
            append_event(root, a)
            cache = ContributionHistory()
            records, partial = cache.read([], NOW)
            self.assertFalse(partial)
            self.assertEqual(len(records), 1)
            self.assertEqual(len(cache.read([str(root)], NOW)[0]), 1)
            self.assertEqual(cache.read([str(root.parent / "other")], NOW)[0], [])
            self.assertEqual(cache.read([], NOW + WINDOW_MS)[0], [])

    def test_partial_scope_is_visible_even_with_a_long_path(self):
        screen = render_contributions(
            [], 28, 8, NOW, scope="/a/very/long/project/folder/name", partial=True
        )
        self.assertIn("PARTIAL", screen)
        self.assertIn("last 24h", screen)

    def test_board_to_watch_preserves_explicit_folders(self):
        from side_dog.cli import _alternate_terminal_view_args

        parser = build_parser()
        args = parser.parse_args(["board", "--activity", "/project"])
        self.assertEqual(
            _alternate_terminal_view_args(parser, "watch", args).projects, ["/project"]
        )

    def test_man_pages_match_parser_and_are_packaged(self):
        directory = Path(__file__).parents[1] / "side_dog/man"
        for name, text in manual_pages(build_parser()).items():
            self.assertEqual((directory / name).read_text(), text)
        self.assertIn(
            "--activity",
            (directory / "side-dog-board.1").read_text().replace("\\-", "-"),
        )
