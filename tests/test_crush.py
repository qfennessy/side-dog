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
)
from side_dog.polling import CheckpointStore, PollCoordinator, PollTarget
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
                database, ("session",), 1_000_000
            )

        self.assertEqual(
            [(call.call_id, call.status) for call in calls],
            [("test", "success"), ("shell:1", "success")],
        )
        self.assertEqual(
            [(turn.message_id, turn.status) for turn in turns],
            [("assistant", "success")],
        )
        self.assertEqual(boundary, 1_002_000)

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

            calls, turns, _boundary = read_crush_activity(database, ("session",), 0)

        serialized = repr((calls, turns))
        self.assertIn("safe.py", serialized)
        self.assertNotIn(private, serialized)


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
                    return_value=((), (), 1_000_000),
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
        self.assertTrue(any(event.title == "Subagent completed" for event in events))
        self.assertNotIn(private, json.dumps([event.to_wire() for event in events]))
