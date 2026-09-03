from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    STATE_ENV,
    CRUSH_ROOT_CHECKPOINT_SESSION,
    CRUSH_ROOT_CHECKPOINT_SOURCE,
    CrushPollAdapter,
    _crush_checkpoint_source,
    append_event_once,
    clear_crush_listing_cache,
    crush_identities,
    crush_working_folders,
    events_path,
    latest_events,
)
from side_dog.crush import (
    CRUSH_MESSAGE_LIMIT,
    CrushProject,
    CrushSession,
    crush_global_data,
    read_crush_activity,
    read_crush_projects,
    read_crush_sessions,
)
from side_dog.doctor import crush_readiness
from side_dog.integrations import (
    AdapterHealthStatus,
    AgentIdentity,
    SessionKey,
    StreamCheckpoint,
)
from side_dog.polling import CheckpointStore, PollCoordinator, PollErrorCode, PollTarget
from side_dog.panel import PanelFeed, encode_sse


def make_crush_database(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "crush.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT NOT NULL, "
        "updated_at INTEGER NOT NULL, created_at INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE messages ("
        "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, "
        "parts TEXT NOT NULL, model TEXT, provider TEXT, "
        "is_summary_message INTEGER NOT NULL DEFAULT 0, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
        "finished_at INTEGER)"
    )
    connection.commit()
    connection.close()
    return database


def insert_session(
    database: Path,
    session_id: str,
    *,
    title: str = "Crush task",
    parent: str | None = None,
    created_at: int = 1000,
    updated_at: int = 1000,
) -> None:
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (session_id, parent, title, updated_at, created_at),
    )
    connection.commit()
    connection.close()


def insert_message(
    database: Path,
    message_id: str,
    session_id: str,
    parts: list[dict],
    *,
    role: str = "assistant",
    model: str = "claude-sonnet-4",
    provider: str = "anthropic",
    created_at: int = 1000,
    updated_at: int = 1000,
    finished_at: int | None = None,
    summary: bool = False,
) -> None:
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            session_id,
            role,
            json.dumps(parts),
            model,
            provider,
            int(summary),
            created_at,
            updated_at,
            finished_at,
        ),
    )
    connection.commit()
    connection.close()


def tool_call(
    call_id: str, name: str, arguments: dict, *, finished: bool = True
) -> dict:
    return {
        "type": "tool_call",
        "data": {
            "id": call_id,
            "name": name,
            "input": json.dumps(arguments),
            "finished": finished,
        },
    }


def tool_result(call_id: str, *, failed: bool = False, private: str = "") -> dict:
    return {
        "type": "tool_result",
        "data": {
            "tool_call_id": call_id,
            "name": "bash",
            "content": private,
            "data": private,
            "metadata": private,
            "is_error": failed,
        },
    }


def finish(reason: str, epoch: int, private: str = "") -> dict:
    return {
        "type": "finish",
        "data": {
            "reason": reason,
            "time": epoch,
            "message": private,
            "details": private,
        },
    }


def write_project_index(global_data: Path, projects: list[dict]) -> Path:
    global_data.mkdir(parents=True, exist_ok=True)
    index = global_data / "projects.json"
    index.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return index


class CrushReaderTest(TestCase):
    def test_global_data_uses_supported_absolute_overrides(self) -> None:
        self.assertEqual(
            crush_global_data({"CRUSH_GLOBAL_DATA": "/tmp/crush-data"}),
            Path("/tmp/crush-data"),
        )
        self.assertEqual(
            crush_global_data({"XDG_DATA_HOME": "/tmp/share"}),
            Path("/tmp/share/crush"),
        )
        self.assertIsNone(crush_global_data({"CRUSH_GLOBAL_DATA": "relative"}))

    def test_project_index_honours_relative_and_absolute_data_dirs(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            project = (base / "project").resolve()
            project.mkdir()
            absolute = (base / "elsewhere").resolve()
            index = write_project_index(
                base / "global",
                [
                    {
                        "path": os.fspath(project),
                        "data_dir": ".crush",
                        "last_accessed": "2026-09-03T10:00:00Z",
                    },
                    {
                        "path": os.fspath(project),
                        "data_dir": os.fspath(absolute),
                        "last_accessed": "invalid",
                    },
                    {"path": "relative", "data_dir": ".crush"},
                ],
            )

            projects = read_crush_projects(index)

        self.assertEqual(len(projects), 2)
        self.assertEqual(projects[0].data_dir, project / ".crush")
        self.assertGreater(projects[0].last_accessed_epoch_ms, 0)
        self.assertEqual(projects[1].data_dir, absolute)

    def test_project_limit_selects_recent_valid_entries_after_parsing(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            active = (base / "active").resolve()
            index = write_project_index(
                base / "global",
                [
                    {"path": "relative", "data_dir": ".crush"},
                    {
                        "path": os.fspath((base / "old-one").resolve()),
                        "data_dir": ".crush",
                        "last_accessed": "2026-01-01T00:00:00Z",
                    },
                    {
                        "path": os.fspath((base / "old-two").resolve()),
                        "data_dir": ".crush",
                        "last_accessed": "2026-02-01T00:00:00Z",
                    },
                    {
                        "path": os.fspath(active),
                        "data_dir": ".crush",
                        "last_accessed": "2026-09-03T12:00:00Z",
                    },
                ],
            )

            with patch("side_dog.crush.CRUSH_PROJECT_LIMIT", 2):
                projects = read_crush_projects(index)

        self.assertEqual(projects[0].path, active)
        self.assertEqual(
            {project.path for project in projects},
            {active, (base / "old-two").resolve()},
        )

    def test_malformed_index_and_parent_relationships_fail_closed(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            index = base / "projects.json"
            index.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid Crush project index"):
                read_crush_projects(index, strict=True)

            database = make_crush_database(base / "data")
            insert_session(database, "child", parent="private parent identifier")

            self.assertEqual(read_crush_sessions(database), ())

    def test_sessions_use_latest_non_summary_message_and_finish_state(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "parent", title="Fix issue")
            insert_message(
                database,
                "older",
                "parent",
                [finish("end_turn", 900)],
                model="old-model",
                updated_at=900,
            )
            insert_message(
                database,
                "latest",
                "parent",
                [finish("tool_use", 1000)],
                model="new-model",
                provider="openai",
                updated_at=1000,
            )
            insert_message(
                database,
                "summary",
                "parent",
                [finish("end_turn", 1100)],
                model="private-summary-model",
                updated_at=1100,
                summary=True,
            )

            sessions = read_crush_sessions(database)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].model, "new-model")
        self.assertEqual(sessions[0].provider, "openai")
        self.assertEqual(sessions[0].finish_reason, "tool_use")
        self.assertFalse(sessions[0].finished)

    def test_session_limit_preserves_parent_chain_and_rejects_orphans(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "parent", title="Public task", updated_at=1)
            insert_session(
                database,
                "child",
                title="private subagent prompt",
                parent="parent",
                updated_at=1000,
            )
            insert_session(database, "filler", updated_at=999)
            insert_session(
                database,
                "orphan",
                title="another private prompt",
                parent="missing-parent",
                updated_at=998,
            )

            with patch("side_dog.crush.CRUSH_SESSION_LIMIT", 3):
                sessions = read_crush_sessions(database)

        records = {session.session_id: session for session in sessions}
        self.assertEqual(set(records), {"parent", "child", "filler"})
        self.assertEqual(records["child"].parent_session_id, "parent")
        self.assertNotIn(
            "private subagent prompt",
            {s.title for s in sessions if not s.parent_session_id},
        )

    def test_activity_pairs_calls_results_turns_and_shell_commands(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "session")
            insert_message(
                database,
                "assistant",
                "session",
                [
                    tool_call("test", "bash", {"command": "pytest tests"}),
                    tool_call(
                        "partial", "edit", {"file_path": "ignored.py"}, finished=False
                    ),
                    finish("end_turn", 1002),
                ],
                updated_at=1001,
            )
            insert_message(
                database,
                "result",
                "session",
                [
                    tool_result("test"),
                    {
                        "type": "shell_command",
                        "data": {
                            "command": "git status",
                            "output": "private",
                            "exit_code": 0,
                        },
                    },
                ],
                role="tool",
                updated_at=1002,
            )

            calls, turns, boundary = read_crush_activity(
                database, {"session": 1_000_000}
            )

        self.assertEqual(
            [(call.call_id, call.status) for call in calls],
            [("test", "success"), ("shell:result:1", "success")],
        )
        self.assertEqual(
            [(turn.message_id, turn.status) for turn in turns],
            [("assistant", "success")],
        )
        self.assertEqual(boundary["session"], 1_002_000)

    def test_reader_drops_private_nonessential_tool_and_result_fields(self) -> None:
        private = "PRIVATE_CRUSH_READER_CANARY"
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "session")
            insert_message(
                database,
                "message",
                "session",
                [
                    private,
                    {"type": "text", "data": {"text": private}},
                    {"type": "reasoning", "data": {"thinking": private}},
                    tool_call(
                        "edit",
                        "edit",
                        {
                            "file_path": "safe.py",
                            "old_string": private,
                            "new_string": private,
                        },
                    ),
                    tool_result("edit", private=private),
                    finish("end_turn", 1000, private),
                ],
            )

            calls, turns, _boundary = read_crush_activity(database, {"session": 0})

        serialized = repr((calls, turns))
        self.assertIn("safe.py", serialized)
        self.assertNotIn(private, serialized)

    def test_activity_pages_oldest_new_messages_without_skipping(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "session")
            connection = sqlite3.connect(database)
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        f"message-{index:05d}",
                        "session",
                        "assistant",
                        '[{"type":"text","data":{"text":"private"}}]',
                        "model",
                        "provider",
                        0,
                        1001 + index,
                        1001 + index,
                        None,
                    )
                    for index in range(CRUSH_MESSAGE_LIMIT + 4)
                ),
            )
            connection.commit()
            connection.close()

            first_calls, first_turns, first = read_crush_activity(
                database, {"session": 1_000_000}
            )
            second_calls, second_turns, second = read_crush_activity(database, first)

        self.assertEqual((first_calls, first_turns), ((), ()))
        self.assertEqual((second_calls, second_turns), ((), ()))
        self.assertEqual(first["session"], 5_096_000)
        self.assertEqual(second["session"], 5_100_000)

    def test_activity_overlap_is_bounded_independently_per_session(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "busy")
            insert_session(database, "quiet")
            insert_message(database, "busy-1", "busy", [], updated_at=4999)
            insert_message(database, "busy-2", "busy", [], updated_at=4999)
            insert_message(
                database,
                "quiet-call",
                "quiet",
                [tool_call("quiet-tool", "bash", {"command": "pytest tests"})],
                updated_at=4998,
            )
            insert_message(
                database,
                "quiet-result",
                "quiet",
                [tool_result("quiet-tool")],
                role="tool",
                updated_at=5001,
            )

            with patch("side_dog.crush.CRUSH_MESSAGE_LIMIT", 2):
                calls, _turns, boundaries = read_crush_activity(
                    database,
                    {"busy": 5_000_000, "quiet": 5_000_000},
                )

        self.assertIn(
            ("quiet", "quiet-tool", "success"),
            {(call.session_id, call.call_id, call.status) for call in calls},
        )
        self.assertEqual(boundaries["quiet"], 5_001_000)

    def test_activity_overlap_retains_call_matched_by_new_result(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "session")
            insert_message(database, "newer-1", "session", [], updated_at=4999)
            insert_message(database, "newer-2", "session", [], updated_at=4999)
            insert_message(
                database,
                "older-call",
                "session",
                [tool_call("delayed-tool", "bash", {"command": "pytest tests"})],
                updated_at=4998,
            )
            insert_message(
                database,
                "new-result",
                "session",
                [tool_result("delayed-tool")],
                role="tool",
                updated_at=5001,
            )

            with patch("side_dog.crush.CRUSH_MESSAGE_LIMIT", 2):
                calls, _turns, boundaries = read_crush_activity(
                    database,
                    {"session": 5_000_000},
                )

        self.assertIn(
            ("delayed-tool", "success"),
            {(call.call_id, call.status) for call in calls},
        )
        self.assertEqual(boundaries["session"], 5_001_000)

    def test_activity_ignores_history_before_session_overlap_floor(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "session")
            insert_message(
                database,
                "old-call",
                "session",
                [tool_call("too-old", "bash", {"command": "pytest tests"})],
                updated_at=4000,
            )
            insert_message(
                database,
                "new-result",
                "session",
                [tool_result("too-old")],
                role="tool",
                updated_at=5001,
            )

            with patch("side_dog.crush.CRUSH_OVERLAP_MS", 500_000):
                calls, _turns, boundaries = read_crush_activity(
                    database,
                    {"session": 5_000_000},
                )

        self.assertEqual(calls, ())
        self.assertEqual(boundaries["session"], 5_001_000)

    def test_activity_retains_equal_cursor_result_and_matching_call(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "session")
            insert_message(database, "z-newer-1", "session", [], updated_at=5000)
            insert_message(database, "z-newer-2", "session", [], updated_at=5000)
            insert_message(
                database,
                "older-call",
                "session",
                [tool_call("same-second-tool", "bash", {"command": "pytest tests"})],
                updated_at=4998,
            )
            insert_message(
                database,
                "a-equal-result",
                "session",
                [tool_result("same-second-tool")],
                role="tool",
                updated_at=5000,
            )

            with patch("side_dog.crush.CRUSH_MESSAGE_LIMIT", 2):
                calls, _turns, boundaries = read_crush_activity(
                    database,
                    {"session": 5_000_000},
                )

        self.assertIn(
            ("same-second-tool", "success"),
            {(call.call_id, call.status) for call in calls},
        )
        self.assertEqual(boundaries["session"], 5_000_000)

    def test_shell_command_ids_are_qualified_by_message(self) -> None:
        with TemporaryDirectory() as directory:
            database = make_crush_database(Path(directory) / "data")
            insert_session(database, "session")
            for message_id, updated_at in (("first", 1001), ("second", 1002)):
                insert_message(
                    database,
                    message_id,
                    "session",
                    [
                        {
                            "type": "shell_command",
                            "data": {
                                "command": "git status",
                                "output": "private",
                                "exit_code": 0,
                            },
                        }
                    ],
                    updated_at=updated_at,
                )

            calls, _turns, _boundaries = read_crush_activity(
                database, {"session": 1_000_000}
            )

        self.assertEqual(
            {call.call_id for call in calls},
            {"shell:first:0", "shell:second:0"},
        )


class CrushIdentityTest(TestCase):
    def setUp(self) -> None:
        clear_crush_listing_cache()

    tearDown = setUp

    def test_parent_identity_uses_child_freshness_and_top_level_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            data_dir = root / ".crush"
            database = make_crush_database(data_dir)
            insert_session(database, "parent", title="Fix widgets", updated_at=900)
            insert_session(
                database,
                "child",
                parent="parent",
                title="Private child prompt",
                updated_at=1000,
            )
            insert_message(
                database,
                "parent-message",
                "parent",
                [finish("end_turn", 900)],
                model="model-one",
                provider="provider-one",
                updated_at=900,
            )
            insert_message(
                database,
                "child-message",
                "child",
                [finish("tool_use", 1000)],
                updated_at=1000,
            )
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": ".crush"}],
            )

            with patch.dict(
                os.environ, {"CRUSH_GLOBAL_DATA": os.fspath(global_data)}, clear=True
            ):
                identities = crush_identities(root, now=1000)
                folders = crush_working_folders(1000)

        self.assertEqual(list(identities), ["parent"])
        self.assertEqual(identities["parent"]["status"], "working")
        self.assertEqual(identities["parent"]["label"], "Fix widgets")
        self.assertEqual(identities["parent"]["model"], "model-one")
        self.assertEqual(identities["parent"]["inference_provider"], "provider-one")
        self.assertEqual(folders, [(os.fspath(root), True)])

    def test_identity_limit_never_promotes_child_title_to_top_level_label(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            database = make_crush_database(root / ".crush")
            insert_session(database, "parent", title="Public task", updated_at=1)
            insert_session(
                database,
                "child",
                title="private subagent prompt",
                parent="parent",
                updated_at=1000,
            )
            insert_session(database, "filler", updated_at=999)
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": ".crush"}],
            )

            with (
                patch.dict(
                    os.environ,
                    {"CRUSH_GLOBAL_DATA": os.fspath(global_data)},
                    clear=True,
                ),
                patch("side_dog.crush.CRUSH_SESSION_LIMIT", 2),
            ):
                identities = crush_identities(root, now=1000)

        self.assertIn("parent", identities)
        self.assertEqual(identities["parent"]["label"], "Public task")
        self.assertNotIn("child", identities)
        self.assertNotIn(
            "private subagent prompt",
            {identity["label"] for identity in identities.values()},
        )

    def test_finished_and_old_sessions_have_bounded_identity_lifetimes(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            database = make_crush_database(root / ".crush")
            insert_session(database, "done", updated_at=1000)
            insert_message(database, "done-message", "done", [finish("end_turn", 1000)])
            insert_session(database, "old", updated_at=1)
            insert_message(
                database, "old-message", "old", [finish("end_turn", 1)], updated_at=1
            )
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": os.fspath(root / ".crush")}],
            )
            with patch.dict(
                os.environ, {"CRUSH_GLOBAL_DATA": os.fspath(global_data)}, clear=True
            ):
                identities = crush_identities(root, now=1000)

        self.assertEqual(list(identities), ["done"])
        self.assertEqual(identities["done"]["status"], "done")


class CrushReadinessTest(TestCase):
    def test_ready_matching_store_is_checked_read_only(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            database = make_crush_database(root / ".crush")
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": ".crush"}],
            )
            before = database.read_bytes()

            health = crush_readiness(
                root,
                {
                    "HOME": os.fspath(base / "home"),
                    "CRUSH_GLOBAL_DATA": os.fspath(global_data),
                },
            )

            self.assertEqual(database.read_bytes(), before)
        self.assertIs(health.status, AdapterHealthStatus.AVAILABLE)

    def test_missing_configured_index_and_relative_override_degrade(self) -> None:
        with TemporaryDirectory() as directory:
            missing = crush_readiness(
                Path(directory),
                {"CRUSH_GLOBAL_DATA": os.fspath(Path(directory) / "missing")},
            )
        relative = crush_readiness(Path.cwd(), {"CRUSH_GLOBAL_DATA": "relative"})

        self.assertIs(missing.status, AdapterHealthStatus.DEGRADED)
        self.assertIs(relative.status, AdapterHealthStatus.DEGRADED)
        self.assertIn("absolute path", relative.detail)

    def test_malformed_index_degrades_without_parser_details(self) -> None:
        private = "PRIVATE_CRUSH_INDEX_CANARY"
        with TemporaryDirectory() as directory:
            base = Path(directory)
            global_data = base / "global"
            global_data.mkdir()
            (global_data / "projects.json").write_text(
                f"not-json-{private}", encoding="utf-8"
            )

            health = crush_readiness(
                base, {"CRUSH_GLOBAL_DATA": os.fspath(global_data)}
            )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)
        self.assertNotIn(private, health.detail)

    def test_no_matching_project_is_unavailable(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            other = (base / "other").resolve()
            root.mkdir()
            other.mkdir()
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(other), "data_dir": ".crush"}],
            )

            health = crush_readiness(
                root, {"CRUSH_GLOBAL_DATA": os.fspath(global_data)}
            )

        self.assertIs(health.status, AdapterHealthStatus.UNAVAILABLE)

    def test_matching_unsupported_schema_degrades_without_details(self) -> None:
        private = "PRIVATE_CRUSH_SCHEMA_CANARY"
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            data_dir = root / ".crush"
            data_dir.mkdir()
            database = data_dir / "crush.db"
            connection = sqlite3.connect(database)
            connection.execute(f'CREATE TABLE sessions ("{private}" TEXT)')
            connection.commit()
            connection.close()
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": ".crush"}],
            )

            health = crush_readiness(
                root, {"CRUSH_GLOBAL_DATA": os.fspath(global_data)}
            )

        self.assertIs(health.status, AdapterHealthStatus.DEGRADED)
        self.assertNotIn(private, health.detail)


class CrushPollAdapterTest(TestCase):
    def setUp(self) -> None:
        clear_crush_listing_cache()

    tearDown = setUp

    def test_missing_optional_store_does_not_report_a_poll_error(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            adapter = CrushPollAdapter(CheckpointStore(base / "state.sqlite"))
            with patch.dict(os.environ, {"HOME": os.fspath(base / "home")}, clear=True):
                batch = adapter.poll((PollTarget(root),))

        self.assertIsNone(batch.stats.last_error)
        self.assertEqual(batch.events, ())

    def test_checkpoint_load_error_aborts_events_and_checkpoints(self) -> None:
        private = "PRIVATE_CRUSH_CHECKPOINT_ERROR"
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = CrushProject(root, root / ".crush")
            sessions = (CrushSession("session", "", "Task", "", "", 1000, 1000, ""),)
            identity = AgentIdentity(
                agent="crush",
                session_id="session",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
            )
            store = CheckpointStore(root / "side-dog-state.sqlite")
            adapter = CrushPollAdapter(store)
            root_checkpoint = StreamCheckpoint(
                session=SessionKey("crush", CRUSH_ROOT_CHECKPOINT_SESSION),
                source=CRUSH_ROOT_CHECKPOINT_SOURCE,
                position=999,
            )
            with (
                patch.object(
                    store,
                    "load",
                    side_effect=(
                        root_checkpoint,
                        sqlite3.OperationalError(private),
                    ),
                ),
                patch(
                    "side_dog.cli._crush_session_snapshot",
                    return_value=(((project, sessions),), 1000, ()),
                ),
                patch("side_dog.cli.read_crush_activity") as read,
            ):
                batch = adapter.poll((PollTarget(root, (identity,)),))

        self.assertEqual(batch.events, ())
        self.assertEqual(batch.checkpoints, ())
        self.assertEqual(batch.stats.last_error, PollErrorCode.SQLITE)
        self.assertNotIn(private, repr(batch))
        read.assert_not_called()

    def test_fresh_unowned_session_tree_blocks_root_baseline_advance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = CrushProject(root, root / ".crush")
            sessions = (
                CrushSession("existing", "", "Existing", "", "", 999_000, 999_000, ""),
                CrushSession(
                    "new-session", "", "New", "", "", 1_001_000, 1_001_000, ""
                ),
            )
            identity = AgentIdentity(
                agent="crush",
                session_id="existing",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
            )
            adapter = CrushPollAdapter(CheckpointStore(root / "side-dog-state.sqlite"))
            with (
                patch("side_dog.cli.time.time", return_value=1000),
                patch(
                    "side_dog.cli._crush_session_snapshot",
                    return_value=(((project, sessions),), 1_002_000, ()),
                ),
                patch(
                    "side_dog.cli.read_crush_activity",
                    side_effect=lambda _database, positions: ((), (), positions),
                ),
            ):
                batch = adapter.poll((PollTarget(root, (identity,)),))

        self.assertFalse(
            any(
                checkpoint.source == CRUSH_ROOT_CHECKPOINT_SOURCE
                for _root, checkpoint in batch.checkpoints
            )
        )

    def test_failed_sibling_project_blocks_root_baseline_advance(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ready = CrushProject(root, root / ".crush-ready")
            failed = CrushProject(root, root / ".crush-busy")
            sessions = (
                CrushSession("existing", "", "Existing", "", "", 999_000, 999_000, ""),
            )
            identity = AgentIdentity(
                agent="crush",
                session_id="existing",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
            )
            adapter = CrushPollAdapter(CheckpointStore(root / "side-dog-state.sqlite"))
            with (
                patch("side_dog.cli.time.time", return_value=1000),
                patch(
                    "side_dog.cli._crush_session_snapshot",
                    return_value=(((ready, sessions),), 1_002_000, (failed,)),
                ),
                patch(
                    "side_dog.cli.read_crush_activity",
                    side_effect=lambda _database, positions: ((), (), positions),
                ),
            ):
                batch = adapter.poll((PollTarget(root, (identity,)),))

        self.assertFalse(
            any(
                checkpoint.source == CRUSH_ROOT_CHECKPOINT_SOURCE
                for _root, checkpoint in batch.checkpoints
            )
        )

    def test_stale_listing_does_not_advance_past_a_new_session(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            database = make_crush_database(root / ".crush")
            insert_session(database, "existing", updated_at=999)
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": ".crush"}],
            )
            existing = AgentIdentity(
                agent="crush",
                session_id="existing",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
            )
            store = CheckpointStore(base / "state.sqlite")
            adapter = CrushPollAdapter(store)
            with (
                patch.dict(
                    os.environ,
                    {"CRUSH_GLOBAL_DATA": os.fspath(global_data)},
                    clear=True,
                ),
                patch("side_dog.cli.time.time", return_value=1000.9),
            ):
                baseline = adapter.poll((PollTarget(root, (existing,)),))
                store.save_many(baseline.checkpoints)

                insert_session(database, "new-session", updated_at=1000)
                insert_message(
                    database,
                    "new-message",
                    "new-session",
                    [
                        tool_call("new-call", "bash", {"command": "pytest tests"}),
                        tool_result("new-call"),
                    ],
                    created_at=1000,
                    updated_at=1000,
                )
                with patch("side_dog.cli.time.time", return_value=1000.95):
                    stale = adapter.poll((PollTarget(root, (existing,)),))
                    store.save_many(stale.checkpoints)

                root_checkpoint = store.load(
                    root,
                    SessionKey("crush", CRUSH_ROOT_CHECKPOINT_SESSION),
                    CRUSH_ROOT_CHECKPOINT_SOURCE,
                )
                clear_crush_listing_cache()
                new_identity = AgentIdentity(
                    agent="crush",
                    session_id="new-session",
                    status="working",
                    root=os.fspath(root),
                    working_root=os.fspath(root),
                )
                with patch("side_dog.cli.time.time", return_value=1003):
                    discovered = adapter.poll((PollTarget(root, (new_identity,)),))

        self.assertIsNotNone(root_checkpoint)
        assert root_checkpoint is not None
        self.assertEqual(root_checkpoint.position, 1_000_000)
        self.assertTrue(any(event.kind == "test" for _root, event in discovered.events))

    def test_coordinator_restart_persists_each_safe_event_once(self) -> None:
        private = "PRIVATE_CRUSH_DURABLE_CANARY"
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            database = make_crush_database(root / ".crush")
            insert_session(database, "parent", updated_at=999)
            initial_parts = [
                tool_call(
                    "test",
                    "bash",
                    {"command": f"pytest tests --token {private}"},
                )
            ]
            insert_message(
                database,
                "assistant",
                "parent",
                initial_parts,
                created_at=999,
                updated_at=999,
            )
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE files (id TEXT, session_id TEXT, path TEXT, content TEXT)"
            )
            connection.execute(
                "INSERT INTO files VALUES (?, ?, ?, ?)",
                ("snapshot", "parent", "private.txt", private),
            )
            connection.commit()
            connection.close()
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": ".crush"}],
            )
            identity = AgentIdentity(
                agent="crush",
                session_id="parent",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
            )
            target = PollTarget(root, (identity,))
            state = base / "state"
            store = CheckpointStore(base / "checkpoints.sqlite")

            def run_once() -> tuple:
                coordinator = PollCoordinator(
                    (CrushPollAdapter(store),),
                    event_sink=append_event_once,
                    checkpoint_store=store,
                )
                try:
                    coordinator.tick((target,))
                    return coordinator.drain(timeout=2.0)
                finally:
                    coordinator.close()

            with (
                patch.dict(
                    os.environ,
                    {
                        "CRUSH_GLOBAL_DATA": os.fspath(global_data),
                        STATE_ENV: os.fspath(state),
                    },
                    clear=True,
                ),
                patch("side_dog.cli.time.time", return_value=1000),
            ):
                baseline = run_once()

                connection = sqlite3.connect(database)
                connection.execute(
                    "UPDATE messages SET parts = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                    (
                        json.dumps(
                            initial_parts
                            + [
                                tool_result("test", private=private),
                                {
                                    "type": "shell_command",
                                    "data": {
                                        "command": f"python --token {private}",
                                        "output": private,
                                        "exit_code": 1,
                                    },
                                },
                                finish("end_turn", 1001, private),
                            ]
                        ),
                        1001,
                        1001,
                        "assistant",
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (1001, "parent"),
                )
                connection.commit()
                connection.close()
                clear_crush_listing_cache()

                completed = run_once()
                replayed = run_once()
                history = latest_events(events_path(root))
                with (
                    patch("side_dog.panel.load_git_state", return_value={}),
                    patch.object(PanelFeed, "_start_external_refreshes"),
                ):
                    feed = PanelFeed(
                        [root],
                        follow_worktrees=False,
                        requested_roots=[root],
                        poll_coordinator=PollCoordinator(()),
                    )
                    try:
                        panel_sse = encode_sse("snapshot", feed.snapshot())
                    finally:
                        feed.close()

            self.assertEqual(baseline[0].events, ())
            self.assertEqual(
                {event.source_event_id for _root, event in completed[0].events},
                {event.source_event_id for _root, event in replayed[0].events},
            )
            self.assertEqual(len(history), len(completed[0].events))
            durable = json.dumps(history)
            self.assertNotIn(private, durable)
            self.assertNotIn(private.encode(), panel_sse)
            self.assertNotIn(private.encode(), store.path_for(root).read_bytes())
            for path in state.rglob("*"):
                if path.is_file():
                    self.assertNotIn(private.encode(), path.read_bytes())

    def test_baseline_restart_overlap_event_mapping_and_privacy(self) -> None:
        private = "PRIVATE_CRUSH_POLL_CANARY"
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            database = make_crush_database(root / ".crush")
            insert_session(database, "parent", updated_at=999)
            initial_parts = [
                tool_call(
                    "test",
                    "bash",
                    {"command": f"pytest tests --token {private}"},
                ),
                tool_call(
                    "edit",
                    "edit",
                    {
                        "file_path": "safe.py",
                        "old_string": private,
                        "new_string": private,
                    },
                ),
                {"type": "text", "data": {"text": private}},
                {"type": "reasoning", "data": {"thinking": private}},
            ]
            insert_message(
                database,
                "assistant",
                "parent",
                initial_parts,
                created_at=999,
                updated_at=999,
            )
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": os.fspath(root / ".crush")}],
            )
            checkpoint_source = _crush_checkpoint_source(
                read_crush_projects(global_data / "projects.json")[0]
            )
            identity = AgentIdentity(
                agent="crush",
                session_id="parent",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
                model="model-one",
            )
            target = PollTarget(root, (identity,))
            store = CheckpointStore(base / "checkpoints.sqlite")
            adapter = CrushPollAdapter(store)

            with (
                patch.dict(
                    os.environ,
                    {"CRUSH_GLOBAL_DATA": os.fspath(global_data)},
                    clear=True,
                ),
                patch("side_dog.cli.time.time", return_value=1000),
            ):
                baseline = adapter.poll((target,))
                store.save_many(baseline.checkpoints)

                connection = sqlite3.connect(database)
                connection.execute(
                    "UPDATE messages SET parts = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                    (
                        json.dumps(
                            initial_parts
                            + [
                                tool_result("test", private=private),
                                tool_result("edit", private=private),
                                finish("end_turn", 1001, private),
                            ]
                        ),
                        1001,
                        1001,
                        "assistant",
                    ),
                )
                connection.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (1001, "parent"),
                )
                connection.commit()
                connection.close()

                completed = adapter.poll((target,))
                store.save_many(completed.checkpoints)
                replay = adapter.poll((target,))

        self.assertEqual(baseline.events, ())
        kinds = {event.kind for _root, event in completed.events}
        self.assertTrue({"test", "file", "session"} <= kinds)
        serialized = json.dumps(
            {
                "events": [event.to_wire() for _root, event in completed.events],
                "checkpoints": [
                    checkpoint.to_wire() for _root, checkpoint in completed.checkpoints
                ],
                "stats": {
                    "provider": completed.stats.provider,
                    "error": completed.stats.last_error,
                },
            },
            default=str,
        )
        self.assertNotIn(private, serialized)
        self.assertEqual(
            {event.source_event_id for _root, event in replay.events},
            {event.source_event_id for _root, event in completed.events},
        )
        session_checkpoint = next(
            (
                checkpoint
                for _root, checkpoint in completed.checkpoints
                if checkpoint.session == SessionKey("crush", "parent")
                and checkpoint.source == checkpoint_source
            ),
            None,
        )
        self.assertIsNotNone(session_checkpoint)
        assert session_checkpoint is not None
        self.assertGreaterEqual(session_checkpoint.position, 1_001_000)

    def test_one_poll_reads_each_project_database_once_for_multiple_roots(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            global_data = base / "global"
            targets = []
            projects: list[dict] = []
            for index in range(2):
                root = (base / f"project-{index}").resolve()
                root.mkdir()
                database = make_crush_database(root / ".crush")
                session_id = f"session-{index}"
                insert_session(database, session_id)
                projects.append(
                    {"path": os.fspath(root), "data_dir": os.fspath(root / ".crush")}
                )
                targets.append(
                    PollTarget(
                        root,
                        (
                            AgentIdentity(
                                agent="crush",
                                session_id=session_id,
                                status="working",
                                root=os.fspath(root),
                                working_root=os.fspath(root),
                            ),
                        ),
                    )
                )
            write_project_index(global_data, projects)
            adapter = CrushPollAdapter(CheckpointStore(base / "state.sqlite"))
            with (
                patch.dict(
                    os.environ,
                    {"CRUSH_GLOBAL_DATA": os.fspath(global_data)},
                    clear=True,
                ),
                patch(
                    "side_dog.cli.read_crush_activity",
                    return_value=((), (), {}),
                ) as read,
            ):
                adapter.poll(tuple(targets))

        self.assertEqual(read.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in read.call_args_list},
            {Path(project["data_dir"]) / "crush.db" for project in projects},
        )

    def test_new_child_session_emits_subagent_and_failed_command_for_parent(
        self,
    ) -> None:
        private = "PRIVATE_CRUSH_CHILD_PROMPT"
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "project").resolve()
            root.mkdir()
            database = make_crush_database(root / ".crush")
            insert_session(database, "parent", updated_at=999)
            global_data = base / "global"
            write_project_index(
                global_data,
                [{"path": os.fspath(root), "data_dir": ".crush"}],
            )
            identity = AgentIdentity(
                agent="crush",
                session_id="parent",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
            )
            target = PollTarget(root, (identity,))
            store = CheckpointStore(base / "state.sqlite")
            adapter = CrushPollAdapter(store)
            with (
                patch.dict(
                    os.environ,
                    {"CRUSH_GLOBAL_DATA": os.fspath(global_data)},
                    clear=True,
                ),
                patch("side_dog.cli.time.time", return_value=1000),
            ):
                baseline = adapter.poll((target,))
                store.save_many(baseline.checkpoints)

                insert_session(
                    database,
                    "child$$call",
                    parent="parent",
                    title=private,
                    created_at=1001,
                    updated_at=1002,
                )
                insert_message(
                    database,
                    "child-message",
                    "child$$call",
                    [
                        tool_call(
                            "failed-call",
                            "bash",
                            {"command": f"python --prompt {private}"},
                        ),
                        tool_result("failed-call", failed=True, private=private),
                        finish("end_turn", 1002, private),
                    ],
                    created_at=1001,
                    updated_at=1002,
                    finished_at=1002,
                )
                insert_session(
                    database,
                    "child-error",
                    parent="parent",
                    created_at=1001,
                    updated_at=1003,
                )
                insert_message(
                    database,
                    "child-error-message",
                    "child-error",
                    [finish("error", 1003, private)],
                    created_at=1001,
                    updated_at=1003,
                    finished_at=1003,
                )
                insert_session(
                    database,
                    "child-canceled",
                    parent="parent",
                    created_at=1001,
                    updated_at=1004,
                )
                insert_message(
                    database,
                    "child-canceled-message",
                    "child-canceled",
                    [finish("canceled", 1004, private)],
                    created_at=1001,
                    updated_at=1004,
                    finished_at=1004,
                )
                clear_crush_listing_cache()

                batch = adapter.poll((target,))

        events = [event for _root, event in batch.events]
        self.assertEqual({event.session_id for event in events}, {"parent"})
        self.assertTrue(
            any(
                event.kind == "command"
                and event.status == "failed"
                and event.detail == "python"
                for event in events
            )
        )
        self.assertTrue(any(event.title == "Subagent started" for event in events))
        lifecycle = {
            (event.title, event.status)
            for event in events
            if event.kind == "session" and event.title.startswith("Subagent ")
        }
        self.assertIn(("Subagent completed", "success"), lifecycle)
        self.assertIn(("Subagent failed", "failed"), lifecycle)
        self.assertIn(("Subagent cancelled", "unknown"), lifecycle)
        self.assertNotIn(private, json.dumps([event.to_wire() for event in events]))
