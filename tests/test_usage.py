from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from side_dog.cli import main, render_usage_banner
from side_dog.config import config_usage
from side_dog.panel import PanelFeed
from side_dog.usage import (
    USAGE_SCHEMA,
    UsageMonitor,
    UsageReport,
    UsageSample,
    ccusage_command,
    ccusage_readiness,
    load_ccusage,
    parse_ccusage_json,
    samples_for_sessions,
    usage_summary,
    usage_summary_wire,
    usage_totals,
)


def sample(**changes: object) -> UsageSample:
    values: dict[str, object] = {
        "agent": "claude-code",
        "period": "session-1",
        "session_id": "session-1",
        "model": "claude-sonnet-4",
        "input_tokens": 1_000,
        "output_tokens": 500,
        "cache_creation_tokens": 100,
        "cache_read_tokens": 400,
        "reasoning_tokens": 200,
        "cost_microusd": 1_250_000,
        "cost_basis": "estimated",
    }
    values.update(changes)
    return UsageSample(**values)  # type: ignore[arg-type]


class UsageBoundaryTests(unittest.TestCase):
    def test_total_does_not_double_count_reasoning_tokens(self) -> None:
        row = sample()

        self.assertEqual(row.total_tokens, 2_000)
        self.assertEqual(row.to_wire()["reasoning_tokens"], 200)

    def test_boundary_rejects_invalid_scalar_values(self) -> None:
        with self.assertRaises(ValueError):
            sample(input_tokens=-1)
        with self.assertRaises(ValueError):
            sample(cost_basis="exact")
        with self.assertRaises(ValueError):
            sample(coverage="unknown")

    def test_usage_boundary_round_trips_and_rejects_unknown_fields(self) -> None:
        report = UsageReport("session", samples=(sample(),))

        self.assertEqual(UsageReport.from_wire(report.to_wire()), report)
        with self.assertRaisesRegex(ValueError, "unsupported usage sample field"):
            UsageSample.from_wire({**sample().to_wire(), "prompt": "private"})

    def test_parser_normalizes_ccusage_daily_rows(self) -> None:
        report = parse_ccusage_json(
            json.dumps(
                {
                    "daily": [
                        {
                            "date": "2026-09-03",
                            "modelsUsed": ["claude-sonnet-4", "claude-opus-4"],
                            "inputTokens": 100,
                            "outputTokens": 20,
                            "cacheCreationTokens": 5,
                            "cacheReadTokens": 50,
                            "totalCost": 0.125,
                        }
                    ]
                }
            ),
            "daily",
        )

        self.assertEqual(report.status, "available")
        self.assertEqual(len(report.samples), 1)
        row = report.samples[0]
        self.assertEqual(row.agent, "claude-code")
        self.assertEqual(row.period, "2026-09-03")
        self.assertEqual(row.total_tokens, 175)
        self.assertEqual(row.cost_microusd, 125_000)
        self.assertEqual(row.cost_basis, "estimated")

    def test_parser_flattens_provider_rows_and_honors_no_cost(self) -> None:
        report = parse_ccusage_json(
            json.dumps(
                {
                    "monthly": [
                        {
                            "month": "2026-09",
                            "agents": [
                                {
                                    "agent": "codex",
                                    "inputTokens": 10,
                                    "outputTokens": 5,
                                    "totalCost": 99,
                                }
                            ],
                        }
                    ]
                }
            ),
            "monthly",
            no_cost=True,
        )

        row = report.samples[0]
        self.assertEqual((row.agent, row.period), ("codex", "2026-09"))
        self.assertIsNone(row.cost_microusd)
        self.assertEqual(row.cost_basis, "omitted")
        self.assertEqual(usage_totals(report.samples)["pricing_coverage"], "omitted")

    def test_missing_cost_is_explicitly_unpriced(self) -> None:
        report = parse_ccusage_json(
            '[{"sessionId":"s","inputTokens":10}]', "session"
        )

        self.assertEqual(report.samples[0].coverage, "partial")
        self.assertEqual(report.samples[0].cost_basis, "unpriced")
        self.assertIn("unpriced", usage_summary(report))

    def test_fallback_pricing_remains_partial_when_every_row_has_cost(self) -> None:
        report = parse_ccusage_json(
            '[{"date":"2026-09-03","inputTokens":10,'
            '"totalCost":0.25,"isFallback":true}]',
            "daily",
        )

        self.assertEqual(usage_totals(report.samples)["pricing_coverage"], "partial")
        self.assertIn("partial pricing", usage_summary(report))

    def test_malformed_json_has_a_safe_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            parse_ccusage_json("not json", "daily")
        with self.assertRaisesRegex(ValueError, "shape is unsupported"):
            parse_ccusage_json('{"unexpected":"private"}', "daily")

    def test_session_filter_is_provider_qualified_and_empty_means_none(self) -> None:
        report = UsageReport(
            "session",
            samples=(
                sample(agent="codex", session_id="same"),
                sample(agent="claude-code", session_id="same"),
            ),
        )

        selected = samples_for_sessions(report, (("codex", "same"),))
        self.assertEqual([row.agent for row in selected], ["codex"])
        self.assertEqual(samples_for_sessions(report, ()), ())
        self.assertEqual(usage_summary_wire(report, ())["rows"], [])

    def test_panel_wire_omits_session_and_activity_identifiers(self) -> None:
        wire = usage_summary_wire(
            UsageReport("session", samples=(sample(last_activity="secret-time"),)),
            (("claude-code", "session-1"),),
        )

        self.assertEqual(wire["schema"], USAGE_SCHEMA)
        row = wire["rows"][0]
        self.assertNotIn("session_id", row)
        self.assertNotIn("period", row)
        self.assertNotIn("last_activity", row)

    def test_summary_distinguishes_recorded_partial_and_stale(self) -> None:
        report = UsageReport(
            "session",
            samples=(
                sample(cost_basis="recorded"),
                sample(
                    agent="codex",
                    session_id="session-2",
                    cost_microusd=None,
                    cost_basis="unpriced",
                    coverage="partial",
                ),
            ),
            status="stale",
        )

        text = usage_summary(report)
        self.assertIn("partial pricing", text)
        self.assertIn("stale", text)


class UsageAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = {
            "enabled": True,
            "command": ("ccusage",),
            "agent": "claude-code",
            "offline": True,
            "refresh_seconds": 30,
        }

    def test_config_accepts_only_valid_usage_settings(self) -> None:
        self.assertEqual(
            config_usage(
                {
                    "usage": {
                        "enabled": False,
                        "command": ["bunx", "ccusage"],
                        "agent": "codex",
                        "offline": False,
                        "refresh_seconds": 60,
                    }
                }
            ),
            {
                "enabled": False,
                "command": ["bunx", "ccusage"],
                "agent": "codex",
                "offline": False,
                "refresh_seconds": 60.0,
            },
        )
        self.assertEqual(
            config_usage(
                {
                    "usage": {
                        "command": "ccusage; rm anything",
                        "refresh_seconds": 1,
                    }
                }
            ),
            {},
        )

    def test_command_is_argv_only_and_includes_filters(self) -> None:
        command = ccusage_command(
            "daily",
            since="2026-09-01",
            until="2026-09-03",
            mode="calculate",
            no_cost=True,
            project="side-dog",
            settings=self.settings,
        )

        self.assertEqual(command[0], "ccusage")
        self.assertIn("--offline", command)
        self.assertEqual(command[command.index("--since") + 1], "2026-09-01")
        self.assertEqual(command[command.index("--project") + 1], "side-dog")
        self.assertIn("--no-cost", command)

    def test_loader_never_invokes_a_shell(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["ccusage"], 0, stdout='{"daily":[]}', stderr=""
            )
        )
        with patch("side_dog.usage.shutil.which", return_value="/bin/ccusage"):
            report = load_ccusage(
                "daily", settings=self.settings, runner=runner, timeout=2
            )

        self.assertEqual(report.status, "available")
        self.assertEqual(runner.call_args.args[0][0], "ccusage")
        self.assertNotIn("shell", runner.call_args.kwargs)
        self.assertEqual(runner.call_args.kwargs["timeout"], 2)

    def test_loader_reports_absent_failure_timeout_and_malformed_output(self) -> None:
        with patch("side_dog.usage.shutil.which", return_value=None):
            self.assertEqual(
                load_ccusage("daily", settings=self.settings).detail,
                "ccusage is not installed",
            )
        cases = (
            (
                Mock(
                    return_value=subprocess.CompletedProcess(
                        ["ccusage"], 2, stdout="", stderr="private details"
                    )
                ),
                "ccusage report failed",
            ),
            (
                Mock(side_effect=subprocess.TimeoutExpired("ccusage", 2)),
                "ccusage timed out",
            ),
            (
                Mock(
                    return_value=subprocess.CompletedProcess(
                        ["ccusage"], 0, stdout="bad", stderr=""
                    )
                ),
                "ccusage returned malformed JSON",
            ),
        )
        with patch("side_dog.usage.shutil.which", return_value="/bin/ccusage"):
            for runner, detail in cases:
                with self.subTest(detail=detail):
                    self.assertEqual(
                        load_ccusage(
                            "daily", settings=self.settings, runner=runner
                        ).detail,
                        detail,
                    )

    def test_monitor_replaces_snapshots_and_retains_only_stale_safe_rows(self) -> None:
        reports = iter(
            (
                UsageReport("session", samples=(sample(),)),
                UsageReport("session", status="unavailable", detail="refresh failed"),
            )
        )
        monitor = UsageMonitor(
            settings={**self.settings, "refresh_seconds": 5},
            loader=lambda *_args, **_kwargs: next(reports),
        )
        try:
            monitor.tick(now=0)
            assert monitor._future is not None
            monitor._future.result(timeout=1)
            self.assertTrue(monitor.tick(now=1))
            self.assertEqual(monitor.report.status, "available")
            monitor.tick(now=6)
            assert monitor._future is not None
            monitor._future.result(timeout=1)
            self.assertTrue(monitor.tick(now=7))
            self.assertEqual(monitor.report.status, "stale")
            self.assertEqual(monitor.report.samples[0].session_id, "session-1")
            self.assertEqual(monitor.report.detail, "refresh failed")
        finally:
            monitor.close()

    def test_doctor_probe_checks_version_and_json_compatibility(self) -> None:
        calls: list[list[str]] = []

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "17.0.0\n", "")
            return subprocess.CompletedProcess(command, 0, '{"sessions":[]}', "")

        with (
            patch("side_dog.usage.shutil.which", return_value="/bin/ccusage"),
            patch("side_dog.usage.subprocess.run", side_effect=runner),
        ):
            status, detail = ccusage_readiness(self.settings)

        self.assertEqual(status, "ok")
        self.assertIn("JSON report compatible", detail)
        self.assertEqual(len(calls), 2)


class UsageSurfaceTests(unittest.TestCase):
    def test_cli_prints_json_schema_and_filters_agent(self) -> None:
        report = UsageReport(
            "daily",
            samples=(sample(agent="codex"), sample(agent="claude-code")),
        )
        output = io.StringIO()
        with (
            patch("side_dog.cli.load_ccusage", return_value=report),
            redirect_stdout(output),
        ):
            code = main(["usage", "daily", "--agent", "codex", "--json"])

        document = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(document["schema"], USAGE_SCHEMA)
        self.assertEqual([row["agent"] for row in document["rows"]], ["codex"])

    def test_terminal_banner_uses_only_sessions_for_the_root(self) -> None:
        report = UsageReport(
            "session",
            samples=(
                sample(agent="codex", session_id="visible"),
                sample(agent="claude-code", session_id="hidden"),
            ),
        )
        text = render_usage_banner(
            report,
            ({"agent": "codex", "session_id": "visible"},),
            {},
            120,
            False,
        )

        self.assertIn("2K tok", text)
        self.assertNotIn("4K tok", text)

    def test_panel_snapshot_contains_reduced_root_scoped_usage(self) -> None:
        monitor = Mock()
        monitor.report = UsageReport(
            "session", samples=(sample(agent="codex", session_id="visible"),)
        )
        monitor.tick.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("side_dog.panel.UsageMonitor", return_value=monitor),
                patch("side_dog.panel.pinned_folders", return_value=[]),
                patch("side_dog.panel.load_git_state", return_value={}),
            ):
                feed = PanelFeed([root], follow_worktrees=False, notify=False)
                feed.roots[0].identities = {
                    "codex:visible": {
                        "agent": "codex",
                        "session_id": "visible",
                        "root": str(root),
                    }
                }
                try:
                    usage = feed.snapshot()["roots"][0]["usage"]
                finally:
                    feed.close()

        self.assertEqual(usage["status"], "available")
        self.assertEqual(usage["rows"][0]["agent"], "codex")
        self.assertNotIn("session_id", usage["rows"][0])
        monitor.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
