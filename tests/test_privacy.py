from __future__ import annotations

import json
import math
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from side_dog.integrations import (
    ACTIVITY_SCHEMA,
    ActivityEvent,
    CODING_AGENT_PROVIDERS,
    MAX_SAFE_INTEGER,
    NormalizedEvent,
    PANEL_SAFE_EVENT_FIELDS,
    SAFE_EVENT_FIELDS,
    SAFE_EVENT_KINDS,
    SAFE_EVENT_STATUSES,
    SafeEvent,
)
from side_dog.privacy import (
    EventObservation,
    PrivacyRejection,
    PrivacyRejectionReason,
    SAFE_PANEL_WIRE_FIELDS,
    classify_command,
    normalize_project_path,
    rejection_diagnostic,
    safe_event,
    safe_events,
)


class SafeEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "repo"
        self.root.mkdir()
        self.now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def event(self, **values: object) -> SafeEvent:
        wire: dict[str, object] = {
            "agent": "codex",
            "kind": "session",
            "status": "success",
            "title": "Pi turn finished",
            "detail": "",
        }
        wire.update(values)
        return safe_event(self.root, wire, now=self.now)

    def test_safe_event_has_a_closed_wire_shape(self) -> None:
        canary = "PROMPT-CANARY-91f2"
        with self.assertRaises(PrivacyRejection) as raised:
            safe_event(
                self.root,
                {
                    "agent": "codex",
                    "kind": "session",
                    "status": "success",
                    "title": "Observed",
                    "prompt": canary,
                },
            )

        self.assertEqual(
            raised.exception.reason, PrivacyRejectionReason.UNEXPECTED_FIELD
        )
        self.assertNotIn(canary, str(raised.exception))

    def test_every_agent_rejects_private_vendor_payloads_at_the_same_boundary(
        self,
    ) -> None:
        canary = "CROSS-INTEGRATION-CANARY-72"
        private_fields = ("prompt", "response", "output", "diff", "patch", "command")
        for provider in CODING_AGENT_PROVIDERS:
            with self.subTest(provider=provider):
                with self.assertRaises(PrivacyRejection) as raised:
                    safe_event(
                        self.root,
                        {
                            "agent": provider,
                            "kind": "session",
                            "status": "success",
                            "title": "Observed",
                            **{field: canary for field in private_fields},
                        },
                    )
                self.assertEqual(
                    raised.exception.reason,
                    PrivacyRejectionReason.UNEXPECTED_FIELD,
                )
                self.assertNotIn(canary, str(raised.exception))

    def test_nested_github_metadata_is_also_allowlisted(self) -> None:
        with self.assertRaises(PrivacyRejection) as raised:
            self.event(
                kind="github",
                title="PR #42 confirmed",
                github={"number": 42, "state": "OPEN", "body": "secret"},
            )
        self.assertEqual(raised.exception.reason, PrivacyRejectionReason.INVALID_VALUE)
        self.assertNotIn("secret", str(raised.exception))

        event = self.event(
            kind="github",
            title="PR #42 confirmed",
            github={"number": 42, "state": "OPEN", "checks_pending": 2},
        )
        self.assertEqual(
            event.to_wire()["github"],
            {"number": 42, "state": "OPEN", "checks_pending": 2},
        )

    def test_non_finite_github_coverage_is_rejected(self) -> None:
        for coverage in (math.nan, math.inf, -math.inf):
            with self.subTest(coverage=coverage):
                with self.assertRaises(PrivacyRejection) as raised:
                    self.event(
                        kind="github",
                        title="PR #42 confirmed",
                        github={"number": 42, "coverage": coverage},
                    )
                self.assertEqual(
                    raised.exception.reason, PrivacyRejectionReason.INVALID_VALUE
                )

    def test_safe_event_sanitizes_top_level_and_nested_urls(self) -> None:
        canary = "URL-CANARY-72"
        event = self.event(
            kind="github",
            title="PR #42 confirmed",
            url=(
                f"https://person:{canary}@github.com/org/repo/pull/42"
                f"?token={canary}#{canary}"
            ),
            github={
                "number": 42,
                "url": (
                    f"https://person:{canary}@github.com/org/repo/pull/42"
                    f"?token={canary}#{canary}"
                ),
            },
        )
        direct = SafeEvent.from_wire(
            {
                "kind": "session",
                "title": "Pi turn finished",
                "url": f"https://person:{canary}@example.test/path?secret={canary}",
            }
        )

        self.assertEqual(event.url, "https://github.com/org/repo/pull/42")
        self.assertEqual(
            event.to_wire()["github"]["url"],
            "https://github.com/org/repo/pull/42",
        )
        self.assertEqual(direct.url, "https://example.test/path")
        self.assertNotIn(canary, json.dumps([event.to_wire(), direct.to_wire()]))

    def test_unsafe_urls_are_blank_at_safe_event_construction(self) -> None:
        event = SafeEvent.from_wire(
            {
                "kind": "github",
                "title": "PR #1 confirmed",
                "url": "file:///private/token",
                "github": {"number": 1, "url": "javascript:private-token"},
            }
        )
        self.assertEqual(event.url, "")
        self.assertEqual(event.github["url"], "")

    def test_existing_render_fields_and_source_identity_survive(self) -> None:
        event = self.event(
            session_id="session-1",
            kind="file",
            title="Wrote file",
            detail="src/example.py",
            operation_id="call-1",
            group_id="turn-1",
            source_event_id="codex:session-1:17",
            turn_id="turn-1",
            model="gpt-5",
            effort="high",
            started_epoch_ms=1767323044000,
            lines_added=3,
            lines_removed=1,
            url="https://example.test/change",
            git_oid="a1b2c3",
            herdr_pane_id="pane-1",
            herdr_tab_id="tab-1",
            herdr_workspace_id="workspace-1",
        )
        wire = event.to_wire()

        self.assertEqual(event.session_key.to_wire(), "codex:session-1")
        self.assertEqual(wire["title"], "Wrote file")
        self.assertEqual(wire["detail"], "src/example.py")
        self.assertEqual(wire["started_epoch_ms"], 1767323044000)
        self.assertEqual(wire["herdr_pane_id"], "pane-1")
        self.assertEqual(SafeEvent.from_wire(wire), event)

    def test_strings_are_bounded_and_jsonl_controls_are_removed(self) -> None:
        event = SafeEvent.from_wire(
            {
                "kind": "session",
                "title": "Pi turn finished",
                "model": "x" * 400,
                "detail": "one\ntwo\rthree",
            }
        )
        payload = json.dumps(event.to_wire())

        self.assertEqual(len(event.model), 256)
        self.assertEqual(event.detail, "onetwothree")
        self.assertNotIn("\\n", payload)

    def test_source_time_is_preserved_and_missing_time_uses_boundary_clock(
        self,
    ) -> None:
        event = self.event(
            timestamp="2025-03-04T05:06:07.890+00:00",
            epoch_ms=1741064767890,
        )
        self.assertEqual(event.timestamp, "2025-03-04T05:06:07.890+00:00")
        self.assertEqual(event.epoch_ms, 1741064767890)

        generated = self.event()
        self.assertEqual(generated.timestamp, "2026-01-02T03:04:05.000+00:00")
        self.assertEqual(generated.epoch_ms, 1767323045000)

    def test_contradictory_source_times_are_rejected(self) -> None:
        with self.assertRaises(PrivacyRejection) as raised:
            self.event(
                timestamp="2025-03-04T05:06:07.890+00:00",
                epoch_ms=1767323045000,
            )
        self.assertEqual(raised.exception.reason, PrivacyRejectionReason.INVALID_VALUE)

    def test_boolean_and_fractional_epochs_are_rejected_without_coercion(self) -> None:
        for epoch_ms in (True, 12.5):
            with self.subTest(epoch_ms=epoch_ms):
                with self.assertRaises(PrivacyRejection) as raised:
                    self.event(epoch_ms=epoch_ms)
                self.assertEqual(
                    raised.exception.reason, PrivacyRejectionReason.INVALID_VALUE
                )
                with self.assertRaisesRegex(ValueError, "epoch_ms must be an integer"):
                    SafeEvent.from_wire(
                        {
                            "kind": "session",
                            "title": "Pi turn finished",
                            "epoch_ms": epoch_ms,
                        }
                    )

    def test_all_event_integers_are_bounded_for_json_consumers(self) -> None:
        huge = 10**10_000
        invalid_wires = (
            ("epoch_ms", {"epoch_ms": huge}),
            ("started_epoch_ms", {"started_epoch_ms": huge}),
            ("lines_added", {"lines_added": huge}),
            ("lines_removed", {"lines_removed": huge}),
            ("github.number", {"github": {"number": huge}}),
            ("github.checks_total", {"github": {"checks_total": huge}}),
            ("github.coverage", {"github": {"coverage": huge}}),
        )
        for field_name, addition in invalid_wires:
            with self.subTest(field_name=field_name):
                wire = {
                    "kind": "session",
                    "title": "Pi turn finished",
                    **addition,
                }
                with self.assertRaises(ValueError):
                    SafeEvent.from_wire(wire)

        maximum = SafeEvent.from_wire(
            {
                "kind": "session",
                "title": "Pi turn finished",
                "epoch_ms": MAX_SAFE_INTEGER,
                "started_epoch_ms": MAX_SAFE_INTEGER,
                "lines_added": MAX_SAFE_INTEGER,
                "lines_removed": MAX_SAFE_INTEGER,
            }
        )
        json.dumps(maximum.to_wire())

    def test_root_is_authoritative_and_mismatches_do_not_echo_paths(self) -> None:
        canary = Path(self.directory.name) / "private-canary"
        with self.assertRaises(PrivacyRejection) as raised:
            self.event(project=os.fspath(canary))
        self.assertEqual(
            raised.exception.reason, PrivacyRejectionReason.PROJECT_MISMATCH
        )
        self.assertNotIn("private-canary", str(raised.exception))

        event = self.event()
        self.assertEqual(event.project, os.fspath(self.root.resolve()))

    def test_only_safe_fields_are_available_to_the_panel(self) -> None:
        self.assertEqual(SAFE_PANEL_WIRE_FIELDS, PANEL_SAFE_EVENT_FIELDS)
        self.assertLessEqual(PANEL_SAFE_EVENT_FIELDS, SAFE_EVENT_FIELDS)
        self.assertNotIn("source_event_id", PANEL_SAFE_EVENT_FIELDS)
        self.assertNotIn("herdr_pane_id", PANEL_SAFE_EVENT_FIELDS)
        self.assertNotIn("project", PANEL_SAFE_EVENT_FIELDS)

    def test_compatibility_names_cannot_bypass_the_safe_type(self) -> None:
        self.assertIs(NormalizedEvent, SafeEvent)
        self.assertIs(ActivityEvent, SafeEvent)

    def test_policy_declares_every_current_event_kind_and_status(self) -> None:
        self.assertEqual(
            SAFE_EVENT_KINDS,
            {
                "branch",
                "command",
                "commit",
                "config",
                "file",
                "github",
                "issue",
                "merge",
                "pr",
                "push",
                "search",
                "session",
                "test",
                "todo",
                "worktree",
            },
        )
        self.assertEqual(
            SAFE_EVENT_STATUSES, {"failed", "running", "success", "unknown"}
        )

    def test_command_derived_events_require_known_titles_and_safe_details(self) -> None:
        canary = "COMMAND-SEMANTIC-CANARY-72"
        for wire in (
            {
                "agent": "claude-code",
                "kind": "command",
                "status": "failed",
                "title": "Command failed",
                "detail": f"python3 --token {canary}",
            },
            {
                "agent": "claude-code",
                "kind": "pr",
                "status": "running",
                "title": canary,
                "detail": "gh pr create",
            },
        ):
            with self.subTest(kind=wire["kind"]):
                with self.assertRaises(PrivacyRejection) as raised:
                    safe_event(self.root, wire)
                self.assertEqual(
                    raised.exception.reason, PrivacyRejectionReason.INVALID_VALUE
                )
                self.assertNotIn(canary, str(raised.exception))

    def test_legacy_claude_title_flags_are_replaced_by_fixed_actions(self) -> None:
        canary = "PRIVATE PR TITLE CANARY 72"
        pull_request = safe_event(
            self.root,
            {
                "agent": "claude-code",
                "kind": "pr",
                "status": "running",
                "title": "Opening pull request",
                "detail": canary,
            },
        )
        issue = safe_event(
            self.root,
            {
                "agent": "claude-code",
                "kind": "issue",
                "status": "running",
                "title": "Opening issue",
                "detail": canary,
            },
        )

        self.assertEqual(pull_request.detail, "gh pr create")
        self.assertEqual(issue.detail, "gh issue create")
        self.assertNotIn(canary, json.dumps([pull_request.to_wire(), issue.to_wire()]))

    def test_real_claude_classifier_title_flags_are_scrubbed_at_the_boundary(
        self,
    ) -> None:
        from side_dog.cli import normalized_tool_events

        canary = "CLAUDE-TITLE-CANARY-72"
        for command, expected_kind, expected_detail in (
            (
                f"gh pr create --title '{canary}' --body private",
                "pr",
                "gh pr create",
            ),
            (
                f"gh issue create --title '{canary}' --body private",
                "issue",
                "gh issue create",
            ),
        ):
            with self.subTest(kind=expected_kind):
                produced = normalized_tool_events(
                    {
                        "agent": "claude-code",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    },
                    self.root,
                    status="running",
                )
                event = safe_event(self.root, produced[0])
                self.assertEqual(event.kind, expected_kind)
                self.assertEqual(event.detail, expected_detail)
                self.assertNotIn(canary, json.dumps(event.to_wire()))

    def test_safe_existing_details_and_generic_milestones_render_unchanged(
        self,
    ) -> None:
        cases = (
            ("branch", "Creating branch", "issue_12", "running", "issue_12"),
            (
                "worktree",
                "Creating worktree",
                "git worktree add",
                "running",
                "git worktree add",
            ),
            ("test", "Tests passed", "pytest", "success", "pytest"),
            ("session", "Turn completed", "ready", "success", ""),
        )
        for kind, title, detail, status, expected_detail in cases:
            with self.subTest(kind=kind, title=title):
                event = safe_event(
                    self.root,
                    {
                        "agent": "codex",
                        "kind": kind,
                        "status": status,
                        "title": title,
                        "detail": detail,
                    },
                )
                self.assertEqual(event.title, title)
                self.assertEqual(event.detail, expected_detail)

    def test_titles_are_closed_for_generic_session_and_search_events(self) -> None:
        canary = "UNAPPROVED-TITLE-CANARY-72"
        for kind in ("session", "search"):
            with self.subTest(kind=kind):
                with self.assertRaises(PrivacyRejection) as raised:
                    safe_event(
                        self.root,
                        {
                            "agent": "codex",
                            "kind": kind,
                            "status": "success",
                            "title": canary,
                            "detail": canary,
                        },
                    )
                self.assertEqual(
                    raised.exception.reason, PrivacyRejectionReason.INVALID_VALUE
                )

    def test_search_patterns_are_reduced_to_fixed_semantic_details(self) -> None:
        canary = "SEARCH-PATTERN-CANARY-72"
        code = safe_event(
            self.root,
            {
                "agent": "opencode",
                "kind": "search",
                "status": "success",
                "title": "Searched code",
                "detail": canary,
            },
        )
        files = safe_event(
            self.root,
            {
                "agent": "opencode",
                "kind": "search",
                "status": "success",
                "title": "Searched files",
                "detail": canary,
            },
        )
        self.assertEqual((code.detail, files.detail), ("code", "files"))
        self.assertNotIn(canary, json.dumps([code.to_wire(), files.to_wire()]))

    def test_generic_milestone_cannot_carry_a_raw_command_detail(self) -> None:
        event = safe_event(
            self.root,
            {
                "agent": "codex",
                "kind": "session",
                "status": "success",
                "title": "Turn completed",
                "detail": "python3 --token PRIVATE-CANARY-72",
            },
        )
        self.assertEqual(event.detail, "")


class ObservationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "repo"
        (self.root / "pkg").mkdir(parents=True)

    def test_observation_is_ephemeral_and_hides_raw_values_from_repr(self) -> None:
        canary = "RAW-CANARY-42"
        observation = EventObservation(
            agent="codex",
            command=f"pytest --password {canary}",
            path=canary,
            cwd=canary,
        )
        self.assertFalse(hasattr(observation, "to_wire"))
        self.assertNotIn(canary, repr(observation))

    def test_relative_paths_resolve_against_session_cwd(self) -> None:
        relative = normalize_project_path(
            self.root, "src/example.py", os.fspath(self.root / "pkg")
        )
        self.assertEqual(relative, "pkg/src/example.py")

        event = safe_events(
            self.root,
            EventObservation(
                agent="pi",
                kind="file",
                status="success",
                title="Wrote file",
                path="src/example.py",
                cwd=os.fspath(self.root / "pkg"),
            ),
        )[0]
        self.assertEqual(event.detail, "pkg/src/example.py")

    def test_outside_paths_are_rejected_without_echoing_them(self) -> None:
        canary = os.fspath(Path(self.directory.name) / "secret-canary.txt")
        with self.assertRaises(PrivacyRejection) as raised:
            normalize_project_path(self.root, canary)
        self.assertEqual(
            raised.exception.reason, PrivacyRejectionReason.OUTSIDE_PROJECT
        )
        self.assertNotIn("secret-canary", str(raised.exception))

    def test_test_command_is_classified_without_arguments(self) -> None:
        canary = "COMMAND-CANARY-15"
        event = safe_events(
            self.root,
            EventObservation(
                agent="claude-code",
                session_id="session-1",
                status="running",
                command=f"pytest --password {canary}",
            ),
        )[0]
        payload = json.dumps(event.to_wire())

        self.assertEqual(event.kind, "test")
        self.assertEqual(event.title, "Running tests")
        self.assertEqual(event.detail, "pytest")
        self.assertNotIn(canary, payload)
        self.assertNotIn("--password", payload)

    def test_safe_test_runner_families_survive_durable_validation(self) -> None:
        for runner in ("npm", "pnpm", "yarn", "bun", "make"):
            with self.subTest(runner=runner):
                event = safe_events(
                    self.root,
                    EventObservation(
                        agent="claude-code",
                        kind="test",
                        status="success",
                        title="Tests passed",
                        detail=runner,
                    ),
                )[0]

                self.assertEqual(event.kind, "test")
                self.assertEqual(event.detail, runner)

    def test_private_test_stage_identity_survives_durable_validation(self) -> None:
        stage_id = "test:0123456789abcdef"
        event = safe_events(
            self.root,
            EventObservation(
                agent="claude-code",
                kind="test",
                status="success",
                title="Tests passed",
                detail="pytest",
                task_stage_id=stage_id,
            ),
        )[0]

        self.assertEqual(event.task_stage_id, stage_id)
        self.assertEqual(event.to_wire()["task_stage_id"], stage_id)

    def test_pull_request_command_observation_drops_title_and_body(self) -> None:
        canary = "OBSERVATION-PR-CANARY-72"
        event = safe_events(
            self.root,
            EventObservation(
                agent="claude-code",
                status="running",
                command=(f"gh pr create --title '{canary}' --body 'private {canary}'"),
            ),
        )[0]
        self.assertEqual(event.kind, "pr")
        self.assertEqual(event.title, "Opening pull request")
        self.assertEqual(event.detail, "gh pr create")
        self.assertNotIn(canary, json.dumps(event.to_wire()))

    def test_git_command_classification_round_trips_through_safe_events(self) -> None:
        cases = (
            (
                "git switch feature",
                "running",
                ("branch", "Switching branch", "git branch"),
            ),
            (
                "git switch -c feature",
                "success",
                ("branch", "Branch created", "git branch"),
            ),
            (
                "git worktree add ../feature",
                "success",
                ("worktree", "Worktree updated", "git worktree add"),
            ),
            (
                "git worktree remove ../feature",
                "running",
                ("worktree", "Removing worktree", "git worktree"),
            ),
            (
                "git merge feature",
                "failed",
                ("merge", "Branch merge failed", "git merge"),
            ),
        )
        for command, status, expected in cases:
            with self.subTest(command=command, status=status):
                classified = classify_command(command, status)
                self.assertIsNotNone(classified)
                event = safe_events(
                    self.root,
                    EventObservation(
                        agent="codex", status=status, command=command
                    ),
                )[0]
                self.assertEqual((event.kind, event.title, event.detail), expected)
                self.assertEqual(
                    (classified["kind"], classified["title"], classified["detail"]),
                    expected,
                )

    def test_unknown_failed_command_keeps_program_name_only(self) -> None:
        canary = "OUTPUT-CANARY-19"
        event = safe_events(
            self.root,
            EventObservation(
                agent="opencode",
                status="failed",
                command=f"python3 --token {canary}",
            ),
        )[0]
        self.assertEqual(event.kind, "command")
        self.assertEqual(event.title, "Command failed")
        self.assertEqual(event.detail, "python3")
        self.assertNotIn(canary, json.dumps(event.to_wire()))

    def test_wrapper_option_operands_never_become_persisted_program_names(
        self,
    ) -> None:
        from side_dog.cli import STATE_ENV, append_event, events_path, latest_events

        state = Path(self.directory.name) / "state"
        canaries = ("PRIVATE_USERNAME_72", "PRIVATE_VARIABLE_72")
        commands = (
            f"sudo -u {canaries[0]} false",
            f"env -u {canaries[1]} false",
        )
        with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
            for command in commands:
                event = safe_events(
                    self.root,
                    EventObservation(
                        agent="codex", status="failed", command=command
                    ),
                )[0]
                append_event(self.root, event)
            records = latest_events(events_path(self.root), root=self.root)
            persisted = events_path(self.root).read_text()

        self.assertEqual([record["detail"] for record in records], ["command"] * 2)
        for canary in canaries:
            self.assertNotIn(canary, persisted)

    def test_plain_wrappers_still_report_the_actual_program(self) -> None:
        for command in ("sudo make", "env FOO=bar make"):
            with self.subTest(command=command):
                event = safe_events(
                    self.root,
                    EventObservation(
                        agent="codex", status="failed", command=command
                    ),
                )[0]
                self.assertEqual(event.detail, "make")

    def test_fixed_demo_failure_detail_crosses_the_safe_boundary(self) -> None:
        from side_dog.cli import demo_tour_samples

        sample = next(
            event
            for _delay, event in demo_tour_samples()
            if event.get("detail") == "one intentional demo failure"
        )
        event = safe_event(self.root, sample)

        self.assertEqual(event.kind, "test")
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.detail, "one intentional demo failure")

    def test_compound_command_is_safely_downgraded(self) -> None:
        events = safe_events(
            self.root,
            EventObservation(
                agent="codex", status="failed", command="pytest && secret-tool"
            ),
        )
        self.assertEqual(events, ())

    def test_rejection_diagnostic_uses_only_the_fixed_reason(self) -> None:
        diagnostic = rejection_diagnostic(
            self.root, "pi", PrivacyRejectionReason.UNEXPECTED_FIELD
        )
        wire = diagnostic.to_wire()
        self.assertEqual(wire["title"], "Agent activity omitted")
        self.assertEqual(wire["detail"], "unexpected_field")
        self.assertEqual(wire["schema"], ACTIVITY_SCHEMA)


if __name__ == "__main__":
    unittest.main()
