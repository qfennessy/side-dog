from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    CLINE_LISTING_CACHE,
    ClineStream,
    STATE_ENV,
    agent_working_folders,
    cline_db_path,
    cline_data_dir,
    cline_identities,
    cline_session_listing,
    cline_sessions_root,
    events_path,
    latest_events,
    poll_cline_events,
)


def make_cline_db(data: Path) -> Path:
    db = data / "db" / "sessions.db"
    db.parent.mkdir(parents=True)
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE sessions ("
        "session_id TEXT PRIMARY KEY, pid INTEGER, status TEXT, cwd TEXT, "
        "workspace_root TEXT, model TEXT, metadata_json TEXT, messages_path TEXT, "
        "updated_at TEXT, started_at TEXT, parent_session_id TEXT, is_subagent INTEGER)"
    )
    connection.commit()
    connection.close()
    return db


def insert_session(
    db: Path,
    session_id: str,
    root: Path,
    messages: Path,
    *,
    title: str = "Fix the widget",
    model: str = "anthropic/claude-sonnet-4-6",
    status: str = "running",
    parent_id: str = "",
    is_subagent: bool = False,
) -> None:
    now = "2026-09-02T13:00:00.000Z"
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            os.getpid(),
            status,
            os.fspath(root),
            os.fspath(root),
            model,
            json.dumps({"title": title}),
            os.fspath(messages),
            now,
            now,
            parent_id,
            int(is_subagent),
        ),
    )
    connection.commit()
    connection.close()


def message_file(path: Path, session_id: str, messages: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-09-02T13:00:04.000Z",
                "sessionId": session_id,
                "messages": messages,
            }
        )
    )


class ClineIntegrationTest(TestCase):
    def setUp(self) -> None:
        CLINE_LISTING_CACHE.clear()

    tearDown = setUp

    def test_cline_session_store_names_a_live_agent(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "cline-data"
            db = make_cline_db(data)
            messages = data / "sessions" / "session-1" / "session-1.messages.json"
            message_file(messages, "session-1", [])
            insert_session(db, "session-1", root, messages)
            now = time.time()

            with (
                patch.dict(os.environ, {"CLINE_DATA_DIR": os.fspath(data)}),
                patch(
                    "side_dog.cli.git_repository_location",
                    return_value=(os.fspath(root / ".git"), os.fspath(root)),
                ),
                patch("side_dog.cli.process_is_alive", return_value=True),
            ):
                os.utime(messages, (now, now))
                identities = cline_identities(root, now=now)

            self.assertEqual(list(identities), ["session-1"])
            identity = identities["session-1"]
            self.assertEqual(identity["agent"], "cline")
            self.assertEqual(identity["label"], "Fix the widget")
            self.assertEqual(identity["model"], "anthropic/claude-sonnet-4-6")
            self.assertEqual(identity["status"], "working")
            self.assertEqual(identity["working_root"], os.fspath(root))

    def test_cline_paths_honor_all_storage_overrides(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            db_dir = base / "custom-db"
            db_dir.mkdir()
            expected = db_dir / "sessions.db"
            expected.touch()
            session_dir = base / "custom-sessions"
            with patch.dict(
                os.environ,
                {
                    "CLINE_DIR": os.fspath(base / "cline-home"),
                    "CLINE_DATA_DIR": os.fspath(base / "data"),
                    "CLINE_DB_DATA_DIR": os.fspath(db_dir),
                    "CLINE_SESSION_DATA_DIR": os.fspath(session_dir),
                },
            ):
                self.assertEqual(cline_data_dir(), base / "data")
                self.assertEqual(cline_db_path(), expected)
                self.assertEqual(cline_sessions_root(), session_dir)

            with patch.dict(
                os.environ,
                {"CLINE_DIR": os.fspath(base / "cline-home")},
                clear=True,
            ):
                self.assertEqual(cline_data_dir(), base / "cline-home" / "data")

    def test_native_tools_report_activity_without_storing_private_content(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            messages = Path(directory) / "session.messages.json"
            session_id = "session-private"
            message_file(
                messages,
                session_id,
                [
                    {
                        "id": "turn-1",
                        "role": "user",
                        "content": [{"type": "text", "text": "SECRET PROMPT"}],
                        "ts": 1_788_351_600_000,
                    },
                    {
                        "id": "assistant-1",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "commands-1",
                                "name": "run_commands",
                                "input": {
                                    "commands": [
                                        "python -m unittest discover -s tests -v",
                                        "git commit -m 'SECRET COMMIT MESSAGE'",
                                        {
                                            "command": "git",
                                            "args": ["push", "origin", "feature"],
                                        },
                                        {
                                            "commands": {
                                                "command": "python",
                                                "args": ["-m", "unittest", "tests.test_cline"],
                                            }
                                        },
                                    ]
                                },
                            }
                        ],
                        "ts": 1_788_351_601_000,
                        "modelInfo": {"id": "cline/test-model", "provider": "cline"},
                    },
                    {
                        "id": "result-1",
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "commands-1",
                                "name": "run_commands",
                                "content": [
                                    {"query": "SECRET COMMAND", "result": "SECRET OUTPUT", "success": True}
                                ],
                            }
                        ],
                        "ts": 1_788_351_602_000,
                    },
                    {
                        "id": "assistant-2",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "edit-1",
                                "name": "editor",
                                "input": {
                                    "path": os.fspath(root / "app.py"),
                                    "old_text": "SECRET OLD CONTENT",
                                    "new_text": "SECRET NEW CONTENT",
                                },
                            }
                        ],
                        "ts": 1_788_351_603_000,
                    },
                    {
                        "id": "result-2",
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "edit-1",
                                "name": "editor",
                                "content": json.dumps(
                                    {"result": "SECRET DIFF", "success": True}
                                ),
                            }
                        ],
                        "ts": 1_788_351_604_000,
                    },
                ],
            )
            stream = ClineStream(session_id, messages, agent_root=os.fspath(root))

            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                count = poll_cline_events(
                    root,
                    {},
                    {session_id: stream},
                )
                events = latest_events(events_path(root))
                second = poll_cline_events(root, {}, {session_id: stream})

            self.assertEqual(count, 10)
            self.assertEqual(second, 0)
            self.assertEqual(
                [event["title"] for event in events],
                [
                    "Running tests",
                    "Creating commit",
                    "Pushing branch",
                    "Running tests",
                    "Tests passed",
                    "Commit created",
                    "Branch pushed",
                    "Tests passed",
                    "Writing file",
                    "Wrote file",
                ],
            )
            self.assertEqual({event["agent"] for event in events}, {"cline"})
            self.assertEqual({event["turn_id"] for event in events}, {"turn-1"})
            self.assertEqual(events[-1]["model"], "cline/test-model")
            serialized = json.dumps(events)
            for secret in (
                "SECRET PROMPT",
                "SECRET COMMIT MESSAGE",
                "SECRET COMMAND",
                "SECRET OUTPUT",
                "SECRET OLD CONTENT",
                "SECRET NEW CONTENT",
                "SECRET DIFF",
                "discover -s tests",
            ):
                self.assertNotIn(secret, serialized)

    def test_relative_tool_paths_resolve_from_the_session_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            working = root / "nested"
            working.mkdir(parents=True)
            state = Path(directory) / "state"
            messages = Path(directory) / "session.messages.json"
            session_id = "session-from-subdirectory"
            message_file(
                messages,
                session_id,
                [
                    {
                        "id": "turn-subdirectory",
                        "role": "user",
                        "content": "edit the nested file",
                        "ts": 1_788_351_600_000,
                    },
                    {
                        "id": "assistant-subdirectory",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "edit-relative",
                                "name": "editor",
                                "input": {"path": "src/app.py", "new_text": "private"},
                            }
                        ],
                        "ts": 1_788_351_601_000,
                    },
                ],
            )
            listing = [
                {
                    "id": session_id,
                    "directory": os.fspath(working),
                    "model": "cline/model",
                    "messages_path": messages,
                    "parent_id": "",
                }
            ]
            identities = {
                session_id: {
                    "agent": "cline",
                    "session_id": session_id,
                    "root": os.fspath(root),
                    "working_root": os.fspath(working),
                    "model": "cline/model",
                }
            }
            streams: dict[str, ClineStream] = {}

            with (
                patch.dict(os.environ, {STATE_ENV: os.fspath(state)}),
                patch("side_dog.cli.cline_session_listing", return_value=listing),
            ):
                poll_cline_events(root, identities, streams)
                events = latest_events(events_path(root))

        self.assertEqual(streams[session_id].agent_root, os.fspath(working))
        self.assertEqual(events[0]["detail"], "nested/src/app.py")

    def test_batch_commands_keep_individual_result_statuses(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            messages = Path(directory) / "session.messages.json"
            session_id = "session-mixed-command-results"
            message_file(
                messages,
                session_id,
                [
                    {
                        "id": "assistant-mixed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "commands-mixed",
                                "name": "run_commands",
                                "input": {
                                    "commands": [
                                        "python -m unittest tests.test_cline",
                                        "git push origin feature",
                                    ]
                                },
                            }
                        ],
                        "ts": 1_788_351_601_000,
                    },
                    {
                        "id": "results-mixed",
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "commands-mixed",
                                "name": "run_commands",
                                "content": [
                                    {
                                        "query": "private",
                                        "result": '{"success": false}',
                                        "success": True,
                                    },
                                    {"query": "private", "result": "private", "success": False},
                                ],
                            }
                        ],
                        "ts": 1_788_351_602_000,
                    },
                ],
            )
            stream = ClineStream(session_id, messages, agent_root=os.fspath(root))

            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                poll_cline_events(root, {}, {session_id: stream})
                events = latest_events(events_path(root))

        self.assertEqual(
            [event["title"] for event in events],
            ["Running tests", "Pushing branch", "Tests passed", "Push failed"],
        )

    def test_failed_patch_and_subagent_events_are_normalized_and_deduplicated(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            messages = Path(directory) / "session.messages.json"
            session_id = "session-patch"
            patch_input = (
                "*** Begin Patch\n"
                "*** Update File: src/app.py\n"
                "@@\n-SECRET\n+PRIVATE\n"
                "*** End Patch"
            )
            message_file(
                messages,
                session_id,
                [
                    {
                        "id": "turn-2",
                        "role": "user",
                        "content": [{"type": "text", "text": "private"}],
                        "ts": 1_788_351_600_000,
                    },
                    {
                        "id": "assistant",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "patch-1",
                                "name": "apply_patch",
                                "input": patch_input,
                            },
                            {
                                "type": "tool_use",
                                "id": "spawn-1",
                                "name": "spawn_agent",
                                "input": {"task": "SECRET DELEGATION"},
                            },
                        ],
                        "ts": 1_788_351_601_000,
                    },
                    {
                        "id": "results",
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "patch-1",
                                "content": {"success": False, "error": "SECRET ERROR"},
                            },
                            {
                                "type": "tool_result",
                                "tool_use_id": "spawn-1",
                                "content": {"success": True, "text": "SECRET RESULT"},
                            },
                        ],
                        "ts": 1_788_351_602_000,
                    },
                ],
            )
            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                first = poll_cline_events(
                    root,
                    {},
                    {
                        session_id: ClineStream(
                            session_id, messages, agent_root=os.fspath(root)
                        )
                    },
                )
                second = poll_cline_events(
                    root,
                    {},
                    {
                        session_id: ClineStream(
                            session_id, messages, agent_root=os.fspath(root)
                        )
                    },
                )
                events = latest_events(events_path(root))

            self.assertEqual(first, 4)
            self.assertEqual(second, 0)
            self.assertEqual(
                [event["title"] for event in events],
                [
                    "Writing file",
                    "Subagent started",
                    "File write failed",
                    "Subagent completed",
                ],
            )
            self.assertEqual(events[0]["detail"], "src/app.py")
            serialized = json.dumps(events)
            self.assertNotIn("SECRET", serialized)
            self.assertNotIn("PRIVATE", serialized)

    def test_cline_sessions_feed_automatic_folder_discovery(self) -> None:
        root = Path("/tmp/cline-project").resolve()
        listing = [
            {
                "id": "session-1",
                "pid": os.getpid(),
                "status": "running",
                "directory": os.fspath(root),
                "time_updated": 1_788_351_600_000,
                "parent_id": "",
                "is_subagent": False,
            }
        ]
        with (
            patch("side_dog.cli.herdr_snapshot", return_value={}),
            patch("side_dog.cli.claude_session_registry", return_value=[]),
            patch("side_dog.cli.codex_recent_sessions", return_value=[]),
            patch("side_dog.cli.pi_recent_sessions", return_value=[]),
            patch("side_dog.cli.opencode_session_listing", return_value=[]),
            patch("side_dog.cli.cline_session_listing", return_value=listing),
            patch("side_dog.cli.process_is_alive", return_value=True),
            patch("side_dog.cli.worktree_root_for", return_value=root),
        ):
            folders = agent_working_folders(now=1_788_351_600)

        self.assertEqual(folders, {root: True})

    def test_listing_falls_back_to_session_manifests_without_sqlite(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            session_id = "file-session"
            session_dir = data / "sessions" / session_id
            session_dir.mkdir(parents=True)
            messages = session_dir / f"{session_id}.messages.json"
            message_file(messages, session_id, [])
            (session_dir / f"{session_id}.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_id": session_id,
                        "pid": os.getpid(),
                        "status": "idle",
                        "cwd": "/tmp/project",
                        "workspace_root": "/tmp/project",
                        "model": "cline/model",
                        "metadata": {"title": "From file storage"},
                        "started_at": "2026-09-02T13:00:00.000Z",
                        "messages_path": os.fspath(messages),
                    }
                )
            )

            with patch.dict(os.environ, {"CLINE_DATA_DIR": os.fspath(data)}):
                listing = cline_session_listing()

        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["title"], "From file storage")
        self.assertEqual(listing[0]["messages_path"], messages)

    def test_listing_merges_manifest_sessions_with_existing_sqlite_rows(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_cline_db(data)
            sqlite_messages = (
                data / "sessions" / "sqlite-session" / "sqlite-session.messages.json"
            )
            message_file(sqlite_messages, "sqlite-session", [])
            insert_session(db, "sqlite-session", root, sqlite_messages)
            sqlite_manifest = data / "sessions" / "sqlite-session" / "sqlite-session.json"
            current_root = root / "current"
            current_root.mkdir()
            sqlite_manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_id": "sqlite-session",
                        "pid": os.getpid(),
                        "status": "idle",
                        "cwd": os.fspath(current_root),
                        "workspace_root": os.fspath(root),
                        "model": "cline/current-manifest-model",
                        "started_at": "2026-09-02T13:00:00.000Z",
                        "messages_path": os.fspath(sqlite_messages),
                    }
                )
            )

            manifest_id = "manifest-session"
            manifest_dir = data / "sessions" / manifest_id
            manifest_dir.mkdir(parents=True)
            manifest_messages = manifest_dir / f"{manifest_id}.messages.json"
            message_file(manifest_messages, manifest_id, [])
            (manifest_dir / f"{manifest_id}.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "session_id": manifest_id,
                        "pid": os.getpid(),
                        "status": "running",
                        "cwd": os.fspath(root),
                        "workspace_root": os.fspath(root),
                        "model": "cline/file-model",
                        "started_at": "2026-09-02T13:00:00.000Z",
                        "messages_path": os.fspath(manifest_messages),
                    }
                )
            )

            with patch.dict(os.environ, {"CLINE_DATA_DIR": os.fspath(data)}):
                listing = cline_session_listing()

        self.assertEqual(
            {record["id"] for record in listing},
            {"sqlite-session", "manifest-session"},
        )
        current = next(record for record in listing if record["id"] == "sqlite-session")
        self.assertEqual(current["directory"], os.fspath(current_root))
        self.assertEqual(current["model"], "cline/current-manifest-model")

    def test_manifest_fallback_preserves_subagent_ancestry(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            now = time.time()
            for session_id, parent_id, is_subagent in (
                ("parent-session", "", False),
                ("child-session", "parent-session", True),
            ):
                session_dir = data / "sessions" / session_id
                session_dir.mkdir(parents=True)
                messages = session_dir / f"{session_id}.messages.json"
                message_file(messages, session_id, [])
                os.utime(messages, (now, now))
                (session_dir / f"{session_id}.json").write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "session_id": session_id,
                            "pid": os.getpid(),
                            "status": "running",
                            "cwd": os.fspath(root),
                            "workspace_root": os.fspath(root),
                            "model": "cline/model",
                            "started_at": "2026-09-02T13:00:00.000Z",
                            "messages_path": os.fspath(messages),
                            "parent_session_id": parent_id,
                            "is_subagent": is_subagent,
                        }
                    )
                )

            with (
                patch.dict(os.environ, {"CLINE_DATA_DIR": os.fspath(data)}),
                patch(
                    "side_dog.cli.git_repository_location",
                    return_value=(os.fspath(root / ".git"), os.fspath(root)),
                ),
                patch("side_dog.cli.process_is_alive", return_value=True),
            ):
                listing = cline_session_listing()
                identities = cline_identities(root, now=now)
                self.assertEqual(list(identities), ["parent-session"])
                identities["child-session"] = {
                    "agent": "cline",
                    "session_id": "child-session",
                    "root": os.fspath(root),
                    "working_root": os.fspath(root),
                    "model": "cline/model",
                }
                streams = {
                    "child-session": ClineStream(
                        "child-session",
                        data
                        / "sessions"
                        / "child-session"
                        / "child-session.messages.json",
                        agent_root=os.fspath(root),
                    )
                }
                poll_cline_events(root, identities, streams)

        child = next(record for record in listing if record["id"] == "child-session")
        self.assertEqual(child["parent_id"], "parent-session")
        self.assertTrue(child["is_subagent"])
        self.assertEqual(streams["child-session"].context_session_id, "parent-session")
