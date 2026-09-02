from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from side_dog.cli import (
    ANTIGRAVITY_HISTORY_CACHE,
    ANTIGRAVITY_LISTING_CACHE,
    ANTIGRAVITY_METADATA_CACHE,
    ANTIGRAVITY_SESSION_HEADERS,
    ANTIGRAVITY_WORKER_CACHE,
    NativeAgentStream,
    announce_native_history,
    events_path,
    herdr_identities_for_root,
    antigravity_session_header,
    antigravity_session_path,
    antigravity_sessions_roots,
    antigravity_workers,
    clear_session_path_cache,
    load_agent_identities,
    load_antigravity_session_identities,
    poll_native_agent_events,
)
from side_dog.doctor import antigravity_probe
from side_dog.model import agent_label, display_model, normalize_agent


class AntigravityModelTests(unittest.TestCase):
    def test_normalize_agent_and_label(self) -> None:
        self.assertEqual(normalize_agent("antigravity"), "antigravity")
        self.assertEqual(normalize_agent("antigravity-cli"), "antigravity")
        self.assertEqual(normalize_agent("AGY"), "antigravity")
        self.assertEqual(agent_label("antigravity"), "Antigravity")
        self.assertEqual(agent_label("agy"), "Antigravity")

    def test_display_model_strips_vendor_prefixes(self) -> None:
        self.assertEqual(display_model("gemini-2.5-pro"), "2.5-pro")
        self.assertEqual(display_model("antigravity-flash"), "flash")
        self.assertEqual(display_model("google.gemini-2.5-flash"), "2.5-flash")


class AntigravityProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_session_path_cache()
        ANTIGRAVITY_LISTING_CACHE.clear()
        ANTIGRAVITY_HISTORY_CACHE.clear()
        ANTIGRAVITY_SESSION_HEADERS.clear()
        ANTIGRAVITY_METADATA_CACHE.clear()
        ANTIGRAVITY_WORKER_CACHE.clear()

    def test_probe_detects_configured_home_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory) / "custom-agy"
            brain = app_dir / "brain"
            brain.mkdir(parents=True)
            result = antigravity_probe({"ANTIGRAVITY_APP_DATA_DIR": str(app_dir)})
            self.assertEqual(result.status, "ok")
            self.assertIn("Native local session discovery is ready", result.detail)

    def test_probe_reports_info_when_unconfigured_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("pathlib.Path.home", return_value=Path(directory)):
                result = antigravity_probe({})
                self.assertEqual(result.status, "info")
                self.assertIn(
                    "No local Antigravity session directory yet", result.detail
                )


class AntigravityDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_session_path_cache()
        ANTIGRAVITY_LISTING_CACHE.clear()
        ANTIGRAVITY_HISTORY_CACHE.clear()
        ANTIGRAVITY_SESSION_HEADERS.clear()
        ANTIGRAVITY_METADATA_CACHE.clear()
        ANTIGRAVITY_WORKER_CACHE.clear()

    def test_session_roots_and_path_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory) / "antigravity-cli"
            session_id = "11111111-2222-3333-4444-555555555555"
            log_dir = app_dir / "brain" / session_id / ".system_generated" / "logs"
            log_dir.mkdir(parents=True)
            transcript = log_dir / "transcript.jsonl"
            transcript.write_text(
                json.dumps({"type": "USER_INPUT", "content": "Fix bug"}) + "\n"
            )

            with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_dir)}):
                roots = antigravity_sessions_roots()
                self.assertEqual(roots, [app_dir])
                resolved = antigravity_session_path(session_id)
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved, transcript)

    def test_session_header_and_identities_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = (Path(directory) / "my-repo").resolve()
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
            )

            app_dir = (Path(directory) / "antigravity-cli").resolve()
            session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            log_dir = app_dir / "brain" / session_id / ".system_generated" / "logs"
            log_dir.mkdir(parents=True)
            transcript = log_dir / "transcript.jsonl"
            (app_dir / "history.jsonl").write_text(
                json.dumps(
                    {
                        "conversationId": session_id,
                        "display": "Add feature X",
                        "timestamp": 1_788_350_000_000,
                        "workspace": str(repo),
                    }
                )
                + "\n"
            )

            lines = [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "content": "Add feature X\nDetails here",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "model": "gemini-2.5-pro",
                    "effort": "high",
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {"CommandLine": "git status", "Cwd": str(repo)},
                        },
                        {
                            "name": "invoke_subagent",
                            "args": {
                                "Subagents": [
                                    {
                                        "TypeName": "research",
                                        "Role": "Codebase Researcher",
                                    }
                                ]
                            },
                        },
                    ],
                },
            ]
            transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

            with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_dir)}):
                header = antigravity_session_header(transcript)
                self.assertEqual(header["session_id"], session_id)
                self.assertEqual(header["cwd"], str(repo))
                self.assertEqual(header["model"], "gemini-2.5-pro")
                self.assertEqual(header["effort"], "high")
                self.assertEqual(header["subagents"], ["Codebase Researcher"])

                now = transcript.stat().st_mtime
                identities = load_antigravity_session_identities(repo, now=now)
                self.assertIn(session_id, identities)
                identity = identities[session_id]
                self.assertEqual(identity["agent"], "antigravity")
                self.assertEqual(identity["status"], "working")
                self.assertEqual(identity["model"], "gemini-2.5-pro")
                self.assertEqual(identity["effort"], "high")

                workers = antigravity_workers(repo, now=now)
                self.assertEqual(workers, ["Codebase Researcher"])

                all_identities = load_agent_identities(repo, now=now)
                self.assertIn(session_id, all_identities)

                herdr = herdr_identities_for_root(
                    repo,
                    [
                        {
                            "agent": "agy",
                            "cwd": str(repo),
                            "agent_status": "working",
                            "agent_session": {"value": session_id},
                        }
                    ],
                )
                self.assertEqual(herdr[session_id]["agent"], "antigravity")


class AntigravityStreamingTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_session_path_cache()
        ANTIGRAVITY_LISTING_CACHE.clear()
        ANTIGRAVITY_HISTORY_CACHE.clear()
        ANTIGRAVITY_SESSION_HEADERS.clear()
        ANTIGRAVITY_METADATA_CACHE.clear()
        ANTIGRAVITY_WORKER_CACHE.clear()

    def test_poll_antigravity_events_and_diffs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = (Path(directory) / "test-repo").resolve()
            repo.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "tester@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tester"],
                cwd=repo,
                check=True,
            )

            file_path = repo / "hello.py"
            file_path.write_text("print('hello')\n")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"], cwd=repo, check=True
            )

            app_dir = (Path(directory) / "antigravity-cli").resolve()
            session_id = "12345678-1234-1234-1234-123456789abc"
            log_dir = app_dir / "brain" / session_id / ".system_generated" / "logs"
            log_dir.mkdir(parents=True)
            transcript = log_dir / "transcript.jsonl"
            state_dir = Path(directory) / "state"
            (app_dir / "history.jsonl").write_text(
                json.dumps(
                    {
                        "conversationId": session_id,
                        "display": "Improve greeting",
                        "timestamp": 1_788_350_000_000,
                        "workspace": str(repo),
                    }
                )
                + "\n"
            )

            file_path.write_text("print('hello world')\nprint('second line')\n")

            records = [
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "content": "Improve greeting with private-token-123",
                    "created_at": "2026-09-02T12:00:00Z",
                },
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-09-02T12:00:05Z",
                    "tool_calls": [
                        {
                            "name": "replace_file_content",
                            "args": {"TargetFile": str(file_path)},
                        }
                    ],
                },
                {
                    "step_index": 2,
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-09-02T12:00:06Z",
                    "content": "The file was updated.",
                },
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-09-02T12:00:07Z",
                    "tool_calls": [
                        {
                            "name": "run_command",
                            "args": {
                                "CommandLine": "python -m unittest",
                                "Cwd": str(repo),
                            },
                        }
                    ],
                },
                {
                    "step_index": 4,
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-09-02T12:00:08Z",
                    "content": "The command exited with code 1.",
                },
                {
                    "step_index": 5,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-09-02T12:00:09Z",
                    "tool_calls": [
                        {
                            "name": "invoke_subagent",
                            "args": {"Role": "Test Runner"},
                        }
                    ],
                },
                {
                    "step_index": 6,
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-09-02T12:00:10Z",
                    "content": "Subagent finished.",
                },
            ]
            transcript.write_text(
                "".join(json.dumps(record) + "\n" for record in records)
            )

            environment = {
                "ANTIGRAVITY_APP_DATA_DIR": str(app_dir),
                "SIDE_DOG_STATE_DIR": str(state_dir),
            }
            with patch.dict(os.environ, environment):
                identities = load_antigravity_session_identities(repo)
                streams: dict[str, NativeAgentStream] = {}
                count = poll_native_agent_events(repo, identities, streams)
                self.assertGreater(count, 0)
                self.assertIn(session_id, streams)
                self.assertEqual(streams[session_id].agent, "antigravity")
                events = [
                    json.loads(line)
                    for line in events_path(repo).read_text().splitlines()
                ]
                self.assertTrue(
                    any(
                        event.get("kind") == "file" and event.get("status") == "success"
                        for event in events
                    )
                )
                self.assertTrue(
                    any(
                        event.get("kind") == "test" and event.get("status") == "failed"
                        for event in events
                    )
                )
                self.assertTrue(
                    any(event.get("title") == "Subagent completed" for event in events)
                )
                self.assertNotIn("private-token-123", events_path(repo).read_text())

                self.assertEqual(poll_native_agent_events(repo, identities, streams), 0)
                resumed_streams: dict[str, NativeAgentStream] = {}
                self.assertEqual(
                    poll_native_agent_events(repo, identities, resumed_streams), 0
                )

    def test_restart_replays_a_tool_call_until_its_result_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            repo.mkdir()
            app_dir = root / "antigravity-cli"
            state_dir = root / "state"
            session_id = "87654321-4321-4321-4321-cba987654321"
            log_dir = app_dir / "brain" / session_id / ".system_generated" / "logs"
            log_dir.mkdir(parents=True)
            transcript = log_dir / "transcript.jsonl"
            request = {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "tool_calls": [
                    {
                        "name": "run_command",
                        "args": {
                            "CommandLine": "python -m unittest",
                            "Cwd": str(repo),
                        },
                    }
                ],
            }
            transcript.write_text(json.dumps(request) + "\n")
            identity = {
                session_id: {
                    "agent": "antigravity",
                    "session_id": session_id,
                    "root": str(repo),
                }
            }
            environment = {
                "ANTIGRAVITY_APP_DATA_DIR": str(app_dir),
                "SIDE_DOG_STATE_DIR": str(state_dir),
            }
            with patch.dict(os.environ, environment):
                first_streams: dict[str, NativeAgentStream] = {}
                self.assertEqual(
                    poll_native_agent_events(repo, identity, first_streams), 1
                )

                with transcript.open("a") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "step_index": 2,
                                "type": "GENERIC",
                                "status": "DONE",
                                "content": "The command exited with code 1.",
                            }
                        )
                        + "\n"
                    )

                resumed_streams: dict[str, NativeAgentStream] = {}
                self.assertEqual(
                    poll_native_agent_events(repo, identity, resumed_streams), 1
                )
                statuses = [
                    json.loads(line).get("status")
                    for line in events_path(repo).read_text().splitlines()
                    if json.loads(line).get("kind") == "test"
                ]
                self.assertEqual(statuses, ["running", "failed"])

                final_streams: dict[str, NativeAgentStream] = {}
                self.assertEqual(
                    poll_native_agent_events(repo, identity, final_streams), 0
                )

    def test_announce_native_history_antigravity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = (Path(directory) / "test-repo").resolve()
            repo.mkdir()
            stream = NativeAgentStream(
                session_id="12345678-1234-1234-1234-123456789abc",
                path=repo / "transcript.jsonl",
                position=0,
                agent="antigravity",
            )
            with patch("side_dog.cli.native_event_count", return_value=5):
                with patch("side_dog.cli.append_event_once") as append_mock:
                    announce_native_history(repo, stream, initial_position=0)
                    append_mock.assert_called_once()
                    args = append_mock.call_args[0][1]
                    self.assertIn(
                        "antigravity:12345678-1234-1234-1234-123456789abc",
                        args["source_event_id"],
                    )
                    self.assertEqual(args["agent"], "antigravity")


if __name__ == "__main__":
    unittest.main()
