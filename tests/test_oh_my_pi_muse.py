from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog import cli as side_dog_cli
from side_dog.cli import (
    NativeAgentStream,
    STATE_ENV,
    clear_session_path_cache,
    events_path,
    latest_events,
    load_agent_identities,
    load_muse_session_identities,
    load_oh_my_pi_session_identities,
    poll_native_agent_events,
)


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


class OhMyPiAndMuseIntegrationTest(TestCase):
    def setUp(self) -> None:
        clear_session_path_cache()
        side_dog_cli.OH_MY_PI_SESSION_HEADERS.clear()
        side_dog_cli.OH_MY_PI_LISTING_CACHE.clear()
        side_dog_cli.OH_MY_PI_METADATA_CACHE.clear()
        side_dog_cli.MUSE_SESSION_SUMMARIES.clear()
        side_dog_cli.MUSE_LISTING_CACHE.clear()

    tearDown = setUp

    def test_oh_my_pi_title_slot_and_safe_activity(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            agent_dir = Path(directory) / "omp-agent"
            session_id = "1f9d2a6b9c0d1234"
            path = agent_dir / "sessions" / "-project" / f"run_{session_id}.jsonl"
            write_jsonl(path, [
                {"type": "title", "title": "private task"},
                {"type": "session", "id": session_id, "cwd": os.fspath(root)},
                {"type": "model_change", "model": "openai/gpt-6"},
                {"type": "thinking_level_change", "thinkingLevel": "high"},
                {"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "call-1", "name": "bash", "arguments": {"command": "pytest -q secret"}}]}},
                {"type": "message", "message": {"role": "toolResult", "toolCallId": "call-1", "isError": False}},
            ])
            with patch.dict(os.environ, {"OMP_AGENT_DIR": os.fspath(agent_dir)}):
                identities = load_oh_my_pi_session_identities(root)
                self.assertEqual(identities[session_id]["model"], "openai/gpt-6")
                state = Path(directory) / "state"
                with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                    poll_native_agent_events(root, identities, {"oh-my-pi:" + session_id: NativeAgentStream(session_id, path, 0, agent="oh-my-pi", agent_root=os.fspath(root), session_cwd=os.fspath(root))})
                    events = latest_events(events_path(root))
            self.assertTrue(any(event["title"] == "Tests passed" for event in events))
            self.assertNotIn("private task", json.dumps(events))
            self.assertNotIn("pytest -q secret", json.dumps(events))

    def test_muse_projects_safe_tool_lifecycle(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data_dir = Path(directory) / "muse"
            session_id = "01a08cb1-8479-7af3-80ea-06f07cea6736"
            path = data_dir / "sessions" / "2026" / "09" / "10" / session_id / "session.jsonl"
            def record(kind: str, body: dict[str, object], event_id: str) -> dict[str, object]:
                return {"id": event_id, "recorded_at": "2026-09-10T12:00:00.000Z", "stream": {"kind": "session", "id": session_id}, "payload_type": kind, "payload": {"record": body}}
            write_jsonl(path, [
                record("runtime.session", {"root_session_id": session_id}, "one"),
                record("runtime.session.metadata", {"workspace_root": os.fspath(root), "model_id": "muse-spark"}, "two"),
                record("runtime.user_intent.accepted", {"prompt": "secret"}, "three"),
                record("tool_batch.effect.started", {"effect_id": "effect-1", "tool_name": "bash", "raw_command": "pytest secret"}, "four"),
                record("tool_batch.effect.terminal", {"effect_id": "effect-1", "tool_name": "bash", "outcome": {"kind": "completed", "output": "secret"}}, "five"),
            ])
            with patch.dict(os.environ, {"MUSE_DATA_DIR": os.fspath(data_dir)}):
                identities = load_muse_session_identities(root)
                self.assertEqual(identities[session_id]["model"], "muse-spark")
                state = Path(directory) / "state"
                with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                    poll_native_agent_events(root, identities, {"muse:" + session_id: NativeAgentStream(session_id, path, 0, agent="muse", agent_root=os.fspath(root), session_cwd=os.fspath(root))})
                    events = latest_events(events_path(root))
            self.assertTrue(any(event["title"] == "Muse command started" for event in events))
            self.assertTrue(any(event["title"] == "Muse command finished" for event in events))
            self.assertNotIn("pytest secret", json.dumps(events))

    def test_muse_discovers_persistent_subagent_sessions(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data_dir = Path(directory) / "muse"
            parent_id = "01a08cb1-8479-7af3-80ea-06f07cea6736"
            subagent_id = "01a08cb1-8479-7af3-80ea-06f07cea6737"
            path = (
                data_dir
                / "sessions"
                / "2026"
                / "09"
                / "10"
                / parent_id
                / "subagent"
                / subagent_id
                / "session.jsonl"
            )
            write_jsonl(
                path,
                [
                    {
                        "id": "metadata",
                        "stream": {"kind": "session", "id": parent_id},
                        "payload_type": "runtime.session.metadata",
                        "payload": {
                            "record": {
                                "workspace_root": os.fspath(root),
                                "model_id": "muse-spark",
                            }
                        },
                    }
                ],
            )
            with patch.dict(os.environ, {"MUSE_DATA_DIR": os.fspath(data_dir)}):
                identities = load_muse_session_identities(root)

            self.assertIn(subagent_id, identities)
            self.assertEqual(
                identities[subagent_id]["label"], f"Muse subagent · {root.name}"
            )

    def test_pane_only_herdr_entries_join_one_native_session(self) -> None:
        root = Path("/work/project")
        fixtures = {
            "oh-my-pi": (
                "omp-session",
                "load_oh_my_pi_session_identities",
                "Oh My Pi",
            ),
            "muse": ("muse-session", "load_muse_session_identities", "Muse"),
        }
        for provider, (session_id, loader, label) in fixtures.items():
            with self.subTest(provider=provider):
                native = {
                    session_id: {
                        "agent": provider,
                        "session_id": session_id,
                        "root": os.fspath(root),
                        "working_root": os.fspath(root),
                        "status": "working",
                        "label": label,
                    }
                }
                herdr = {
                    "pane:w1:p1": {
                        "agent": provider,
                        "pane_id": "w1:p1",
                        "workspace_id": "w1",
                        "tab_id": "t1",
                        "root": os.fspath(root),
                        "working_root": os.fspath(root),
                        "status": "idle",
                        "label": "w1:p1",
                    }
                }
                with (
                    patch("side_dog.cli.load_herdr_identities", return_value=herdr),
                    patch(f"side_dog.cli.{loader}", return_value=native),
                ):
                    identities = load_agent_identities(root, now=0)

                self.assertEqual(list(identities), [f"{provider}:{session_id}"])
                self.assertEqual(
                    identities[f"{provider}:{session_id}"]["pane_id"], "w1:p1"
                )
                self.assertEqual(identities[f"{provider}:{session_id}"]["label"], label)

    def test_pane_only_herdr_entry_stays_separate_when_ambiguous(self) -> None:
        root = Path("/work/project")
        native = {
            session_id: {
                "agent": "oh-my-pi",
                "session_id": session_id,
                "root": os.fspath(root),
                "working_root": os.fspath(root),
                "status": "working",
                "label": "Oh My Pi",
            }
            for session_id in ("omp-one", "omp-two")
        }
        herdr = {
            "pane:w1:p1": {
                "agent": "oh-my-pi",
                "pane_id": "w1:p1",
                "root": os.fspath(root),
                "working_root": os.fspath(root),
                "status": "idle",
                "label": "w1:p1",
            }
        }
        with (
            patch("side_dog.cli.load_herdr_identities", return_value=herdr),
            patch("side_dog.cli.load_oh_my_pi_session_identities", return_value=native),
        ):
            identities = load_agent_identities(root, now=0)

        self.assertEqual(
            set(identities),
            {"oh-my-pi:omp-one", "oh-my-pi:omp-two", "pane:w1:p1"},
        )
