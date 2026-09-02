from __future__ import annotations

import json
import os
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from side_dog.cli import (
    STATE_ENV,
    append_event,
    events_path,
    load_native_stream_position,
    read_new_events,
    save_native_stream_position,
)
from side_dog.integrations import (
    ACTIVITY_SCHEMA,
    AdapterHealth,
    AdapterHealthStatus,
    AgentIdentity,
    AgentStatus,
    NormalizedEvent,
    SessionKey,
    StreamCheckpoint,
    event_from_wire,
    event_to_wire,
    identity_from_wire,
    identity_to_wire,
    normalize_provider,
)


class SessionKeyTests(unittest.TestCase):
    def test_same_external_id_is_distinct_across_providers(self) -> None:
        codex = SessionKey("codex", "shared")
        claude = SessionKey("claude-code", "shared")

        self.assertNotEqual(codex, claude)
        self.assertEqual(str(codex), "codex:shared")
        self.assertEqual(SessionKey.from_wire(str(codex)), codex)
        self.assertEqual(len({codex, claude}), 2)

    def test_missing_provider_and_session_are_explicitly_unknown(self) -> None:
        self.assertEqual(SessionKey.from_wire(None), SessionKey("unknown", "unknown"))
        self.assertEqual(normalize_provider(None), "unknown")
        self.assertEqual(normalize_provider("claude"), "claude-code")

    def test_is_immutable(self) -> None:
        key = SessionKey("codex", "session")
        with self.assertRaises(FrozenInstanceError):
            key.provider = "pi"  # type: ignore[misc]


class AgentIdentityTests(unittest.TestCase):
    def test_current_identity_shape_round_trips_with_extra_context(self) -> None:
        wire = {
            "agent": "codex",
            "session_id": "session-1",
            "root": "/repo",
            "pane_id": "%4",
            "workspace_id": "workspace",
            "tab_id": "tab",
            "working_root": "/repo/pkg",
            "status": "working",
            "label": "Codex · side-dog",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "herdr_window_id": "window",
        }

        identity = identity_from_wire(wire)

        self.assertEqual(identity.key, SessionKey("codex", "session-1"))
        self.assertIs(identity.status, AgentStatus.WORKING)
        self.assertEqual(identity_to_wire(identity), wire)

    def test_qualified_key_supplies_missing_identity_fields(self) -> None:
        identity = AgentIdentity.from_wire(
            {"label": "Pi", "status": "idle"}, key="pi:review_53"
        )

        self.assertEqual(identity.agent, "pi")
        self.assertEqual(identity.session_id, "review_53")
        self.assertEqual(identity.key.to_wire(), "pi:review_53")

    def test_missing_attribution_does_not_default_to_claude(self) -> None:
        identity = AgentIdentity.from_wire({})

        self.assertEqual(identity.agent, "unknown")
        self.assertEqual(identity.session_id, "unknown")
        self.assertIs(identity.status, AgentStatus.UNKNOWN)
        self.assertEqual(identity.to_wire()["agent"], "unknown")

    def test_blocked_status_from_herdr_is_preserved(self) -> None:
        identity = AgentIdentity.from_wire(
            {"agent": "codex", "session_id": "one", "status": "blocked"}
        )

        self.assertIs(identity.status, AgentStatus.BLOCKED)
        self.assertEqual(identity.to_wire()["status"], "blocked")

    def test_extra_fields_cannot_mutate_the_frozen_identity(self) -> None:
        source = {"future_field": {"enabled": True, "rows": [1]}}
        identity = AgentIdentity.from_wire(source)
        source["future_field"]["enabled"] = False
        source["future_field"]["rows"].append(2)

        with self.assertRaises(TypeError):
            identity.extras["new"] = True  # type: ignore[index]
        with self.assertRaises(TypeError):
            identity.extras["future_field"]["enabled"] = False
        with self.assertRaises(AttributeError):
            identity.extras["future_field"]["rows"].append(2)
        self.assertEqual(
            identity.to_wire()["future_field"], {"enabled": True, "rows": [1]}
        )


class NormalizedEventTests(unittest.TestCase):
    def test_v1_record_preserves_current_fields_and_rejects_new_ones(self) -> None:
        wire = {
            "schema": ACTIVITY_SCHEMA,
            "timestamp": "2026-09-02T16:00:00.000+00:00",
            "epoch_ms": 1_788_364_800_000,
            "started_epoch_ms": 1_788_364_799_000,
            "agent": "opencode",
            "project": "/repo",
            "session_id": "shared",
            "turn_id": "turn-1",
            "model": "gpt-5",
            "effort": "high",
            "operation_id": "tool-1",
            "group_id": "tool-1",
            "source_event_id": "opencode:shared:tool-1",
            "kind": "file",
            "status": "success",
            "title": "Wrote file",
            "detail": "side_dog/cli.py",
            "lines_added": 4,
            "lines_removed": 1,
        }

        event = event_from_wire(wire)

        self.assertEqual(event.session_key, SessionKey("opencode", "shared"))
        self.assertEqual(event_to_wire(event), wire)
        with self.assertRaisesRegex(ValueError, "unapproved fields"):
            event_from_wire(
                {**wire, "provider_metadata": {"source": "sqlite", "rows": [2, 3]}}
            )

    def test_missing_event_attribution_is_unknown(self) -> None:
        event = NormalizedEvent.from_wire(
            {
                "schema": ACTIVITY_SCHEMA,
                "kind": "session",
                "title": "Turn started",
            }
        )

        self.assertEqual(event.agent, "unknown")
        self.assertIsNone(event.session_id)
        self.assertEqual(event.status, "unknown")
        self.assertEqual(event.to_wire()["agent"], "unknown")
        self.assertNotIn("session_id", event.to_wire())

    def test_rejects_a_different_wire_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported activity schema"):
            NormalizedEvent.from_wire({"schema": "side-dog-activity-v2"})


class IntegrationStateTests(unittest.TestCase):
    def test_stream_checkpoint_reads_legacy_path_shape(self) -> None:
        checkpoint = StreamCheckpoint.from_wire(
            {
                "agent": "pi",
                "session_id": "review_53",
                "transcript_path": "/tmp/session.jsonl",
                "position": 42,
                "inode": 7,
            }
        )

        self.assertEqual(checkpoint.session, SessionKey("pi", "review_53"))
        self.assertEqual(checkpoint.source, "/tmp/session.jsonl")
        self.assertEqual(checkpoint.position, 42)
        self.assertEqual(checkpoint.to_wire()["inode"], 7)

    def test_negative_checkpoint_position_is_clamped(self) -> None:
        checkpoint = StreamCheckpoint(SessionKey("codex", "one"), position=-2)

        self.assertEqual(checkpoint.position, 0)

    def test_adapter_health_has_typed_status_and_round_trips_extras(self) -> None:
        health = AdapterHealth.from_wire(
            {
                "adapter": "opencode",
                "status": "degraded",
                "detail": "database is busy",
                "checked_epoch_ms": 123,
                "retryable": True,
            }
        )

        self.assertEqual(health.adapter, "opencode")
        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)
        self.assertEqual(
            health.to_wire(),
            {
                "adapter": "opencode",
                "status": "degraded",
                "detail": "database is busy",
                "checked_epoch_ms": 123,
                "retryable": True,
            },
        )


class ProductionBoundaryTests(unittest.TestCase):
    def test_jsonl_boundary_marks_missing_agent_unknown_without_inventing_session(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            state = Path(directory) / "state"
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                append_event(
                    root,
                    {
                        "kind": "session",
                        "status": "success",
                        "title": "Observed activity",
                        "detail": "",
                    },
                )
                records, _ = read_new_events(events_path(root), 0)

            self.assertEqual(records[0]["agent"], "unknown")
            self.assertNotIn("session_id", records[0])

    def test_reader_scrubs_v1_extras_and_normalizes_legacy_missing_agent(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "schema": ACTIVITY_SCHEMA,
                        "kind": "file",
                        "status": "success",
                        "title": "Wrote file",
                        "detail": "example.py",
                        "future": {"kept": True},
                    }
                )
                + "\n"
            )

            records, _ = read_new_events(path, 0)

            self.assertEqual(records[0]["agent"], "unknown")
            self.assertNotIn("future", records[0])

    def test_reader_skips_overflowing_numbers_and_keeps_later_records(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                '{"schema":"side-dog-activity-v1","epoch_ms":1e309,'
                '"kind":"file","title":"Malformed"}\n'
                + json.dumps(
                    {
                        "schema": ACTIVITY_SCHEMA,
                        "epoch_ms": 123,
                        "kind": "file",
                        "status": "success",
                        "title": "Wrote file",
                        "detail": "example.py",
                    }
                )
                + "\n"
            )

            records, _ = read_new_events(path, 0)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["epoch_ms"], 123)
            self.assertEqual(records[0]["title"], "Wrote file")

    def test_reader_scrubs_deep_metadata_and_keeps_later_records(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            nested = {"value": True}
            for _ in range(40):
                nested = {"next": nested}
            path.write_text(
                json.dumps(
                    {
                        "schema": ACTIVITY_SCHEMA,
                        "epoch_ms": 1,
                        "kind": "file",
                        "title": "Writing file",
                        "future": nested,
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "schema": ACTIVITY_SCHEMA,
                        "epoch_ms": 123,
                        "kind": "file",
                        "status": "success",
                        "title": "Wrote file",
                        "detail": "example.py",
                    }
                )
                + "\n"
            )

            records, _ = read_new_events(path, 0)

            self.assertEqual(len(records), 2)
            self.assertEqual(
                [record["title"] for record in records],
                ["Writing file", "Wrote file"],
            )
            self.assertNotIn("future", records[0])

    def test_reader_preserves_legacy_backfill_milestones(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "schema": ACTIVITY_SCHEMA,
                            "epoch_ms": index,
                            "kind": "session",
                            "status": "success",
                            "title": title,
                            "detail": detail,
                        }
                    )
                    for index, (title, detail) in enumerate(
                        (
                            ("Transcript backfill complete", "1 event recovered"),
                            (
                                "Side Dog history backfill complete",
                                "2 activity events available",
                            ),
                        ),
                        start=1,
                    )
                )
                + "\n"
            )

            records, _ = read_new_events(path, 0)

            self.assertEqual(
                [(record["title"], record["detail"]) for record in records],
                [
                    ("Transcript backfill complete", "1 event recovered"),
                    (
                        "Side Dog history backfill complete",
                        "2 activity events available",
                    ),
                ],
            )

    def test_persisted_stream_positions_are_provider_qualified(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            state = Path(directory) / "state"
            transcript = Path(directory) / "shared.jsonl"
            transcript.touch()
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                save_native_stream_position(
                    root, "shared", transcript, 11, agent="codex"
                )
                save_native_stream_position(root, "shared", transcript, 22, agent="pi")

                self.assertEqual(
                    load_native_stream_position(
                        root, "shared", transcript, agent="codex"
                    ),
                    11,
                )
                self.assertEqual(
                    load_native_stream_position(root, "shared", transcript, agent="pi"),
                    22,
                )


if __name__ == "__main__":
    unittest.main()
