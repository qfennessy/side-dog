from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from side_dog.cli import (
    T3CodePollAdapter,
    _t3code_enrich_identity,
    clear_t3code_listing_cache,
    cursor_identities,
    load_cursor_metadata,
    load_grok_metadata,
    t3code_working_folders,
    t3code_session_listing,
)
from side_dog.integrations import AgentIdentity
from side_dog.polling import CheckpointStore, PollTarget
from side_dog.t3code import (
    T3CodePollRow,
    T3CodePollRequest,
    T3CodeSession,
    read_t3code_poll_rows,
    read_t3code_sessions,
    t3code_database_path,
)


SCHEMA = """
CREATE TABLE projection_projects (
  project_id TEXT PRIMARY KEY, workspace_root TEXT NOT NULL
);
CREATE TABLE projection_threads (
  thread_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
  worktree_path TEXT, model_selection_json TEXT, updated_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE TABLE projection_thread_sessions (
  thread_id TEXT PRIMARY KEY, status TEXT NOT NULL, provider_name TEXT,
  active_turn_id TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE provider_session_runtime (
  thread_id TEXT PRIMARY KEY, provider_name TEXT NOT NULL, status TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, resume_cursor_json TEXT
);
CREATE TABLE projection_thread_activities (
  activity_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
  sequence INTEGER
);
CREATE TABLE projection_turns (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL,
  turn_id TEXT, state TEXT NOT NULL, completed_at TEXT
);
"""


def fixture_database(base: Path, root: Path, *, provider: str = "cursor") -> Path:
    database = base / "userdata" / "state.sqlite"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO projection_projects VALUES (?, ?)",
        ("project-1", os.fspath(root)),
    )
    connection.execute(
        "INSERT INTO projection_threads VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (
            "thread-1",
            "project-1",
            "Review the release",
            os.fspath(root),
            json.dumps(
                {
                    "instanceId": provider,
                    "model": "agent-model",
                    "options": [{"id": "reasoningEffort", "value": "high"}],
                }
            ),
            "2026-09-02T18:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO projection_thread_sessions VALUES (?, ?, ?, ?, ?)",
        ("thread-1", "ready", provider, None, "2026-09-02T18:00:00Z"),
    )
    connection.execute(
        "INSERT INTO provider_session_runtime VALUES (?, ?, ?, ?, ?)",
        (
            "thread-1",
            provider,
            "ready",
            "2026-09-02T18:00:00Z",
            json.dumps({"schemaVersion": 1, "sessionId": "vendor-session"}),
        ),
    )
    connection.commit()
    connection.close()
    return database


class T3CodeStoreTest(TestCase):
    def tearDown(self) -> None:
        clear_t3code_listing_cache()

    def test_database_location_honors_only_t3code_home(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HOME": "/Users/example",
                "T3CODE_HOME": "/Volumes/private/t3",
                "T3CODE_STATE_DIR": "/wrong/state",
                "T3CODE_BASE_DIR": "/wrong/base",
            },
            clear=True,
        ):
            self.assertEqual(
                t3code_database_path(),
                Path("/Volumes/private/t3/userdata/state.sqlite"),
            )

    def test_session_projection_maps_provider_resume_model_and_status(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            database = fixture_database(base, root, provider="claudeAgent")
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE provider_session_runtime SET resume_cursor_json = ?",
                (json.dumps({"resume": "claude-session"}),),
            )
            connection.execute(
                "UPDATE projection_thread_sessions SET active_turn_id = ?",
                ("turn-running",),
            )
            connection.commit()
            connection.close()

            sessions = read_t3code_sessions(database)

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].provider, "claude-code")
        self.assertEqual(sessions[0].native_session_id, "claude-session")
        self.assertEqual(sessions[0].model, "agent-model")
        self.assertEqual(sessions[0].effort, "high")
        self.assertEqual(sessions[0].status, "working")

    def test_provider_resume_cursor_shapes_are_mapped(self) -> None:
        cases = (
            ("codex", {"threadId": "codex-session"}, "codex", "codex-session"),
            (
                "claudeAgent",
                {"resume": "claude-session"},
                "claude-code",
                "claude-session",
            ),
            ("cursor", {"schemaVersion": 1, "sessionId": "cursor-session"}, "cursor", "cursor-session"),
            ("grok", {"schemaVersion": 1, "sessionId": "grok-session"}, "grok", "grok-session"),
            (
                "opencode",
                {"schemaVersion": 1, "sessionId": "opencode-session"},
                "opencode",
                "opencode-session",
            ),
        )
        for raw_provider, cursor, provider, session_id in cases:
            with self.subTest(provider=raw_provider), TemporaryDirectory() as directory:
                base = Path(directory)
                root = (base / "repo").resolve()
                root.mkdir()
                database = fixture_database(base, root, provider=raw_provider)
                connection = sqlite3.connect(database)
                connection.execute(
                    "UPDATE provider_session_runtime SET resume_cursor_json = ?",
                    (json.dumps(cursor),),
                )
                connection.commit()
                connection.close()

                session = read_t3code_sessions(database)[0]

            self.assertEqual(session.provider, provider)
            self.assertEqual(session.native_session_id, session_id)

    def test_legacy_model_option_object_remains_readable(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "repo").resolve()
            root.mkdir()
            database = fixture_database(base, root)
            connection = sqlite3.connect(database)
            connection.execute(
                "UPDATE projection_threads SET model_selection_json = ?",
                (
                    json.dumps(
                        {
                            "instanceId": "cursor",
                            "model": "legacy-model",
                            "options": {"effort": "medium"},
                        }
                    ),
                ),
            )
            connection.commit()
            connection.close()

            session = read_t3code_sessions(database)[0]

        self.assertEqual(session.model, "legacy-model")
        self.assertEqual(session.effort, "medium")

    def test_schema_change_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            sqlite3.connect(database).close()

            with self.assertRaisesRegex(ValueError, "unsupported T3 Code schema"):
                read_t3code_sessions(database)

    def test_machine_wide_listing_is_shared_across_root_loaders(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "repo").resolve()
            root.mkdir()
            database = fixture_database(base, root)
            with (
                patch.dict(os.environ, {"T3CODE_HOME": os.fspath(base)}),
                patch(
                    "side_dog.cli.read_t3code_sessions",
                    wraps=read_t3code_sessions,
                ) as read,
            ):
                clear_t3code_listing_cache()
                first = t3code_session_listing()
                second = cursor_identities(
                    root, now=first[0].updated_epoch_ms / 1000
                )

            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 1)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(database, t3code_database_path({"T3CODE_HOME": str(base)}))

    def test_native_identity_is_enriched_without_losing_native_model(self) -> None:
        context = T3CodeSession(
            "thread-1",
            "codex",
            "native-1",
            "Fix the release",
            "/repo",
            "/repo/worktree",
            "working",
            "fallback-model",
            "medium",
            1,
        )
        native = {
            "agent": "codex",
            "session_id": "native-1",
            "root": "/repo/worktree",
            "working_root": "/repo/worktree",
            "label": "Codex",
            "model": "native-model",
            "effort": "high",
        }
        with patch("side_dog.cli.t3code_session_listing", return_value=(context,)):
            enriched = _t3code_enrich_identity(native, now=0.001)
            herdr = _t3code_enrich_identity(native, keep_label=True, now=0.001)

        self.assertEqual(enriched["label"], "Fix the release")
        self.assertEqual(enriched["working_root"], "/repo/worktree")
        self.assertEqual(enriched["model"], "native-model")
        self.assertEqual(enriched["effort"], "high")
        self.assertEqual(enriched["surface"], "T3 Code")
        self.assertEqual(herdr["label"], "Codex")

    def test_native_enrichment_rejects_stale_or_different_worktrees(self) -> None:
        context = T3CodeSession(
            "thread-1",
            "codex",
            "native-1",
            "Old T3 thread",
            "/repo",
            "/repo/t3-worktree",
            "idle",
            "fallback-model",
            "medium",
            1_000,
        )
        different_worktree = {
            "agent": "codex",
            "session_id": "native-1",
            "root": "/repo/native-worktree",
            "working_root": "/repo/native-worktree",
            "label": "Codex",
            "model": "native-model",
        }
        same_worktree = {
            **different_worktree,
            "root": "/repo/t3-worktree",
            "working_root": "/repo/t3-worktree",
        }
        with patch("side_dog.cli.t3code_session_listing", return_value=(context,)):
            wrong_root = _t3code_enrich_identity(different_worktree, now=1)
            stale = _t3code_enrich_identity(same_worktree, now=902)

        self.assertNotIn("surface", wrong_root)
        self.assertEqual(wrong_root["working_root"], "/repo/native-worktree")
        self.assertNotIn("surface", stale)
        self.assertEqual(stale["label"], "Codex")

    def test_missing_native_resume_id_does_not_create_an_identity(self) -> None:
        context = T3CodeSession(
            "thread-1",
            "cursor",
            "",
            "Unbound thread",
            "/repo",
            "/repo",
            "working",
            "agent-model",
            "high",
            1,
        )
        with patch("side_dog.cli.t3code_session_listing", return_value=(context,)):
            identities = cursor_identities(Path("/repo"), now=1)

        self.assertEqual(identities, {})

    def test_stale_runtime_is_retired_and_quiet_runtime_becomes_idle(self) -> None:
        updated_ms = 1_000_000
        context = T3CodeSession(
            "thread-1",
            "cursor",
            "cursor-session",
            "Long command",
            "/repo",
            "/repo",
            "working",
            "agent-model",
            "high",
            updated_ms,
        )
        with patch("side_dog.cli.t3code_session_listing", return_value=(context,)):
            quiet = cursor_identities(Path("/repo"), now=updated_ms / 1000 + 61)
            stale = cursor_identities(Path("/repo"), now=updated_ms / 1000 + 901)
            folders = t3code_working_folders(
                "cursor", updated_ms / 1000 + 61
            )

        self.assertEqual(next(iter(quiet.values()))["status"], "idle")
        self.assertEqual(stale, {})
        self.assertEqual(folders, [("/repo", False)])

    def test_newest_t3_thread_wins_when_a_native_session_is_resumed(self) -> None:
        newest = T3CodeSession(
            "thread-new",
            "cursor",
            "cursor-session",
            "Current thread",
            "/repo",
            "/repo",
            "working",
            "new-model",
            "high",
            2_000,
        )
        older = T3CodeSession(
            "thread-old",
            "cursor",
            "cursor-session",
            "Old thread",
            "/repo",
            "/repo",
            "idle",
            "old-model",
            "low",
            1_000,
        )
        with patch(
            "side_dog.cli.t3code_session_listing", return_value=(newest, older)
        ):
            identities = cursor_identities(Path("/repo"), now=2)

        identity = identities["cursor-session"]
        self.assertEqual(identity["label"], "Current thread")
        self.assertEqual(identity["t3code_thread_id"], "thread-new")
        self.assertEqual(identity["model"], "new-model")

    def test_per_root_identity_excludes_a_sibling_worktree(self) -> None:
        watched = Path("/repo/main")
        sibling = Path("/repo/sibling")
        context = T3CodeSession(
            "thread-1",
            "cursor",
            "cursor-session",
            "Sibling task",
            "/repo",
            os.fspath(sibling),
            "working",
            "agent-model",
            "high",
            1_000,
        )
        with (
            patch("side_dog.cli.t3code_session_listing", return_value=(context,)),
            patch("side_dog.cli._t3code_record_root", return_value=sibling),
            patch("side_dog.cli.git_common_dir", return_value="/repo/.git"),
        ):
            identities = cursor_identities(watched, now=1)
            discoverable = t3code_working_folders("cursor", 1)

        self.assertEqual(identities, {})
        self.assertEqual(discoverable, [(os.fspath(sibling), True)])

    def test_unscoped_t3_metadata_is_deferred_to_validated_enrichment(self) -> None:
        self.assertEqual(load_cursor_metadata("session"), {})
        self.assertEqual(load_grok_metadata("session"), {})

    def test_poll_query_returns_only_explicit_tool_fields(self) -> None:
        private = "PRIVATE_OUTPUT_CANARY_77"
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repo"
            root.mkdir()
            database = fixture_database(base, root)
            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO projection_thread_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "activity-1",
                    "thread-1",
                    "turn-1",
                    "tool.completed",
                    json.dumps(
                        {
                            "itemType": "command_execution",
                            "status": "completed",
                            "toolCallId": "call-1",
                            "data": {
                                "command": "pytest tests",
                                "rawOutput": private,
                                "result": private,
                            },
                            "assistantText": private,
                        }
                    ),
                    "2026-09-02T18:01:00Z",
                    1,
                ),
            )
            connection.commit()
            connection.close()

            rows = read_t3code_poll_rows(
                database, (T3CodePollRequest("thread-1", 1, None),)
            )

        serialized = repr(rows)
        self.assertIn("pytest tests", serialized)
        self.assertNotIn(private, serialized)
        self.assertNotIn("payload_json", serialized)


class T3CodePollAdapterTest(TestCase):
    def test_turn_completion_stays_in_its_originating_worktree(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            watched = (base / "watched").resolve()
            sibling = (base / "sibling").resolve()
            watched.mkdir()
            sibling.mkdir()
            identity = AgentIdentity(
                agent="cursor",
                session_id="vendor-session",
                status="working",
                root=os.fspath(sibling),
                working_root=os.fspath(sibling),
                extras={"t3code_thread_id": "thread-1"},
            )
            row = T3CodePollRow(
                row_type="turn",
                source_id="turn:1",
                thread_id="thread-1",
                turn_id="turn-1",
                kind="turn.completed",
                created_at="2026-09-02T18:00:00Z",
                sequence=1,
                maximum_position=1,
                minimum_open_turn=None,
                item_type="",
                status="",
                tool_call_id="",
                command="",
                paths=(),
            )
            adapter = T3CodePollAdapter(
                CheckpointStore(base / "side-dog-state.sqlite")
            )
            with patch("side_dog.cli.read_t3code_poll_rows", return_value=[row]):
                batch = adapter.poll((PollTarget(watched, (identity,)),))

        self.assertEqual(batch.events, ())

    def test_delayed_identity_emits_activity_since_watch_started(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "repo").resolve()
            root.mkdir()
            database = fixture_database(base, root)
            checkpoint_store = CheckpointStore(base / "side-dog-state.sqlite")
            adapter = T3CodePollAdapter(checkpoint_store)

            with patch("side_dog.cli.time.time", return_value=1_000):
                adapter.poll((PollTarget(root, ()),))

            connection = sqlite3.connect(database)
            for activity_id, command, created_at, sequence in (
                ("before-watch", "pytest old", "1970-01-01T00:16:39Z", 1),
                ("after-watch", "pytest new", "1970-01-01T00:16:41Z", 2),
            ):
                connection.execute(
                    "INSERT INTO projection_thread_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        activity_id,
                        "thread-1",
                        "turn-1",
                        "tool.completed",
                        json.dumps(
                            {
                                "itemType": "command_execution",
                                "status": "completed",
                                "toolCallId": activity_id,
                                "data": {"command": command},
                            }
                        ),
                        created_at,
                        sequence,
                    ),
                )
            for turn_id, state, completed_at in (
                ("abandoned-turn", "cancelled", None),
                ("before-watch-turn", "completed", "1970-01-01T00:16:39Z"),
                ("after-watch-turn", "completed", "1970-01-01T00:16:41Z"),
            ):
                connection.execute(
                    "INSERT INTO projection_turns(thread_id, turn_id, state, completed_at) VALUES (?, ?, ?, ?)",
                    ("thread-1", turn_id, state, completed_at),
                )
            connection.commit()
            connection.close()

            identity = AgentIdentity(
                agent="cursor",
                session_id="vendor-session",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
                extras={"t3code_thread_id": "thread-1"},
            )
            with patch("side_dog.cli.t3code_database_path", return_value=database):
                batch = adapter.poll((PollTarget(root, (identity,)),))
                checkpoint_store.save_many(batch.checkpoints)
                replay = adapter.poll((PollTarget(root, (identity,)),))

        source_ids = {
            event.source_event_id for _event_root, event in batch.events
        }
        self.assertTrue(any("after-watch" in source_id for source_id in source_ids))
        self.assertFalse(any("before-watch" in source_id for source_id in source_ids))
        replayed = {
            event.source_event_id for _event_root, event in replay.events
        }
        self.assertIn("t3code:thread-1:turn:3", replayed)
        self.assertNotIn("t3code:thread-1:turn:2", replayed)

    def test_one_poll_reads_all_watched_roots_once(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            checkpoint_store = CheckpointStore(base / "state.sqlite")
            targets = []
            for index, provider in enumerate(("cursor", "grok"), start=1):
                root = base / f"repo-{index}"
                root.mkdir()
                identity = AgentIdentity(
                    agent=provider,
                    session_id=f"session-{index}",
                    status="working",
                    root=os.fspath(root),
                    working_root=os.fspath(root),
                    extras={"t3code_thread_id": f"thread-{index}"},
                )
                targets.append(PollTarget(root, (identity,)))
            adapter = T3CodePollAdapter(checkpoint_store)
            with patch(
                "side_dog.cli.read_t3code_poll_rows", return_value=[]
            ) as read:
                adapter.poll(tuple(targets))

        self.assertEqual(read.call_count, 1)
        requests = read.call_args.args[1]
        self.assertEqual(
            {request.thread_id for request in requests},
            {"thread-1", "thread-2"},
        )

    def test_baseline_restart_routing_and_privacy(self) -> None:
        private = "PRIVATE_T3_CANARY_77"
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = (base / "repo").resolve()
            root.mkdir()
            outside = base / f"outside-{private}.txt"
            database = fixture_database(base, root)
            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO projection_thread_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "old-activity",
                    "thread-1",
                    "old-turn",
                    "tool.completed",
                    json.dumps(
                        {
                            "itemType": "command_execution",
                            "status": "completed",
                            "toolCallId": "old-call",
                            "data": {"command": f"echo {private}"},
                        }
                    ),
                    "2026-09-02T18:00:00Z",
                    10,
                ),
            )
            connection.execute(
                "INSERT INTO projection_turns(thread_id, turn_id, state, completed_at) VALUES (?, ?, ?, NULL)",
                ("thread-1", "turn-1", "running"),
            )
            connection.commit()
            connection.close()

            checkpoint_store = CheckpointStore(base / "side-dog-state.sqlite")
            identity = AgentIdentity(
                agent="cursor",
                session_id="vendor-session",
                status="working",
                root=os.fspath(root),
                working_root=os.fspath(root),
                label="Review the release",
                model="agent-model",
                effort="high",
                extras={"t3code_thread_id": "thread-1"},
            )
            target = PollTarget(root, (identity,))
            adapter = T3CodePollAdapter(checkpoint_store)
            with patch("side_dog.cli.t3code_database_path", return_value=database):
                baseline = adapter.poll((target,))
                checkpoint_store.save_many(baseline.checkpoints)

                connection = sqlite3.connect(database)
                activities = [
                    (
                        "test-start",
                        "tool.started",
                        {
                            "itemType": "command_execution",
                            "toolCallId": "test-call",
                            "data": {"command": f"pytest tests --token {private}"},
                        },
                        11,
                    ),
                    (
                        "test-finish",
                        "tool.completed",
                        {
                            "itemType": "command_execution",
                            "status": "completed",
                            "toolCallId": "test-call",
                            "data": {
                                "command": f"pytest tests --token {private}",
                                "rawOutput": private,
                            },
                        },
                        12,
                    ),
                    (
                        "file-finish",
                        "tool.completed",
                        {
                            "itemType": "file_change",
                            "status": "completed",
                            "toolCallId": "file-call",
                            "data": {
                                "files": [
                                    {"path": "safe.py"},
                                    {"path": "second.py"},
                                    {"path": "third.py"},
                                    {"path": "fourth.py"},
                                    {"path": "fifth.py"},
                                    {"path": os.fspath(outside)},
                                ],
                                "rawOutput": private,
                            },
                        },
                        13,
                    ),
                    (
                        "git-finish",
                        "tool.completed",
                        {
                            "itemType": "command_execution",
                            "status": "completed",
                            "toolCallId": "git-call",
                            "data": {"command": "git push origin feature"},
                        },
                        14,
                    ),
                    (
                        "command-fail",
                        "tool.completed",
                        {
                            "itemType": "command_execution",
                            "status": "failed",
                            "toolCallId": "failed-call",
                            "data": {"command": f"python --token {private}"},
                        },
                        15,
                    ),
                ]
                for activity_id, kind, payload, sequence in activities:
                    connection.execute(
                        "INSERT INTO projection_thread_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            activity_id,
                            "thread-1",
                            "turn-1",
                            kind,
                            json.dumps(payload),
                            f"2026-09-02T18:01:{sequence}.000Z",
                            sequence,
                        ),
                    )
                connection.execute(
                    "UPDATE projection_turns SET state = 'completed', completed_at = ? WHERE turn_id = ?",
                    ("2026-09-02T18:02:00Z", "turn-1"),
                )
                connection.commit()
                connection.close()

                batch = adapter.poll((target,))
                checkpoint_store.save_many(batch.checkpoints)
                replay = adapter.poll((target,))

        serialized = json.dumps(
            [event.to_wire() for _event_root, event in batch.events], sort_keys=True
        )
        self.assertEqual(baseline.events, ())
        self.assertEqual(
            {event.kind for _root, event in batch.events},
            {"test", "file", "push", "command", "session"},
        )
        self.assertIn(
            "Turn completed",
            {event.title for _root, event in batch.events},
        )
        self.assertNotIn(
            "Agent activity omitted",
            {event.title for _root, event in batch.events},
        )
        self.assertTrue(
            any(
                event.kind == "command"
                and event.status == "failed"
                and event.detail == "python"
                for _root, event in batch.events
            )
        )
        self.assertTrue(all(event_root == root for event_root, _event in batch.events))
        self.assertIn("safe.py", serialized)
        self.assertIn("fifth.py", serialized)
        self.assertNotIn(private, serialized)
        self.assertNotIn(os.fspath(outside), serialized)
        emitted = {event.source_event_id for _root, event in batch.events}
        replayed = {event.source_event_id for _root, event in replay.events}
        self.assertTrue(replayed)
        self.assertLessEqual(replayed, emitted)
