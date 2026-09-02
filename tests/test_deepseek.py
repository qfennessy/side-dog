import json
import os
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import zstandard

from side_dog import cli as side_dog_cli
from side_dog.cli import (
    CODEX_SESSION_IDENTITY_WINDOW_SECONDS,
    NativeAgentStream,
    STATE_ENV,
    agent_working_folders,
    events_path,
    git_repository_location,
    latest_events,
    load_deepseek_session_identities,
    poll_native_agent_events,
)


def write_dsh_session(
    sessions: Path,
    session_id: str,
    cwd: Path,
    records: list[dict],
    *,
    compressed: bool = True,
    age_seconds: float = 0.0,
) -> Path:
    directory = sessions / "--project--" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("session.jsonl.zstd" if compressed else "session.jsonl")
    all_records = [
        {
            "type": "session",
            "version": 0,
            "id": session_id,
            "createdAt": int(time.time() * 1000),
            "cwd": os.fspath(cwd),
            "delegationDepth": 0,
        },
        *records,
    ]
    encoded = [(json.dumps(record) + "\n").encode() for record in all_records]
    if compressed:
        path.write_bytes(
            b"".join(zstandard.ZstdCompressor().compress(line) for line in encoded)
        )
    else:
        path.write_bytes(b"".join(encoded))
    changed = time.time() - age_seconds
    os.utime(path, (changed, changed))
    return path


class DeepSeekHarnessIdentityTest(TestCase):
    def setUp(self) -> None:
        side_dog_cli.DSH_SESSION_HEADERS.clear()
        side_dog_cli.DEEPSEEK_METADATA_CACHE.clear()
        side_dog_cli.DEEPSEEK_LISTING_CACHE.clear()
        side_dog_cli.clear_session_path_cache()
        git_repository_location.cache_clear()

    tearDown = setUp

    def test_compressed_session_is_named_with_model_and_effort(self) -> None:
        with TemporaryDirectory() as directory:
            dsh_home = Path(directory) / "dsh"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            session_id = "dsh-session"
            write_dsh_session(
                dsh_home / "sessions",
                session_id,
                folder,
                [
                    {
                        "type": "request/header",
                        "seq": 0,
                        "time": int(time.time() * 1000),
                        "data": {
                            "header": {
                                "config": {
                                    "provider": "deepseek-official",
                                    "model": "deepseek-v4-pro",
                                    "reasoningEffort": "high",
                                }
                            }
                        },
                    }
                ],
            )
            with patch.dict(os.environ, {"DSH_HOME": os.fspath(dsh_home)}):
                identities = load_deepseek_session_identities(folder)

            self.assertEqual(list(identities), [session_id])
            identity = identities[session_id]
            self.assertEqual(identity["agent"], "deepseek")
            self.assertEqual(identity["label"], "DeepSeek · project")
            self.assertEqual(identity["model"], "deepseek-v4-pro")
            self.assertEqual(identity["effort"], "high")
            self.assertEqual(identity["status"], "working")
            self.assertEqual(identity["working_root"], os.fspath(folder))

    def test_plain_session_and_dsh_home_feed_automatic_folder_discovery(self) -> None:
        with TemporaryDirectory() as directory:
            dsh_home = Path(directory) / "configured"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            write_dsh_session(
                dsh_home / "sessions", "raw-session", folder, [], compressed=False
            )
            with (
                patch.dict(os.environ, {"DSH_HOME": os.fspath(dsh_home)}),
                patch("side_dog.cli.herdr_snapshot", return_value={}),
                patch("side_dog.cli.claude_session_registry", return_value=[]),
                patch("side_dog.cli.codex_recent_sessions", return_value=[]),
                patch("side_dog.cli.pi_recent_sessions", return_value=[]),
                patch("side_dog.cli.opencode_session_listing", return_value=[]),
            ):
                folders = agent_working_folders()

            self.assertEqual(folders, {folder: True})

    def test_old_and_subagent_sessions_do_not_get_top_level_banners(self) -> None:
        with TemporaryDirectory() as directory:
            dsh_home = Path(directory) / "dsh"
            folder = (Path(directory) / "project").resolve()
            folder.mkdir()
            sessions = dsh_home / "sessions"
            write_dsh_session(
                sessions,
                "old",
                folder,
                [],
                age_seconds=CODEX_SESSION_IDENTITY_WINDOW_SECONDS + 60,
            )
            child = write_dsh_session(sessions, "child", folder, [])
            decoded, _ = side_dog_cli._dsh_records(child, 0)
            header, *records = decoded
            header["origin"] = "subagent"
            encoded = [(json.dumps(record) + "\n").encode() for record in [header, *records]]
            child.write_bytes(
                b"".join(
                    zstandard.ZstdCompressor().compress(line) for line in encoded
                )
            )
            self.setUp()
            with patch.dict(os.environ, {"DSH_HOME": os.fspath(dsh_home)}):
                identities = load_deepseek_session_identities(folder)

            self.assertEqual(identities, {})


class DeepSeekHarnessIngestionTest(TestCase):
    def setUp(self) -> None:
        side_dog_cli.DSH_SESSION_HEADERS.clear()
        side_dog_cli.DEEPSEEK_METADATA_CACHE.clear()
        side_dog_cli.DEEPSEEK_LISTING_CACHE.clear()
        side_dog_cli.clear_session_path_cache()
        git_repository_location.cache_clear()

    tearDown = setUp

    def test_tools_and_turns_are_ingested_without_private_content(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            sessions = Path(directory) / "dsh" / "sessions"
            now = int(time.time() * 1000)
            session_id = "dsh-ingest"
            path = write_dsh_session(
                sessions,
                session_id,
                root,
                [
                    {"type": "turn/start", "seq": 0, "time": now, "data": {"turn": 1}},
                    {
                        "type": "user/message",
                        "seq": 1,
                        "time": now,
                        "data": {"content": "PRIVATE PROMPT"},
                    },
                    {
                        "type": "tool/call",
                        "seq": 2,
                        "time": now + 1,
                        "data": {
                            "turn": 1,
                            "step": 1,
                            "callId": "test-call",
                            "name": "bash",
                            "arguments": json.dumps(
                                {"command": "python -m unittest", "workdir": os.fspath(root)}
                            ),
                        },
                    },
                    {
                        "type": "tool/result",
                        "seq": 3,
                        "time": now + 2,
                        "data": {
                            "message": {
                                "content": [
                                    {
                                        "type": "tool-result",
                                        "toolCallId": "test-call",
                                        "content": [{"type": "text", "text": "PRIVATE OUTPUT"}],
                                        "isError": False,
                                    }
                                ]
                            }
                        },
                    },
                    {
                        "type": "tool/call",
                        "seq": 4,
                        "time": now + 3,
                        "data": {
                            "callId": "write-call",
                            "name": "write",
                            "arguments": json.dumps(
                                {"path": os.fspath(root / "app.py"), "content": "PRIVATE FILE"}
                            ),
                        },
                    },
                    {
                        "type": "tool/result",
                        "seq": 5,
                        "time": now + 4,
                        "data": {
                            "message": {
                                "content": [
                                    {
                                        "type": "tool-result",
                                        "toolCallId": "write-call",
                                        "content": [{"type": "text", "text": "PRIVATE DIFF"}],
                                        "isError": False,
                                    }
                                ]
                            }
                        },
                    },
                    {
                        "type": "turn/end",
                        "seq": 6,
                        "time": now + 5,
                        "data": {"turn": 1, "reason": {"kind": "completed"}},
                    },
                ],
            )
            identity = {
                session_id: {
                    "agent": "deepseek",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            stream = NativeAgentStream(
                session_id, path, 0, agent_root=os.fspath(root), agent="deepseek"
            )
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                count = poll_native_agent_events(
                    root, identity, {session_id: stream}
                )
                events = latest_events(events_path(root))

            self.assertEqual(count, 5)
            self.assertEqual(
                [event["title"] for event in events],
                [
                    "Running tests",
                    "Tests passed",
                    "Writing file",
                    "Wrote file",
                    "DeepSeek turn finished",
                ],
            )
            self.assertTrue(all(event["agent"] == "deepseek" for event in events))
            self.assertEqual(events[2]["detail"], "app.py")
            rendered = json.dumps(events)
            for private in (
                "PRIVATE PROMPT",
                "PRIVATE OUTPUT",
                "PRIVATE FILE",
                "PRIVATE DIFF",
            ):
                self.assertNotIn(private, rendered)

    def test_read_only_str_replace_editor_call_emits_no_write(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            dsh_home = Path(directory) / "dsh"
            session_id = "dsh-view"
            now = int(time.time() * 1000)
            write_dsh_session(
                dsh_home / "sessions",
                session_id,
                root,
                [
                    {
                        "type": "tool/call",
                        "seq": 0,
                        "time": now,
                        "data": {
                            "callId": "view-call",
                            "name": "str_replace_editor",
                            "arguments": json.dumps(
                                {"command": "view", "path": "app.py"}
                            ),
                        },
                    },
                    {
                        "type": "tool/result",
                        "seq": 1,
                        "time": now + 1,
                        "data": {
                            "message": {
                                "content": [
                                    {
                                        "type": "tool-result",
                                        "toolCallId": "view-call",
                                        "content": [],
                                        "isError": False,
                                    }
                                ]
                            }
                        },
                    },
                ],
            )
            identity = {
                session_id: {
                    "agent": "deepseek",
                    "session_id": session_id,
                    "root": os.fspath(root),
                    "working_root": os.fspath(root),
                }
            }
            with patch.dict(
                os.environ,
                {"DSH_HOME": os.fspath(dsh_home), STATE_ENV: os.fspath(state)},
            ):
                count = poll_native_agent_events(root, identity, {})
                events = latest_events(events_path(root))

            self.assertEqual(count, 0)
            self.assertEqual(events, [])

    def test_relative_edit_path_uses_the_session_working_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            working_root = root / "packages" / "app"
            working_root.mkdir(parents=True)
            state = Path(directory) / "state"
            dsh_home = Path(directory) / "dsh"
            session_id = "dsh-nested-edit"
            now = int(time.time() * 1000)
            write_dsh_session(
                dsh_home / "sessions",
                session_id,
                working_root,
                [
                    {
                        "type": "tool/call",
                        "seq": 0,
                        "time": now,
                        "data": {
                            "callId": "edit-call",
                            "name": "edit",
                            "arguments": json.dumps({"path": "src/main.py"}),
                        },
                    }
                ],
            )
            identity = {
                session_id: {
                    "agent": "deepseek",
                    "session_id": session_id,
                    "root": os.fspath(root),
                    "working_root": os.fspath(working_root),
                }
            }
            with patch.dict(
                os.environ,
                {"DSH_HOME": os.fspath(dsh_home), STATE_ENV: os.fspath(state)},
            ):
                count = poll_native_agent_events(root, identity, {})
                events = latest_events(events_path(root))

            self.assertEqual(count, 1)
            self.assertEqual(events[0]["title"], "Writing file")
            self.assertEqual(events[0]["detail"], "packages/app/src/main.py")

    def test_restart_rehydrates_a_pending_call_at_the_saved_frame_cursor(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            dsh_home = Path(directory) / "dsh"
            session_id = "dsh-resume"
            now = int(time.time() * 1000)
            path = write_dsh_session(
                dsh_home / "sessions",
                session_id,
                root,
                [
                    {
                        "type": "tool/call",
                        "seq": 0,
                        "time": now,
                        "data": {
                            "callId": "call-1",
                            "name": "bash",
                            "arguments": json.dumps({"command": "pytest"}),
                        },
                    }
                ],
            )
            identity = {
                session_id: {
                    "agent": "deepseek",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            with patch.dict(
                os.environ,
                {"DSH_HOME": os.fspath(dsh_home), STATE_ENV: os.fspath(state)},
            ):
                first = poll_native_agent_events(root, identity, {})
                result = {
                    "type": "tool/result",
                    "seq": 1,
                    "time": now + 1,
                    "data": {
                        "message": {
                            "content": [
                                {
                                    "type": "tool-result",
                                    "toolCallId": "call-1",
                                    "content": [{"type": "text", "text": "ok"}],
                                    "isError": False,
                                }
                            ]
                        }
                    },
                }
                compressed = zstandard.ZstdCompressor().compress(
                    (json.dumps(result) + "\n").encode()
                )
                split = len(compressed) // 2
                with path.open("ab") as handle:
                    handle.write(compressed[:split])
                resumed_streams: dict[str, NativeAgentStream] = {}
                partial = poll_native_agent_events(
                    root, identity, resumed_streams
                )
                with path.open("ab") as handle:
                    handle.write(compressed[split:])
                second = poll_native_agent_events(
                    root, identity, resumed_streams
                )
                events = latest_events(events_path(root))

            self.assertEqual((first, partial, second), (1, 0, 1))
            self.assertEqual(
                [event["status"] for event in events if event["kind"] == "test"],
                ["running", "success"],
            )
