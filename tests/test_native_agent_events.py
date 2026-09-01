import io
import json
import os
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    CODEX_METADATA_CACHE,
    CODEX_SESSION_HEADERS,
    CODEX_SESSION_IDENTITY_WINDOW_SECONDS,
    NativeAgentStream,
    STATE_ENV,
    active_agent_identities,
    announce_native_history,
    append_event_once,
    clear_session_path_cache,
    codex_session_path,
    events_path,
    git_repository_location,
    hook,
    latest_events,
    active_agent_identities,
    herdr_identities_for_root,
    load_agent_identities,
    load_codex_session_identities,
    load_pi_session_identities,
    poll_native_agent_events,
)
from side_dog import cli as side_dog_cli
from side_dog.model import MILESTONE_KINDS
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
            clear_session_path_cache()
            try:
                with patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}):
                    self.assertEqual(codex_session_path(session_id), expected)
            finally:
                clear_session_path_cache()

    def test_codex_session_discovery_retries_until_the_rollout_lands(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "configured-codex"
            sessions = codex_home / "sessions" / "2026" / "09" / "01"
            sessions.mkdir(parents=True)
            session_id = "22222222-3333-4444-5555-666666666666"
            expected = sessions / f"rollout-{session_id}.jsonl"
            clear_session_path_cache()
            try:
                with (
                    patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}),
                    patch("side_dog.cli.SESSION_PATH_RETRY_SECONDS", 0.0),
                ):
                    self.assertIsNone(codex_session_path(session_id))
                    expected.write_text("")
                    self.assertEqual(codex_session_path(session_id), expected)
            finally:
                clear_session_path_cache()

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

    def test_codex_native_exec_accepts_quoted_tool_argument_keys(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            call_id = "call-quoted-keys"
            arguments = json.dumps(
                {
                    "cmd": "gh issue close 12 --comment 'resolved'",
                    "workdir": os.fspath(root),
                }
            )
            records = [
                {
                    "timestamp": "2026-09-01T12:18:00.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "call_id": call_id,
                        "input": (
                            f"const r = await tools.exec_command({arguments}); "
                            "text(r.output)\n"
                        ),
                    },
                },
                {
                    "timestamp": "2026-09-01T12:18:01.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"exit_code": 0, "output": "closed"}),
                    },
                },
            ]
            session.write_text("".join(json.dumps(record) + "\n" for record in records))
            stream = NativeAgentStream(session_id, session, 0)
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                }
            }

            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                count = poll_native_agent_events(
                    root, identity, {session_id: stream}
                )
                events = latest_events(events_path(root))

            self.assertEqual(count, 2)
            self.assertEqual(
                [event["title"] for event in events],
                ["Closing issue", "Closed issue"],
            )
            self.assertEqual([event["detail"] for event in events], ["issue #12"] * 2)

    def test_new_stream_backfills_and_resumes_from_persisted_cursor(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            first_record = {
                "timestamp": "2026-09-01T12:18:06.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-before-panel",
                        "command": ["gh", "issue", "close", "12"],
                        "cwd": root.as_uri(),
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
            }
            second_record = {
                "timestamp": "2026-09-01T12:25:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": "exec-after-panel",
                        "command": ["python", "-m", "unittest"],
                        "cwd": root.as_uri(),
                        "status": "completed",
                        "exit_code": 0,
                    },
                },
            }
            session.write_text(json.dumps(first_record) + "\n")
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                }
            }

            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(state)}),
                patch("side_dog.cli.codex_session_path", return_value=session),
            ):
                first_streams: dict[str, NativeAgentStream] = {}
                first_count = poll_native_agent_events(
                    root, identity, first_streams
                )
                with session.open("a") as handle:
                    handle.write(json.dumps(second_record) + "\n")
                resumed_streams: dict[str, NativeAgentStream] = {}
                resumed_count = poll_native_agent_events(
                    root, identity, resumed_streams
                )
                final_count = poll_native_agent_events(root, identity, {})
                events = latest_events(events_path(root))

            self.assertEqual((first_count, resumed_count, final_count), (1, 1, 0))
            self.assertEqual(
                [event["title"] for event in events],
                [
                    "Closed issue",
                    "Side Dog caught up on earlier activity",
                    "Tests passed",
                ],
            )
            self.assertEqual(events[1]["detail"], "1 earlier event added")

    def test_existing_native_index_gets_one_visible_resume_milestone(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            stream = NativeAgentStream(
                session_id,
                Path(directory) / "codex.jsonl",
                123,
                agent_root=os.fspath(root),
            )
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                append_event_once(
                    root,
                    {
                        "kind": "test",
                        "title": "Tests passed",
                        "source_event_id": f"codex:{session_id}:item:test:complete",
                    },
                )
                announce_native_history(root, stream, 123)
                announce_native_history(root, stream, 123)
                events = latest_events(events_path(root))

            self.assertEqual(
                [event["title"] for event in events],
                ["Tests passed", "Side Dog caught up on earlier activity"],
            )
            self.assertEqual(events[-1]["detail"], "1 earlier event already saved")

    def test_codex_subagent_lifecycle_is_reported(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            session = Path(directory) / "codex.jsonl"
            session_id = "01a05846-8d69-7163-86e4-87f3ffd6b084"
            records = []
            for index, lifecycle in enumerate(
                ("started", "interacted", "completed"), start=1
            ):
                records.append(
                    {
                        "timestamp": f"2026-09-01T12:18:0{index}.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "item": {
                                "type": "SubAgentActivity",
                                "id": f"subagent-{index}",
                                "agent_path": "/root/issue_12_root_colors",
                                "agent_thread_id": "thread-subagent",
                                "kind": lifecycle,
                            },
                        },
                    }
                )
            session.write_text("".join(json.dumps(record) + "\n" for record in records))
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(root),
                }
            }

            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                count = poll_native_agent_events(
                    root,
                    identity,
                    {session_id: NativeAgentStream(session_id, session, 0)},
                )
                events = latest_events(events_path(root))

            self.assertEqual(count, 3)
            self.assertEqual(
                [event["title"] for event in events],
                ["Subagent started", "Subagent active", "Subagent completed"],
            )
            self.assertEqual(
                [event["status"] for event in events],
                ["running", "running", "success"],
            )
            self.assertEqual(
                {event["detail"] for event in events}, {"issue_12_root_colors"}
            )
            self.assertIn("session", MILESTONE_KINDS)

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
                native_index = events_path(root).parent / "native-events.sqlite3"

            self.assertEqual((first, second), (1, 0))
            self.assertEqual(len(events), 1)
            self.assertTrue(native_index.exists())

    def test_codex_command_is_routed_to_its_reported_worktree(self) -> None:
        with TemporaryDirectory() as directory:
            primary = (Path(directory) / "primary").resolve()
            sibling = (Path(directory) / "sibling").resolve()
            primary.mkdir()
            sibling.mkdir()
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
                        "call_id": "call-sibling",
                        "input": {
                            "cmd": "python -m unittest",
                            "workdir": os.fspath(sibling),
                        },
                    },
                },
                {
                    "timestamp": "2026-08-31T20:00:01.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "call-sibling",
                        "output": json.dumps({"exit_code": 0}),
                    },
                },
            ]
            session.write_text("".join(json.dumps(record) + "\n" for record in records))
            identity = {
                session_id: {
                    "session_id": session_id,
                    "agent": "codex",
                    "root": os.fspath(primary),
                }
            }

            def worktree_for(path: str) -> str:
                resolved = Path(path).resolve(strict=False)
                if resolved == sibling or sibling in resolved.parents:
                    return os.fspath(sibling)
                return os.fspath(primary)

            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(state)}),
                patch(
                    "side_dog.cli.git_worktree_root", side_effect=worktree_for
                ),
            ):
                primary_count = poll_native_agent_events(
                    primary,
                    identity,
                    {session_id: NativeAgentStream(session_id, session, 0)},
                )
                sibling_count = poll_native_agent_events(
                    sibling,
                    identity,
                    {session_id: NativeAgentStream(session_id, session, 0)},
                )
                primary_events = latest_events(events_path(primary))
                sibling_events = latest_events(events_path(sibling))

            self.assertEqual(primary_count, 0)
            self.assertEqual(primary_events, [])
            self.assertEqual(sibling_count, 2)
            self.assertEqual(
                [event["title"] for event in sibling_events],
                ["Running tests", "Tests passed"],
            )

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
                patch("side_dog.panel.load_agent_identities", return_value=identity),
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


def write_codex_session(
    sessions: Path,
    session_id: str,
    cwd: Path,
    *,
    originator: str = "Codex Desktop",
    thread_source: str = "user",
    model: str = "",
    effort: str = "",
    age_seconds: float = 0.0,
) -> Path:
    """A session file shaped like the ones Codex writes, aged by its mtime."""
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-09-01T13-00-00-{session_id}.jsonl"
    records: list[dict[str, object]] = [
        {
            "timestamp": "2026-09-01T13:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "session_id": session_id,
                "cwd": os.fspath(cwd),
                "originator": originator,
                "thread_source": thread_source,
            },
        }
    ]
    if model or effort:
        records.append(
            {
                "timestamp": "2026-09-01T13:00:01.000Z",
                "type": "turn_context",
                "payload": {"model": model, "effort": effort},
            }
        )
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    changed = time.time() - age_seconds
    os.utime(path, (changed, changed))
    return path


def write_pi_session(
    sessions: Path,
    session_id: str,
    cwd: Path,
    *,
    model: str = "",
    effort: str = "",
    age_seconds: float = 0.0,
) -> Path:
    """A session file shaped like the ones Pi writes, aged by its mtime."""
    encoded = os.fspath(cwd).replace("/", "-")
    directory = sessions / f"-{encoded}-"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"2026-09-01T22-06-41-476Z_{session_id}.jsonl"
    records: list[dict[str, object]] = [
        {
            "type": "session",
            "version": 3,
            "id": session_id,
            "timestamp": "2026-09-01T22:06:41.476Z",
            "cwd": os.fspath(cwd),
        }
    ]
    if model:
        records.append(
            {
                "type": "model_change",
                "timestamp": "2026-09-01T22:06:41.502Z",
                "provider": "anthropic",
                "modelId": model,
            }
        )
    if effort:
        records.append(
            {
                "type": "thinking_level_change",
                "timestamp": "2026-09-01T22:06:41.503Z",
                "thinkingLevel": effort,
            }
        )
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    changed = time.time() - age_seconds
    os.utime(path, (changed, changed))
    return path


class PiSessionIdentitiesTest(TestCase):
    """Pi agents with no Herdr pane, read from Pi's own session files."""

    def setUp(self) -> None:
        clear_session_path_cache()
        side_dog_cli.PI_SESSION_HEADERS.clear()
        side_dog_cli.PI_METADATA_CACHE.clear()
        side_dog_cli.PI_LISTING_CACHE.clear()
        git_repository_location.cache_clear()

    tearDown = setUp

    def test_a_pi_session_is_an_agent_with_model_and_effort(self) -> None:
        with TemporaryDirectory() as directory:
            pi_home = Path(directory) / "pi"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            session_id = "01a05f02-9b44-7525-9f04-4ef76aec5a18"
            write_pi_session(
                pi_home / "agent" / "sessions",
                session_id,
                folder,
                model="claude-opus-4-8",
                effort="medium",
            )
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": os.fspath(pi_home / "agent")}):
                identities = load_pi_session_identities(folder)

            self.assertEqual(list(identities), [session_id])
            identity = identities[session_id]
            self.assertEqual(identity["agent"], "pi")
            self.assertEqual(identity["session_id"], session_id)
            self.assertEqual(identity["status"], "working")
            self.assertEqual(identity["model"], "claude-opus-4-8")
            self.assertEqual(identity["effort"], "medium")
            self.assertEqual(identity["label"], "Pi · project")
            self.assertEqual(identity["working_root"], os.fspath(folder))
            self.assertEqual(identity["root"], os.fspath(folder))
            self.assertEqual(identity["pane_id"], "")

    def test_a_quiet_session_reads_as_idle_and_an_old_one_is_gone(self) -> None:
        with TemporaryDirectory() as directory:
            pi_home = Path(directory) / "pi"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            sessions = pi_home / "agent" / "sessions"
            quiet = "01a058fb-eb33-75d0-9133-cdbe745dadd1"
            write_pi_session(sessions, quiet, folder, age_seconds=300)
            write_pi_session(
                sessions,
                "01a04a7a-13f7-78e3-a962-b4a22d94d7d3",
                folder,
                age_seconds=CODEX_SESSION_IDENTITY_WINDOW_SECONDS + 60,
            )
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": os.fspath(pi_home / "agent")}):
                identities = load_pi_session_identities(folder)

            self.assertEqual(list(identities), [quiet])
            self.assertEqual(identities[quiet]["status"], "idle")

    def test_a_session_in_another_repository_is_left_alone(self) -> None:
        with TemporaryDirectory() as directory:
            pi_home = Path(directory) / "pi"
            folder = (Path(directory) / "project").resolve()
            elsewhere = (Path(directory) / "other").resolve()
            folder.mkdir()
            elsewhere.mkdir()
            write_pi_session(
                pi_home / "agent" / "sessions",
                "01a05d0f-6eaf-7aa2-b9dd-8ac703a35b87",
                elsewhere,
            )
            repositories = {
                os.fspath(folder): (os.fspath(folder / ".git"), os.fspath(folder)),
                os.fspath(elsewhere): (
                    os.fspath(elsewhere / ".git"),
                    os.fspath(elsewhere),
                ),
            }
            with (
                patch.dict(os.environ, {"PI_CODING_AGENT_DIR": os.fspath(pi_home / "agent")}),
                patch(
                    "side_dog.cli.git_repository_location",
                    side_effect=lambda path: repositories.get(path, ("", "")),
                ),
            ):
                identities = load_pi_session_identities(folder)

            self.assertEqual(identities, {})

    def test_herdr_keeps_the_session_it_already_reports(self) -> None:
        with TemporaryDirectory() as directory:
            pi_home = Path(directory) / "pi"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            session_id = "01a05f02-9b44-7525-9f04-4ef76aec5a18"
            write_pi_session(pi_home / "agent" / "sessions", session_id, folder)
            herdr = {
                session_id: {
                    "agent": "pi",
                    "session_id": session_id,
                    "pane_id": "w3:p2",
                    "label": "in a pane",
                    "status": "working",
                    "root": os.fspath(folder),
                    "working_root": os.fspath(folder),
                }
            }
            with (
                patch.dict(os.environ, {"PI_CODING_AGENT_DIR": os.fspath(pi_home / "agent")}),
                patch("side_dog.cli.load_herdr_identities", return_value=dict(herdr)),
            ):
                identities = load_agent_identities(folder)

            self.assertEqual(identities[session_id]["label"], "in a pane")
            self.assertEqual(identities[session_id]["pane_id"], "w3:p2")

    def test_a_pane_less_pi_session_reaches_the_renderer(self) -> None:
        with TemporaryDirectory() as directory:
            pi_home = Path(directory) / "pi"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            session_id = "01a05f02-9b44-7525-9f04-4ef76aec5a18"
            write_pi_session(pi_home / "agent" / "sessions", session_id, folder)
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": os.fspath(pi_home / "agent")}):
                identities = load_pi_session_identities(folder)
                shown = [
                    identity["session_id"]
                    for identity in active_agent_identities(identities)
                ]

            self.assertEqual(shown, [session_id])

    def test_a_herdr_pi_pane_is_named_with_its_pane_and_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            pi_home = Path(directory) / "pi"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            session_id = "01a05f02-9b44-7525-9f04-4ef76aec5a18"
            write_pi_session(
                pi_home / "agent" / "sessions",
                session_id,
                folder,
                model="claude-opus-4-8",
                effort="medium",
            )
            agent = {
                "agent": "pi",
                "foreground_cwd": os.fspath(folder),
                "pane_id": "w3:p2",
                "workspace_id": "ws1",
                "tab_id": "t1",
                "agent_status": "working",
                "terminal_title_stripped": "wiring pi",
                "agent_session": {"value": session_id},
            }
            with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": os.fspath(pi_home / "agent")}):
                identities = herdr_identities_for_root(folder, [agent])

            identity = identities[session_id]
            self.assertEqual(identity["agent"], "pi")
            self.assertEqual(identity["pane_id"], "w3:p2")
            self.assertEqual(identity["label"], "wiring pi")
            self.assertEqual(identity["model"], "claude-opus-4-8")
            self.assertEqual(identity["effort"], "medium")


def git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments], cwd=root, check=True, capture_output=True, text=True
    )


class CodexSessionIdentitiesTest(TestCase):
    """Codex agents with no Herdr pane, read from Codex's own session files."""

    def setUp(self) -> None:
        clear_session_path_cache()
        CODEX_SESSION_HEADERS.clear()
        CODEX_METADATA_CACHE.clear()
        git_repository_location.cache_clear()

    tearDown = setUp

    def test_a_desktop_session_is_an_agent_with_model_and_effort(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            session_id = "01a05cd8-7fbf-7731-8e2e-9eb133364945"
            write_codex_session(
                codex_home / "sessions" / "2026" / "09" / "01",
                session_id,
                folder,
                model="gpt-5-codex",
                effort="high",
            )
            with patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}):
                identities = load_codex_session_identities(folder)

            self.assertEqual(list(identities), [session_id])
            identity = identities[session_id]
            self.assertEqual(identity["agent"], "codex")
            self.assertEqual(identity["session_id"], session_id)
            self.assertEqual(identity["status"], "working")
            self.assertEqual(identity["model"], "gpt-5-codex")
            self.assertEqual(identity["effort"], "high")
            self.assertEqual(identity["label"], "Codex Desktop · project")
            self.assertEqual(identity["working_root"], os.fspath(folder))
            self.assertEqual(identity["root"], os.fspath(folder))
            self.assertEqual(identity["pane_id"], "")

    def test_helper_threads_are_left_to_the_worker_count(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            sessions = codex_home / "sessions" / "2026" / "09" / "01"
            write_codex_session(
                sessions,
                "01a05de6-f34e-7401-9a53-aed22a69865d",
                folder,
                originator="codex-tui",
                thread_source="subagent",
            )
            write_codex_session(
                sessions,
                "01a05de7-0dbe-7bf0-8713-9af5881a780b",
                folder,
                originator="codex-tui",
                thread_source="guardian_review",
            )
            top_level = "01a05d4a-80dd-7661-9288-9ef2f766a93f"
            write_codex_session(
                sessions,
                top_level,
                folder,
                originator="codex-tui",
                thread_source="user",
            )
            with patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}):
                identities = load_codex_session_identities(folder)

            self.assertEqual(list(identities), [top_level])

    def test_a_session_in_another_repository_is_left_alone(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            folder = (Path(directory) / "project").resolve()
            elsewhere = (Path(directory) / "other").resolve()
            folder.mkdir()
            elsewhere.mkdir()
            write_codex_session(
                codex_home / "sessions" / "2026" / "09" / "01",
                "01a05d0f-6eaf-7aa2-b9dd-8ac703a35b87",
                elsewhere,
            )
            repositories = {
                os.fspath(folder): (os.fspath(folder / ".git"), os.fspath(folder)),
                os.fspath(elsewhere): (
                    os.fspath(elsewhere / ".git"),
                    os.fspath(elsewhere),
                ),
            }
            with (
                patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}),
                patch(
                    "side_dog.cli.git_repository_location",
                    side_effect=lambda path: repositories.get(path, ("", "")),
                ),
            ):
                identities = load_codex_session_identities(folder)

            self.assertEqual(identities, {})

    def test_a_quiet_session_reads_as_idle_and_an_old_one_is_gone(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            sessions = codex_home / "sessions" / "2026" / "09" / "01"
            quiet = "01a058fb-eb33-75d0-9133-cdbe745dadd1"
            write_codex_session(sessions, quiet, folder, age_seconds=300)
            write_codex_session(
                sessions,
                "01a04a7a-13f7-78e3-a962-b4a22d94d7d3",
                folder,
                age_seconds=CODEX_SESSION_IDENTITY_WINDOW_SECONDS + 60,
            )
            with patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}):
                identities = load_codex_session_identities(folder)

            self.assertEqual(list(identities), [quiet])
            self.assertEqual(identities[quiet]["status"], "idle")

    def test_herdr_keeps_the_session_it_already_reports(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            paned = "01a04e2c-d755-7151-8def-c5131a5f71ce"
            desktop = "01a05da0-b07e-7f10-814d-ef4c6f5897c6"
            sessions = codex_home / "sessions" / "2026" / "09" / "01"
            write_codex_session(
                sessions, paned, folder, originator="codex-tui", thread_source="user"
            )
            write_codex_session(sessions, desktop, folder)
            herdr = {
                paned: {
                    "agent": "codex",
                    "session_id": paned,
                    "pane_id": "w8:p1",
                    "label": "release notes",
                    "status": "idle",
                    "root": os.fspath(folder),
                    "working_root": os.fspath(folder),
                },
                "pane:w8:p1": {
                    "agent": "codex",
                    "session_id": paned,
                    "pane_id": "w8:p1",
                    "label": "release notes",
                    "status": "idle",
                    "root": os.fspath(folder),
                    "working_root": os.fspath(folder),
                },
            }
            with (
                patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}),
                patch("side_dog.cli.load_herdr_identities", return_value=dict(herdr)),
            ):
                identities = load_agent_identities(folder)

            self.assertEqual(identities[paned]["label"], "release notes")
            self.assertEqual(identities[paned]["pane_id"], "w8:p1")
            self.assertEqual(identities[desktop]["label"], "Codex Desktop · project")
            sessions_shown = [
                identity["session_id"]
                for identity in active_agent_identities(identities)
            ]
            self.assertEqual(sorted(sessions_shown), sorted([paned, desktop]))

    def test_a_desktop_worktree_joins_the_repository_it_belongs_to(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory) / "codex"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            git(folder, "init", "--initial-branch", "main")
            git(folder, "config", "user.email", "test@example.com")
            git(folder, "config", "user.name", "Test")
            (folder / "README.md").write_text("hello\n")
            git(folder, "add", "README.md")
            git(folder, "commit", "-m", "first")
            worktree = (
                Path(directory) / "codex-worktrees" / "5a39" / "project"
            ).resolve()
            git(folder, "worktree", "add", os.fspath(worktree), "-b", "desktop")
            session_id = "01a05d99-1111-7f10-814d-ef4c6f5897c6"
            write_codex_session(
                codex_home / "sessions" / "2026" / "09" / "01",
                session_id,
                worktree,
                thread_source="automation",
            )
            with patch.dict(os.environ, {"CODEX_HOME": os.fspath(codex_home)}):
                identities = load_codex_session_identities(folder)

            self.assertEqual(list(identities), [session_id])
            self.assertEqual(identities[session_id]["root"], os.fspath(worktree))
            self.assertEqual(
                identities[session_id]["working_root"], os.fspath(worktree)
            )
            # The folder above keeps two worktrees of one repository apart.
            self.assertEqual(
                identities[session_id]["label"], "Codex Desktop · 5a39/project"
            )
