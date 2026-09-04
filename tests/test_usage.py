from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from side_dog.cli import (
    initialize_watch_root,
    main,
    refreshed_usage_contexts,
    render,
    render_usage_banner,
    usage_display_snapshot,
)
from side_dog.config import config_usage
from side_dog.panel import PANEL_HTML, PanelFeed
from side_dog.usage import (
    LIVE_USAGE_SCHEMA,
    USAGE_SCHEMA,
    LiveUsageSnapshot,
    UnpricedModel,
    UsageBlock,
    UsageMonitor,
    UsageReport,
    UsageSample,
    ccusage_command,
    ccusage_block_command,
    ccusage_readiness,
    load_ccusage,
    load_ccusage_block,
    live_usage_lines,
    parse_ccusage_block_json,
    parse_ccusage_json,
    samples_for_sessions,
    usage_gauge_line,
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
        "uncategorized_tokens": 0,
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

    def test_live_snapshot_round_trips_without_raw_ccusage_rows(self) -> None:
        snapshot = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(),), pricing_source="online"),
            UsageReport("session", samples=(sample(),), pricing_source="cached"),
            UsageBlock(
                status="available",
                total_tokens=50,
                cost_microusd=250_000,
                pricing_source="online",
            ),
        )

        self.assertEqual(LiveUsageSnapshot.from_wire(snapshot.to_wire()), snapshot)
        self.assertNotIn("prompt", json.dumps(snapshot.to_wire()))

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

    def test_parser_clamps_oversized_integer_counts_without_float_conversion(self) -> None:
        report = parse_ccusage_json(
            json.dumps(
                {"sessions": [{"sessionId": "s", "inputTokens": 10**1_000}]}
            ),
            "session",
        )

        self.assertEqual(report.samples[0].input_tokens, 2**53 - 1)

    def test_parser_rejects_present_invalid_token_counts(self) -> None:
        invalid_values = (None, "10", True, -1, 1.5, float("nan"), float("inf"))

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "must be a non-negative integer"
            ):
                parse_ccusage_json(
                    json.dumps(
                        {
                            "daily": [
                                {"date": "2026-09-03", "inputTokens": value}
                            ]
                        }
                    ),
                    "daily",
                )

    def test_parser_preserves_aggregate_only_total_as_uncategorized(self) -> None:
        report = parse_ccusage_json(
            '{"sessions":[{"sessionId":"s","totalTokens":100}]}',
            "session",
        )

        row = report.samples[0]
        self.assertEqual(row.uncategorized_tokens, 100)
        self.assertEqual(row.total_tokens, 100)
        self.assertEqual(usage_totals(report.samples)["total_tokens"], 100)

    def test_parser_does_not_add_total_tokens_to_component_rows(self) -> None:
        report = parse_ccusage_json(
            '{"sessions":[{"sessionId":"s","inputTokens":60,'
            '"outputTokens":40,"totalTokens":100}]}',
            "session",
        )

        self.assertEqual(report.samples[0].uncategorized_tokens, 0)
        self.assertEqual(report.samples[0].total_tokens, 100)

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

    def test_unpriced_model_is_named_and_excluded_from_priced_tokens(self) -> None:
        report = parse_ccusage_json(
            json.dumps(
                {
                    "sessions": [
                        {
                            "sessionId": "s",
                            "inputTokens": 110,
                            "totalCost": 0.25,
                            "modelBreakdowns": [
                                {
                                    "modelName": "known",
                                    "inputTokens": 100,
                                    "costUSD": 0.25,
                                },
                                {
                                    "modelName": "fable-new",
                                    "inputTokens": 10,
                                    "costUSD": 0,
                                },
                            ],
                        }
                    ]
                }
            ),
            "session",
        )

        totals = usage_totals(report.samples)
        self.assertEqual(totals["pricing_coverage"], "partial")
        self.assertEqual(totals["priced_tokens"], 100)
        self.assertEqual(
            totals["unpriced_models"], [{"model": "fable-new", "tokens": 10}]
        )

    def test_malformed_json_has_a_safe_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "malformed JSON"):
            parse_ccusage_json("not json", "daily")
        with self.assertRaisesRegex(ValueError, "shape is unsupported"):
            parse_ccusage_json('{"unexpected":"private"}', "daily")
        with self.assertRaisesRegex(ValueError, "rows must contain objects"):
            parse_ccusage_json('{"daily":["private"]}', "daily")
        with self.assertRaisesRegex(ValueError, "project entries must be arrays"):
            parse_ccusage_json(
                '{"projects":{"valid":[],"unsupported":{"daily":[]}}}',
                "daily",
            )
        with self.assertRaisesRegex(ValueError, "project rows must contain objects"):
            parse_ccusage_json('{"projects":{"invalid":["private"]}}', "daily")

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

    def test_usage_agents_share_the_integration_registry_aliases(self) -> None:
        aliases = {
            "cursor-agent": "cursor",
            "grok-build": "grok",
            "dsh": "deepseek",
            "antigravity-cli": "antigravity",
        }
        for alias, provider in aliases.items():
            with self.subTest(alias=alias):
                report = parse_ccusage_json(
                    '[{"sessionId":"same","inputTokens":10}]',
                    "session",
                    default_agent=alias,
                )
                selected = samples_for_sessions(
                    report, ((provider, "same"),)
                )
                self.assertEqual(len(selected), 1)

    def test_panel_wire_omits_session_identifiers_but_keeps_last_activity(self) -> None:
        wire = usage_summary_wire(
            UsageReport("session", samples=(sample(last_activity="secret-time"),)),
            (("claude-code", "session-1"),),
        )

        self.assertEqual(wire["schema"], LIVE_USAGE_SCHEMA)
        row = wire["rows"][0]
        self.assertNotIn("session_id", row)
        self.assertNotIn("period", row)
        self.assertEqual(row["last_activity"], "secret-time")

    def test_live_wire_attributes_active_and_historical_sessions_privately(self) -> None:
        captured = 1_800_000
        snapshot = LiveUsageSnapshot(
            UsageReport(
                "session",
                samples=(
                    sample(
                        agent="codex",
                        session_id="active-raw-id",
                        cost_microusd=300_000,
                        last_activity="2026-09-03T17:58:00Z",
                    ),
                ),
                captured_epoch_ms=captured,
                pricing_source="online",
            ),
            UsageReport(
                "session",
                samples=(
                    sample(
                        agent="codex",
                        session_id="active-raw-id",
                        cost_microusd=900_000,
                        last_activity="2026-09-03T17:58:00Z",
                    ),
                    sample(
                        session_id="historical-raw-id",
                        last_activity="2026-09-03T09:12:00Z",
                    ),
                ),
                captured_epoch_ms=captured,
                pricing_source="cached",
            ),
            UsageBlock(
                status="available",
                captured_epoch_ms=captured,
                pricing_source="online",
                cost_microusd=2_550_000,
                burn_rate_microusd_per_hour=102_000_000,
                remaining_minutes=239,
            ),
        )
        wire = usage_summary_wire(
            snapshot,
            (("codex", "active-raw-id"), ("claude-code", "historical-raw-id")),
            (
                {
                    "agent": "codex",
                    "session_id": "active-raw-id",
                    "label": "Current task",
                    "status": "working",
                },
            ),
            root_count=2,
            now_epoch_ms=captured + 1_000,
        )

        self.assertIn("$2.55 this block", wire["label"])
        self.assertIn("$102.00/hr", wire["label"])
        self.assertIn("today $0.30", wire["label"])
        self.assertIn("Tracked lifetime · 2 shown roots", wire["lines"][1])
        self.assertIn("2 matched sessions", wire["lines"][1])
        self.assertEqual([row["status"] for row in wire["rows"]], ["active", "history"])
        self.assertEqual(wire["rows"][0]["label"], "Current task")
        self.assertNotIn("active-raw-id", json.dumps(wire))
        self.assertIn("online (1s old)", wire["pricing_label"])

    def test_compact_usage_lines_name_period_scope_and_api_estimate(self) -> None:
        report = UsageReport("session", samples=(sample(),), captured_epoch_ms=1)
        snapshot = LiveUsageSnapshot(
            report,
            report,
            UsageBlock(
                status="available",
                captured_epoch_ms=1,
                cost_microusd=2_550_000,
                burn_rate_microusd_per_hour=8_110_000,
                remaining_minutes=231,
            ),
        )

        with patch("side_dog.usage._captured_label", return_value="09:08"):
            lines = live_usage_lines(snapshot, root_count=8, now_epoch_ms=1)

        self.assertEqual(
            lines,
            (
                "Today · 8 shown roots · API est $1.25 · 2K tok · as of 09:08",
                "Current 5h window · machine-wide · API est $2.55 · "
                "API pace $8.11/hr · ends in 3h 51m · 0 active sessions · "
                "as of 09:08",
                "Tracked lifetime · 8 shown roots · API est $1.25 · 2K tok · "
                "1 matched session · as of 09:08",
            ),
        )

    def test_gauge_shows_elapsed_block_cost_pace_today_and_oldest_capture(self) -> None:
        now = int(
            datetime.fromisoformat("2026-09-03T17:30:00+00:00").timestamp()
            * 1000
        )
        today = UsageReport(
            "session",
            samples=(sample(cost_microusd=88_950_000),),
            captured_epoch_ms=now,
        )
        snapshot = LiveUsageSnapshot(
            today,
            today,
            UsageBlock(
                status="available",
                captured_epoch_ms=now - 1_000,
                start_time="2026-09-03T15:00:00Z",
                end_time="2026-09-03T20:00:00Z",
                cost_microusd=23_000_000,
                burn_rate_microusd_per_hour=10_480_000,
                remaining_minutes=150,
            ),
        )

        with patch("side_dog.usage._captured_label", return_value="10:33") as label:
            line, details = usage_gauge_line(
                snapshot, now_epoch_ms=now, width=100
            )

        self.assertEqual(
            line,
            "$23.00 this block ▰▰▰▰▱▱▱▱ 2h 30m left · "
            "$10.48/hr · today $88.95 · as of 10:33",
        )
        self.assertEqual(details, ())
        label.assert_called_once_with(now - 1_000)

    def test_gauge_degrades_in_order_as_width_narrows(self) -> None:
        now = int(
            datetime.fromisoformat("2026-09-03T17:30:00+00:00").timestamp()
            * 1000
        )
        report = UsageReport("session", samples=(sample(),), captured_epoch_ms=now)
        snapshot = LiveUsageSnapshot(
            report,
            report,
            UsageBlock(
                status="available",
                captured_epoch_ms=now,
                start_time="2026-09-03T15:00:00Z",
                end_time="2026-09-03T20:00:00Z",
                cost_microusd=23_000_000,
                burn_rate_microusd_per_hour=10_480_000,
                remaining_minutes=150,
            ),
        )

        at_100 = usage_gauge_line(snapshot, now_epoch_ms=now, width=100)[0]
        at_80 = usage_gauge_line(snapshot, now_epoch_ms=now, width=80)[0]
        at_60 = usage_gauge_line(snapshot, now_epoch_ms=now, width=60)[0]
        no_age = usage_gauge_line(snapshot, now_epoch_ms=now, width=64)[0]
        no_today = usage_gauge_line(snapshot, now_epoch_ms=now, width=50)[0]
        short_bar = usage_gauge_line(snapshot, now_epoch_ms=now, width=46)[0]
        no_pace = usage_gauge_line(snapshot, now_epoch_ms=now, width=34)[0]

        self.assertIn("as of", at_100)
        self.assertLessEqual(len(at_100), 100)
        self.assertLessEqual(len(at_80), 80)
        self.assertLessEqual(len(at_60), 60)
        self.assertNotIn("as of", no_age)
        self.assertIn("today", no_age)
        self.assertNotIn("today", no_today)
        self.assertEqual(no_today.count("▰") + no_today.count("▱"), 8)
        self.assertEqual(short_bar.count("▰") + short_bar.count("▱"), 4)
        self.assertNotIn("/hr", no_pace)

    def test_gauge_omits_bar_when_block_is_unavailable(self) -> None:
        report = UsageReport("session", samples=(sample(),), captured_epoch_ms=2_000)
        snapshot = LiveUsageSnapshot(
            report,
            report,
            UsageBlock(captured_epoch_ms=1_000, detail="private failure"),
        )

        with patch("side_dog.usage._captured_label", return_value="09:08") as label:
            line = usage_gauge_line(snapshot, now_epoch_ms=2_000)[0]

        self.assertIn("block unavailable", line)
        self.assertNotIn("▰", line)
        self.assertNotIn("▱", line)
        self.assertNotIn("private failure", line)
        label.assert_called_once_with(2_000)

    def test_gauge_capture_age_ignores_unavailable_inputs(self) -> None:
        unavailable = UsageReport(
            "session", status="unavailable", captured_epoch_ms=1_000
        )
        block = UsageBlock(status="available", captured_epoch_ms=2_000)
        snapshot = LiveUsageSnapshot(unavailable, unavailable, block)

        with patch("side_dog.usage._captured_label", return_value="09:08") as label:
            line = usage_gauge_line(snapshot, now_epoch_ms=2_000)[0]

        self.assertIn("as of 09:08", line)
        label.assert_called_once_with(2_000)

        empty = LiveUsageSnapshot(
            unavailable,
            unavailable,
            UsageBlock(captured_epoch_ms=500),
        )
        line = usage_gauge_line(empty, now_epoch_ms=2_000)[0]
        self.assertNotIn("as of", line)

    def test_partial_pricing_replaces_capture_age_with_unpriced_detail(self) -> None:
        partial = sample(
            coverage="partial",
            unpriced_models=(UnpricedModel("new-model", 750),),
        )
        report = UsageReport("session", samples=(partial,), captured_epoch_ms=2_000)
        snapshot = LiveUsageSnapshot(
            report,
            report,
            UsageBlock(
                status="available",
                captured_epoch_ms=1_000,
                cost_microusd=2_550_000,
            ),
        )

        line = usage_gauge_line(snapshot, now_epoch_ms=2_000)[0]

        self.assertNotIn("as of", line)
        self.assertTrue(line.endswith("unpriced: new-model 750 tok"))
        narrow = usage_gauge_line(snapshot, now_epoch_ms=2_000, width=34)[0]
        self.assertLessEqual(len(narrow), 34)
        self.assertEqual(narrow, "unpriced: new-model 750 tok")

    def test_partial_pricing_without_a_breakdown_replaces_capture_age(self) -> None:
        partial = sample(coverage="partial", unpriced_models=())
        report = UsageReport("session", samples=(partial,), captured_epoch_ms=2_000)
        snapshot = LiveUsageSnapshot(
            report,
            report,
            UsageBlock(
                status="available",
                captured_epoch_ms=1_000,
                cost_microusd=2_550_000,
            ),
        )

        line, details = usage_gauge_line(
            snapshot, now_epoch_ms=2_000, include_details=True
        )

        self.assertNotIn("as of", line)
        self.assertTrue(line.endswith("partial pricing"))
        self.assertIn("partial pricing", details[0])

        narrow = usage_gauge_line(snapshot, now_epoch_ms=2_000, width=34)[0]
        self.assertLessEqual(len(narrow), 34)
        self.assertEqual(narrow, "$2.55 block · partial pricing")

        banner = render_usage_banner(
            snapshot,
            (),
            {},
            34,
            False,
            sessions=(("claude-code", "session-1"),),
        )
        self.assertLessEqual(len(banner), 34)
        self.assertIn("partial pricing", banner)

    def test_gauge_marks_old_inputs_stale_and_expands_lifetime_detail(self) -> None:
        report = UsageReport("session", samples=(sample(),), captured_epoch_ms=1_000)
        snapshot = LiveUsageSnapshot(
            report,
            report,
            UsageBlock(status="available", captured_epoch_ms=2_000),
        )

        line, details = usage_gauge_line(
            snapshot,
            now_epoch_ms=400_000,
            session_cadence=180,
            block_cadence=10,
            include_details=True,
        )

        self.assertIn("stale", line)
        self.assertEqual(len(details), 1)
        self.assertIn("Tracked lifetime", details[0])

    def test_unmatched_today_staleness_does_not_mark_focused_gauge_stale(self) -> None:
        stale_by_status = UsageReport(
            "session",
            samples=(sample(),),
            status="stale",
            captured_epoch_ms=400_000,
        )
        stale_by_age = UsageReport(
            "session",
            samples=(sample(),),
            captured_epoch_ms=1_000,
        )
        for name, today, now in (
            ("status", stale_by_status, 400_000),
            ("age", stale_by_age, 400_000),
        ):
            with self.subTest(name=name):
                snapshot = LiveUsageSnapshot(
                    today,
                    today,
                    UsageBlock(
                        status="available",
                        captured_epoch_ms=now,
                        cost_microusd=2_550_000,
                    ),
                )
                line = usage_gauge_line(
                    snapshot,
                    (("codex", "unmatched-session"),),
                    now_epoch_ms=now,
                    session_cadence=180,
                )[0]

                self.assertNotIn("stale", line)
                self.assertIn("today unavailable", line)

    def test_matched_today_staleness_marks_focused_gauge_stale(self) -> None:
        for name, status, captured, now in (
            ("status", "stale", 400_000, 400_000),
            ("age", "available", 1_000, 400_000),
        ):
            with self.subTest(name=name):
                today = UsageReport(
                    "session",
                    samples=(sample(),),
                    status=status,
                    captured_epoch_ms=captured,
                )
                snapshot = LiveUsageSnapshot(
                    today,
                    today,
                    UsageBlock(
                        status="available",
                        captured_epoch_ms=now,
                        cost_microusd=2_550_000,
                    ),
                )
                line = usage_gauge_line(
                    snapshot,
                    (("claude-code", "session-1"),),
                    now_epoch_ms=now,
                    session_cadence=180,
                )[0]

                self.assertIn("stale", line)

    def test_narrow_complete_pricing_keeps_stale_visible(self) -> None:
        report = UsageReport("session", samples=(sample(),), captured_epoch_ms=2_000)
        snapshot = LiveUsageSnapshot(
            report,
            report,
            UsageBlock(
                status="stale",
                captured_epoch_ms=1_000,
                cost_microusd=2_550_000,
            ),
        )

        line = usage_gauge_line(snapshot, now_epoch_ms=2_000, width=27)[0]
        self.assertEqual(line, "$2.55 block · stale")
        self.assertLessEqual(len(line), 27)

        banner = render_usage_banner(
            snapshot,
            (),
            {},
            28,
            False,
            sessions=(("claude-code", "session-1"),),
        )
        self.assertLessEqual(len(banner), 28)
        self.assertIn("$2.55 block", banner)
        self.assertTrue(banner.endswith("stale"))

    def test_successful_but_old_snapshots_are_marked_stale_by_age(self) -> None:
        snapshot = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(),), captured_epoch_ms=1_000),
            UsageReport("session", samples=(sample(),), captured_epoch_ms=1_000),
            UsageBlock(status="available", captured_epoch_ms=1_000),
        )

        lines = live_usage_lines(
            snapshot,
            (("claude-code", "session-1"),),
            now_epoch_ms=1_000 + 361_000,
            session_cadence=180,
            block_cadence=10,
        )

        self.assertTrue(all("stale" in line for line in lines))

    def test_stale_block_keeps_last_good_values_visible(self) -> None:
        snapshot = LiveUsageSnapshot(
            UsageReport("session", status="unavailable", detail="loading"),
            UsageReport("session", status="unavailable", detail="loading"),
            UsageBlock(
                status="stale",
                cost_microusd=2_550_000,
                burn_rate_microusd_per_hour=102_000_000,
                remaining_minutes=239,
                detail="refresh failed",
            ),
        )

        block_line = live_usage_lines(snapshot)[1]

        self.assertIn("$2.55", block_line)
        self.assertIn("stale", block_line)

    def test_wire_uses_configured_cadences_for_age_staleness(self) -> None:
        captured = 1_000
        snapshot = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(),), captured_epoch_ms=captured),
            UsageReport("session", samples=(sample(),), captured_epoch_ms=captured),
            UsageBlock(status="available", captured_epoch_ms=captured),
        )

        wire = usage_summary_wire(
            snapshot,
            (("claude-code", "session-1"),),
            now_epoch_ms=captured + 30_000,
            session_cadence=3_600,
            block_cadence=60,
        )

        self.assertTrue(all("stale" not in line for line in wire["lines"]))

    def test_panel_wire_pricing_timestamps_are_stable_between_polls(self) -> None:
        snapshot = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(),), captured_epoch_ms=1_000),
            UsageReport("session", samples=(sample(),), captured_epoch_ms=1_000),
            UsageBlock(status="available", captured_epoch_ms=1_000),
        )

        first = usage_summary_wire(
            snapshot,
            (("claude-code", "session-1"),),
            now_epoch_ms=10_000,
            include_pricing_age=False,
        )
        second = usage_summary_wire(
            snapshot,
            (("claude-code", "session-1"),),
            now_epoch_ms=11_000,
            include_pricing_age=False,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["pricing"]["today"]["captured_epoch_ms"], 1_000)
        self.assertNotIn("age", first["pricing"]["today"])

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
        self.assertIn("API recorded $1.25", text)
        self.assertIn("partial pricing", text)
        self.assertIn("stale", text)

    def test_cache_percentage_is_bounded_after_safe_integer_saturation(self) -> None:
        report = UsageReport(
            "daily",
            samples=(
                sample(
                    input_tokens=0,
                    output_tokens=0,
                    cache_creation_tokens=2**53 - 1,
                    cache_read_tokens=2**53 - 1,
                ),
            ),
        )

        self.assertIn("100% cached", usage_summary(report))


class UsageAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = {
            "enabled": True,
            "command": ("ccusage",),
            "agent": "claude-code",
            "offline": True,
            "refresh_seconds": 30,
            "block_refresh_seconds": 10,
            "session_refresh_seconds": 180,
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
                        "block_refresh_seconds": 10,
                        "session_refresh_seconds": 180,
                    }
                }
            ),
            {
                "enabled": False,
                "command": ["bunx", "ccusage"],
                "agent": "codex",
                "offline": False,
                "refresh_seconds": 60.0,
                "block_refresh_seconds": 10.0,
                "session_refresh_seconds": 180.0,
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

    def test_command_is_argv_only_and_excludes_unsupported_project_flags(self) -> None:
        for view in ("daily", "monthly"):
            with self.subTest(view=view):
                command = ccusage_command(
                    view,
                    since="2026-09-01",
                    until="2026-09-03",
                    mode="calculate",
                    no_cost=True,
                    settings=self.settings,
                )

                self.assertEqual(command[0], "ccusage")
                self.assertIn("--offline", command)
                self.assertEqual(
                    command[command.index("--since") + 1], "2026-09-01"
                )
                self.assertIn("--no-cost", command)
                self.assertNotIn("--instances", command)
                self.assertNotIn("--project", command)

    def test_active_block_command_and_parser_use_ccusage_projection(self) -> None:
        self.assertEqual(
            ccusage_block_command(settings=self.settings),
            ["ccusage", "blocks", "--active", "--json", "--offline"],
        )
        block = parse_ccusage_block_json(
            json.dumps(
                {
                    "blocks": [
                        {
                            "isActive": True,
                            "startTime": "2026-09-03T15:00:00Z",
                            "endTime": "2026-09-03T20:00:00Z",
                            "totalTokens": 1_000,
                            "costUSD": 2.55,
                            "models": ["gpt-5.6"],
                            "burnRate": {"costPerHour": 102},
                            "projection": {
                                "remainingMinutes": 239,
                                "totalCost": 25,
                                "totalTokens": 9_000,
                            },
                        }
                    ]
                }
            ),
            pricing_source="online",
        )

        self.assertEqual(block.cost_microusd, 2_550_000)
        self.assertEqual(block.burn_rate_microusd_per_hour, 102_000_000)
        self.assertEqual(block.remaining_minutes, 239)
        self.assertEqual(block.pricing_source, "online")

    def test_online_pricing_failure_falls_back_to_cached_for_session_and_block(self) -> None:
        calls: list[list[str]] = []

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if "--offline" not in command:
                return subprocess.CompletedProcess(command, 2, "", "network")
            body = (
                '{"blocks":[{"isActive":true,"totalTokens":10,"costUSD":0.2}]}'
                if "blocks" in command
                else '{"sessions":[{"sessionId":"s","inputTokens":10,"totalCost":0.2}]}'
            )
            return subprocess.CompletedProcess(command, 0, body, "")

        online = {**self.settings, "offline": False}
        with patch("side_dog.usage.shutil.which", return_value="/bin/ccusage"):
            report = load_ccusage("session", settings=online, runner=runner)
            block = load_ccusage_block(settings=online, runner=runner)

        self.assertEqual(report.pricing_source, "cached")
        self.assertEqual(block.pricing_source, "cached")
        self.assertEqual(len(calls), 4)
        self.assertNotIn("--offline", calls[0])
        self.assertIn("--offline", calls[1])
        self.assertEqual(calls[2][1:4], ["blocks", "--active", "--json"])

    def test_block_loader_caps_each_poll_at_two_seconds(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                ["ccusage"], 0, stdout='{"blocks":[]}', stderr=""
            )
        )
        with patch("side_dog.usage.shutil.which", return_value="/bin/ccusage"):
            load_ccusage_block(settings=self.settings, runner=runner)

        self.assertEqual(runner.call_args.kwargs["timeout"], 2)

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
                UsageReport("session", samples=(sample(),)),
                UsageReport("session", status="unavailable", detail="refresh failed"),
            )
        )
        monitor = UsageMonitor(
            settings={**self.settings, "session_refresh_seconds": 60},
            loader=lambda *_args, **_kwargs: next(reports),
            block_loader=lambda **_kwargs: UsageBlock(detail="no block"),
        )
        try:
            monitor.tick(now=0)
            assert monitor._future is not None
            monitor._future.result(timeout=1)
            self.assertTrue(monitor.tick(now=1))
            self.assertEqual(monitor.report.status, "available")
            monitor.tick(now=60)
            assert monitor._future is not None
            monitor._future.result(timeout=1)
            self.assertTrue(monitor.tick(now=61))
            self.assertEqual(monitor.today_report.status, "available")
            monitor.tick(now=120)
            assert monitor._future is not None
            monitor._future.result(timeout=1)
            self.assertTrue(monitor.tick(now=121))
            self.assertEqual(monitor.report.status, "stale")
            self.assertEqual(monitor.report.samples[0].session_id, "session-1")
            self.assertEqual(monitor.report.detail, "refresh failed")
        finally:
            monitor.close()

    def test_monitor_clears_expired_block_after_successful_inactive_poll(self) -> None:
        previous = UsageBlock(
            status="available",
            cost_microusd=2_550_000,
            remaining_minutes=1,
        )
        inactive = UsageBlock(detail="no active usage block")

        retained = UsageMonitor._retain_block(previous, inactive)

        self.assertEqual(retained.status, "unavailable")
        self.assertIsNone(retained.cost_microusd)

    def test_monitor_staggers_full_scans_and_uses_calendar_today_after_restart(self) -> None:
        calls: list[tuple[str, str | None]] = []

        def loader(view: str, **kwargs: object) -> UsageReport:
            calls.append((view, kwargs.get("since") if isinstance(kwargs.get("since"), str) else None))
            return UsageReport("session", samples=(sample(),))

        monitor = UsageMonitor(
            settings={**self.settings, "session_refresh_seconds": 180},
            loader=loader,
            block_loader=lambda **_kwargs: UsageBlock(detail="no block"),
        )
        try:
            monitor.tick(now=0)
            assert monitor._future is not None
            monitor._future.result(timeout=1)
            monitor.tick(now=1)
            monitor.tick(now=59)
            self.assertEqual(calls, [("session", None)])
            monitor.tick(now=60)
            assert monitor._future is not None
            monitor._future.result(timeout=1)
            monitor.tick(now=61)
        finally:
            monitor.close()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1], time.strftime("%Y-%m-%d"))

    def test_monitor_close_terminates_an_active_usage_process(self) -> None:
        monitor = UsageMonitor(
            settings={
                **self.settings,
                "command": (
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ),
            }
        )
        monitor.tick(now=0)
        assert monitor._future is not None
        deadline = time.monotonic() + 1
        while not monitor._future.running() and time.monotonic() < deadline:
            time.sleep(0.01)

        started = time.monotonic()
        monitor.close()

        self.assertLess(time.monotonic() - started, 2)

    def test_doctor_probe_checks_version_and_json_compatibility(self) -> None:
        calls: list[list[str]] = []

        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "17.0.0\n", "")
            view = "daily" if "daily" in command else "session"
            return subprocess.CompletedProcess(
                command, 0, json.dumps({view: []}), ""
            )

        with (
            patch("side_dog.usage.shutil.which", return_value="/bin/ccusage"),
            patch("side_dog.usage.subprocess.run", side_effect=runner),
        ):
            status, detail = ccusage_readiness(self.settings)

        self.assertEqual(status, "ok")
        self.assertIn("JSON report compatible", detail)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1][0], "ccusage")
        self.assertEqual(calls[1][1], "session")
        self.assertEqual(calls[2][1], "daily")
        self.assertNotIn("--instances", calls[2])
        self.assertNotIn("--project", calls[2])

    def test_doctor_warns_when_daily_json_is_rejected(self) -> None:
        def runner(
            command: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if "--version" in command:
                return subprocess.CompletedProcess(command, 0, "20.0.20\n", "")
            if "daily" in command:
                return subprocess.CompletedProcess(command, 2, "", "bad flag")
            return subprocess.CompletedProcess(command, 0, '{"session":[]}', "")

        with (
            patch("side_dog.usage.shutil.which", return_value="/bin/ccusage"),
            patch("side_dog.usage.subprocess.run", side_effect=runner),
        ):
            status, detail = ccusage_readiness(self.settings)

        self.assertEqual(status, "warn")
        self.assertIn("daily JSON report", detail)


class UsageSurfaceTests(unittest.TestCase):
    def test_browser_pricing_age_stops_while_display_is_paused(self) -> None:
        self.assertIn("if(!state.paused)refreshPricingAges()", PANEL_HTML)

    def test_missing_live_identity_is_retained_as_idle_history(self) -> None:
        previous = {
            ("codex", "session-1"): {
                "agent": "codex",
                "session_id": "session-1",
                "label": "Finished task",
                "model": "gpt-5.6",
                "status": "working",
            }
        }

        refreshed = refreshed_usage_contexts(previous, {}, Path("project"))

        self.assertEqual(refreshed[("codex", "session-1")]["label"], "Finished task")
        self.assertEqual(refreshed[("codex", "session-1")]["status"], "idle")

    def test_cli_refuses_root_for_daily_and_monthly_before_loading(self) -> None:
        for view in ("daily", "monthly"):
            output = io.StringIO()
            with (
                self.subTest(view=view),
                patch("side_dog.cli.load_ccusage") as loader,
                redirect_stderr(output),
            ):
                code = main(["usage", view, "--root", "."])

            self.assertEqual(code, 2)
            self.assertIn("cannot scope daily or monthly", output.getvalue())
            loader.assert_not_called()

    def test_pause_snapshot_holds_usage_report_and_session_scope(self) -> None:
        paused = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(session_id="paused"),)),
            UsageReport("session", samples=(sample(session_id="paused"),)),
            UsageBlock(status="available", total_tokens=10),
        )
        live = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(session_id="live"),)),
            UsageReport("session", samples=(sample(session_id="live"),)),
            UsageBlock(status="available", total_tokens=20),
        )
        paused_sessions = {"root": frozenset({("claude-code", "paused")})}
        live_sessions = {"root": {("claude-code", "live")}}

        report, sessions = usage_display_snapshot(
            live, live_sessions, paused, paused_sessions
        )

        self.assertIs(report, paused)
        self.assertIs(sessions, paused_sessions)

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
            expanded=True,
        )

        self.assertIn("2K tok", text)
        self.assertNotIn("4K tok", text)

    def test_terminal_banner_shows_fast_block_before_slow_scans_finish(self) -> None:
        snapshot = LiveUsageSnapshot(
            UsageReport("session", status="unavailable", detail="loading"),
            UsageReport("session", status="unavailable", detail="loading"),
            UsageBlock(
                status="available",
                cost_microusd=2_550_000,
                burn_rate_microusd_per_hour=102_000_000,
                remaining_minutes=239,
            ),
        )

        text = render_usage_banner(snapshot, (), {}, 160, False)

        self.assertIn("$2.55 this block", text)
        self.assertIn("$102.00/hr", text)
        self.assertEqual(len(text.splitlines()), 1)

    def test_expanded_terminal_usage_caps_rows_and_reports_overflow(self) -> None:
        rows = tuple(
            sample(session_id=f"session-{index}", last_activity=f"{index:02d}")
            for index in range(20)
        )
        snapshot = LiveUsageSnapshot(
            UsageReport("session", samples=rows),
            UsageReport("session", samples=rows),
            UsageBlock(detail="no block"),
        )
        sessions = tuple(("claude-code", f"session-{index}") for index in range(20))

        text = render_usage_banner(
            snapshot,
            (),
            {},
            160,
            False,
            sessions,
            expanded=True,
            max_lines=6,
        )

        self.assertLessEqual(len(text.splitlines()), 6)
        self.assertIn("more sessions", text)
        self.assertIn("not a subscription bill", text)
        self.assertIn("Pricing", text)

    def test_focused_view_does_not_label_usage_as_all_roots(self) -> None:
        report = UsageReport("session", samples=(sample(),))
        snapshot = LiveUsageSnapshot(report, report, UsageBlock(detail="no block"))

        text = render(
            [],
            Path("project"),
            120,
            20,
            False,
            root_count=3,
            focused_root_label="project",
            usage_report=snapshot,
            usage_sessions=(("claude-code", "session-1"),),
            expanded_header=True,
        )

        self.assertIn("Tracked lifetime · shown root", text)
        self.assertNotIn("3 shown roots", text)

    def test_terminal_state_keeps_sessions_older_than_the_display_window(self) -> None:
        history = [
            {"agent": "codex", "session_id": "old"},
            *({"agent": "filesystem", "kind": "file"} for _ in range(600)),
        ]
        with (
            patch("side_dog.cli.events_path", return_value=Path("events.jsonl")),
            patch("side_dog.cli.read_new_events", return_value=(history, 7)),
            patch("side_dog.cli.snapshot", return_value={}),
            patch("side_dog.cli.root_is_missing", return_value=False),
            patch("side_dog.cli.load_git_state", return_value=None),
        ):
            state = initialize_watch_root(Path("project"), 0)

        self.assertEqual(len(state.records), 200)
        self.assertIn(("codex", "old"), state.usage_sessions)

    def test_panel_snapshot_contains_reduced_root_scoped_usage(self) -> None:
        monitor = Mock()
        report = UsageReport(
            "session", samples=(sample(agent="codex", session_id="visible"),)
        )
        monitor.snapshot = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(agent="codex", session_id="visible"),)),
            report,
            UsageBlock(detail="no active block"),
        )
        monitor.settings = {
            "session_refresh_seconds": 180,
            "block_refresh_seconds": 10,
        }
        monitor.tick.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("side_dog.panel.UsageMonitor", return_value=monitor),
                patch("side_dog.panel.pinned_folders", return_value=[]),
                patch("side_dog.panel.load_git_state", return_value={}),
            ):
                feed = PanelFeed([root], follow_worktrees=False, notify=False)
                feed.roots[0].usage_sessions.add(("codex", "visible"))
                try:
                    usage = feed.snapshot()["roots"][0]["usage"]
                finally:
                    feed.close()

        self.assertEqual(usage["status"], "available")
        self.assertEqual(usage["rows"][0]["agent"], "codex")
        self.assertNotIn("session_id", usage["rows"][0])
        monitor.close.assert_called_once()

    def test_panel_usage_keeps_sessions_older_than_the_display_window(self) -> None:
        monitor = Mock()
        report = UsageReport(
            "session", samples=(sample(agent="codex", session_id="old"),)
        )
        monitor.snapshot = LiveUsageSnapshot(
            UsageReport("session", samples=(sample(agent="codex", session_id="old"),)),
            report,
            UsageBlock(detail="no active block"),
        )
        monitor.settings = {
            "session_refresh_seconds": 180,
            "block_refresh_seconds": 10,
        }
        monitor.tick.return_value = False
        history = [
            {"agent": "codex", "session_id": "old"},
            *({"agent": "filesystem", "kind": "file"} for _ in range(600)),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("side_dog.panel.UsageMonitor", return_value=monitor),
                patch("side_dog.panel.pinned_folders", return_value=[]),
                patch("side_dog.panel.load_git_state", return_value={}),
                patch("side_dog.panel.read_new_events", return_value=(history, 1)),
            ):
                feed = PanelFeed([root], follow_worktrees=False, notify=False)
                try:
                    self.assertNotIn("old", str(tuple(feed.roots[0].records)))
                    usage = feed._wire_root(feed.roots[0])["usage"]
                finally:
                    feed.close()

        self.assertEqual(usage["rows"][0]["lifetime_tokens"], 2_000)


if __name__ == "__main__":
    unittest.main()
