import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    NativeAgentStream,
    STATE_ENV,
    codex_session_path,
    events_path,
    hook,
    latest_events,
    poll_native_agent_events,
)
from side_dog.panel import PanelFeed


class NativeAgentEventsTest(TestCase):
    def test_codex_session_discovery_honors_codex_home(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "configured-codex"
            sessions = codex_home / "sessions" / "2026" / "08" / "31"
            sessions.mkdir(parents=True)
            session_id = "11111111-2222-3333-4444-555555555555"
            expected = sessions / f"rollout-{session_id}.jsonl"
            expected.write_text("")
            codex_session_path.cache_clear()
            try:
                with patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}):
                    self.assertEqual(codex_session_path(session_id), expected)
            finally:
                codex_session_path.cache_clear()

    def test_codex_native_command_completion_reports_tests_without_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            root = root.resolve()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            records = [
                {
                    "timestamp": "2026-08-31T20:00:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": "call-1",
                        "input": (
                            'const r = await tools.exec_command({cmd:'
                            '"python -m unittest discover -s tests -v",workdir:'
                            f'"{root}"}}); text(r.output);'
                        ),
                    },
                },
                {
                    "timestamp": "2026-08-31T20:00:01.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "turn_id": "turn-1",
                        "started_at_ms": 1_788_220_000_000,
                        "completed_at_ms": 1_788_220_001_000,
                        "item": {
                            "type": "CommandExecution",
                            "id": "exec-1",
                            "command": [
                                "/bin/zsh",
                                "-lc",
                                "python -m unittest discover -s tests -v",
                            ],
                            "cwd": root.as_uri(),
                            "status": "completed",
                            "exit_code": 0,
                            "stdout": "SECRET TEST OUTPUT",
                            "stderr": "SECRET STDERR",
                        },
                    },
                },
            ]
            session.write_text("".join(json.dumps(record) + "\n" for record in records))
            stream = NativeAgentStream(
                session_id=session_id,
                path=session,
                position=0,
                model="gpt-test",
                effort="high",
            )
            identities = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                    "model": "gpt-test",
                    "effort": "high",
                }
            }
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                count = poll_native_agent_events(
                    root, identities, {session_id: stream}
                )
                events = latest_events(events_path(root))

            self.assertEqual(count, 2)
            self.assertEqual(
                [event["status"] for event in events], ["running", "success"]
            )
            self.assertEqual(events[0]["operation_id"], events[1]["operation_id"])
            self.assertEqual(events[-1]["title"], "Tests passed")
            self.assertEqual(events[-1]["agent"], "codex")
            self.assertEqual(events[-1]["model"], "gpt-test")
            self.assertEqual(events[-1]["effort"], "high")
            serialized = json.dumps(events)
            self.assertNotIn("SECRET TEST OUTPUT", serialized)
            self.assertNotIn("SECRET STDERR", serialized)
            self.assertNotIn("discover -s tests", serialized)

    def test_codex_native_file_change_reports_paths_without_diffs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            root = root.resolve()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            session.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-08-31T20:00:00.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "FileChange",
                                "id": "patch-1",
                                "status": "completed",
                                "changes": {
                                    os.fspath(root / "side_dog" / "cli.py"): {
                                        "type": "update",
                                        "unified_diff": "SECRET PATCH CONTENT",
                                    }
                                },
                            },
                        },
                    }
                )
                + "\n"
            )
            stream = NativeAgentStream(session_id, session, 0)
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                }
            }
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                poll_native_agent_events(root, identity, {session_id: stream})
                events = latest_events(events_path(root))

            self.assertEqual(events[-1]["title"], "Wrote file")
            self.assertEqual(events[-1]["detail"], "side_dog/cli.py")
            self.assertNotIn("SECRET PATCH CONTENT", json.dumps(events))

    def test_custom_tool_output_completes_pending_command_without_storing_output(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            root = root.resolve()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            request = {
                "timestamp": "2026-08-31T20:00:00.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call-output",
                    "input": {
                        "cmd": "python -m unittest",
                        "workdir": os.fspath(root),
                    },
                },
            }
            output = {
                "timestamp": "2026-08-31T20:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-output",
                    "output": json.dumps(
                        {"exit_code": 0, "output": "SECRET COMMAND OUTPUT"}
                    ),
                },
            }
            session.write_text(json.dumps(request) + "\n" + json.dumps(output) + "\n")
            stream = NativeAgentStream(session_id, session, 0)
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                }
            }
            streams = {session_id: stream}
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                self.assertEqual(poll_native_agent_events(root, identity, streams), 2)
                events = latest_events(events_path(root))
                self.assertEqual(events[-1]["title"], "Tests passed")
                self.assertNotIn("SECRET COMMAND OUTPUT", json.dumps(events))

                with session.open("a") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "timestamp": "2026-08-31T20:00:02.000Z",
                                "type": "event_msg",
                                "payload": {
                                    "type": "item_completed",
                                    "item": {
                                        "type": "CommandExecution",
                                        "id": "exec-output",
                                        "command": ["python", "-m", "unittest"],
                                        "cwd": root.as_uri(),
                                        "status": "completed",
                                        "exit_code": 0,
                                    },
                                },
                            }
                        )
                        + "\n"
                    )
                self.assertEqual(poll_native_agent_events(root, identity, streams), 0)
                self.assertEqual(len(latest_events(events_path(root))), 2)

    def test_native_source_ids_deduplicate_two_live_views(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            root = root.resolve()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            session.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-08-31T20:00:00.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "CommandExecution",
                                "id": "exec-1",
                                "command": ["python", "-m", "unittest"],
                                "cwd": root.as_uri(),
                                "status": "completed",
                                "exit_code": 0,
                            },
                        },
                    }
                )
                + "\n"
            )
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                }
            }
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                first = poll_native_agent_events(
                    root, identity, {session_id: NativeAgentStream(session_id, session, 0)}
                )
                second = poll_native_agent_events(
                    root, identity, {session_id: NativeAgentStream(session_id, session, 0)}
                )
                events = latest_events(events_path(root))

            self.assertEqual((first, second), (1, 0))
            self.assertEqual(len(events), 1)

    def test_incomplete_utf8_jsonl_record_is_retried_on_the_next_poll(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            root = root.resolve()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            record = {
                "timestamp": "2026-08-31T20:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-partial",
                        "command": ["python", "-m", "unittest"],
                        "cwd": root.as_uri(),
                        "status": "completed",
                        "exit_code": 0,
                        "stdout": "café",
                    },
                },
            }
            encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode()
            split = encoded.index("é".encode()) + 1
            session.write_bytes(encoded[:split])
            stream = NativeAgentStream(session_id, session, 0)
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                }
            }
            streams = {session_id: stream}
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                self.assertEqual(poll_native_agent_events(root, identity, streams), 0)
                self.assertEqual(stream.position, 0)
                with session.open("ab") as handle:
                    handle.write(encoded[split:])
                self.assertEqual(poll_native_agent_events(root, identity, streams), 1)
                events = latest_events(events_path(root))

            self.assertEqual(events[-1]["title"], "Tests passed")
            self.assertNotIn("café", json.dumps(events))

    def test_claude_native_hook_cannot_be_misattributed_to_codex(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            root = root.resolve()
            state = Path(directory) / "state"
            payload = {
                "agent": "codex",
                "session_id": "claude-session",
                "cwd": os.fspath(root),
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_use_id": "tool-1",
                "tool_input": {"command": "python -m unittest"},
            }
            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(state)}),
                patch("sys.stdin", io.StringIO(json.dumps(payload))),
            ):
                self.assertEqual(hook(), 0)
                events = latest_events(events_path(root))

            self.assertEqual(events[-1]["agent"], "claude-code")
            self.assertEqual(events[-1]["title"], "Tests passed")

    def test_browser_feed_collects_codex_native_events_without_terminal_watch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            root = root.resolve()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session.write_text("")
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                    "working_root": os.fspath(root),
                    "pane_id": "w1:p1",
                    "label": "Codex",
                    "status": "working",
                }
            }
            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(state)}),
                patch("side_dog.cli.codex_session_path", return_value=session),
                patch("side_dog.panel.load_git_state", return_value={}),
                patch("side_dog.panel.load_github_pr", return_value=(None, None)),
                patch("side_dog.panel.load_herdr_identities", return_value=identity),
                patch("side_dog.panel._github_web_root", return_value=""),
            ):
                feed = PanelFeed([root])
                try:
                    feed.roots[0].identities = identity
                    feed.poll()  # Attach at EOF; prior transcript is not replayed.
                    with session.open("a") as handle:
                        handle.write(
                            json.dumps(
                                {
                                    "timestamp": "2026-08-31T20:00:00.000Z",
                                    "type": "event_msg",
                                    "payload": {
                                        "type": "item_completed",
                                        "item": {
                                            "type": "CommandExecution",
                                            "id": "exec-panel",
                                            "command": ["python", "-m", "unittest"],
                                            "cwd": root.as_uri(),
                                            "status": "completed",
                                            "exit_code": 0,
                                        },
                                    },
                                }
                            )
                            + "\n"
                        )
                    updates = feed.poll()
                finally:
                    feed.close()

            units = [value for event, value in updates if event == "unit"]
            self.assertEqual(units[-1]["events"][-1]["title"], "Tests passed")
            self.assertEqual(units[-1]["events"][-1]["agent"], "codex")
