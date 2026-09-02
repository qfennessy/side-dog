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
    CodingAgentPollAdapter,
    OPENCODE_LISTING_CACHE,
    STATE_ENV,
    OpenCodeStream,
    active_agent_identities,
    append_event_once,
    events_path,
    git_repository_location,
    herdr_identities_for_root,
    is_config,
    latest_events,
    load_agent_identities,
    opencode_db_path,
    opencode_identities,
    poll_opencode_events,
)
from side_dog.integrations import SafeEvent
from side_dog.polling import CheckpointStore, PollErrorCode, PollTarget


def make_opencode_db(data_dir: Path) -> Path:
    """A minimal opencode store, with just the tables Side Dog reads."""
    db = data_dir / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL, "
        "title TEXT NOT NULL, model TEXT, agent TEXT, parent_id TEXT, "
        "time_updated INTEGER NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE part (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "data TEXT NOT NULL, time_created INTEGER NOT NULL, "
        "time_updated INTEGER NOT NULL)"
    )
    connection.commit()
    connection.close()
    return db


def insert_session(
    db: Path,
    session_id: str,
    directory: str,
    title: str,
    model: dict,
    *,
    agent: str = "build",
    parent_id: str | None = None,
    time_updated: int,
) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            directory,
            title,
            json.dumps(model),
            agent,
            parent_id,
            time_updated,
        ),
    )
    connection.commit()
    connection.close()


def insert_part(
    db: Path,
    part_id: str,
    session_id: str,
    data: dict,
    *,
    time_created: int,
    time_updated: int,
) -> None:
    connection = sqlite3.connect(db)
    connection.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
        (part_id, session_id, json.dumps(data), time_created, time_updated),
    )
    connection.commit()
    connection.close()


def tool_part(tool: str, state: dict, call_id: str = "call-1") -> dict:
    return {"type": "tool", "tool": tool, "callID": call_id, "state": state}


class OpenCodeIdentityTest(TestCase):
    def setUp(self) -> None:
        OPENCODE_LISTING_CACHE.clear()
        git_repository_location.cache_clear()

    tearDown = setUp

    def test_a_session_is_named_with_model_effort_and_task_title(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now_ms = int(time.time() * 1000)
            insert_session(
                db,
                "ses_opencode1",
                os.fspath(root),
                "Fix the widget",
                {"id": "deepseek-v4-pro", "providerID": "deepseek", "variant": "max"},
                time_updated=now_ms,
            )
            with patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}):
                identities = opencode_identities(root)

            self.assertEqual(list(identities), ["ses_opencode1"])
            identity = identities["ses_opencode1"]
            self.assertEqual(identity["agent"], "opencode")
            self.assertEqual(identity["model"], "deepseek-v4-pro")
            self.assertEqual(identity["effort"], "max")
            self.assertEqual(identity["label"], "Fix the widget")
            self.assertEqual(identity["status"], "working")
            self.assertEqual(identity["working_root"], os.fspath(root))

    def test_relative_data_home_is_not_opened_as_sqlite(self) -> None:
        with patch.dict(os.environ, {"XDG_DATA_HOME": "relative-data"}, clear=True):
            self.assertIsNone(opencode_db_path())

    def test_a_subagent_session_is_left_to_its_parent(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now_ms = int(time.time() * 1000)
            insert_session(
                db, "ses_parent", os.fspath(root), "The task",
                {"id": "m"}, time_updated=now_ms,
            )
            insert_session(
                db, "ses_child", os.fspath(root), "The task",
                {"id": "m"}, parent_id="ses_parent", time_updated=now_ms,
            )
            with patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}):
                identities = opencode_identities(root)

            self.assertEqual(list(identities), ["ses_parent"])

    def test_a_session_in_another_repository_is_left_alone(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            elsewhere = (Path(directory) / "other").resolve()
            root.mkdir()
            elsewhere.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now_ms = int(time.time() * 1000)
            insert_session(
                db, "ses_elsewhere", os.fspath(elsewhere), "Other task",
                {"id": "m"}, time_updated=now_ms,
            )
            repositories = {
                os.fspath(root): (os.fspath(root / ".git"), os.fspath(root)),
                os.fspath(elsewhere): (
                    os.fspath(elsewhere / ".git"),
                    os.fspath(elsewhere),
                ),
            }
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch(
                    "side_dog.cli.git_repository_location",
                    side_effect=lambda path: repositories.get(path, ("", "")),
                ),
            ):
                identities = opencode_identities(root)

            self.assertEqual(identities, {})

    def test_a_quiet_session_reads_as_idle_and_an_old_one_is_gone(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now_ms = int(time.time() * 1000)
            insert_session(
                db, "ses_quiet", os.fspath(root), "Quiet",
                {"id": "m"}, time_updated=now_ms - 300_000,
            )
            insert_session(
                db, "ses_old", os.fspath(root), "Old",
                {"id": "m"}, time_updated=now_ms - 1_000_000,
            )
            with patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}):
                identities = opencode_identities(root)

            self.assertEqual(list(identities), ["ses_quiet"])
            self.assertEqual(identities["ses_quiet"]["status"], "idle")

    def test_a_parent_stays_working_while_its_subagent_is(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now_ms = int(time.time() * 1000)
            insert_session(
                db, "ses_parent", os.fspath(root), "Parent",
                {"id": "m"}, time_updated=now_ms - 120_000,
            )
            insert_session(
                db, "ses_child", os.fspath(root), "Child",
                {"id": "m"}, parent_id="ses_parent", time_updated=now_ms,
            )
            with patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}):
                identities = opencode_identities(root)

            self.assertEqual(list(identities), ["ses_parent"])
            self.assertEqual(identities["ses_parent"]["status"], "working")

    def test_opencode_joins_the_agent_list_herdr_and_claude_share(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now_ms = int(time.time() * 1000)
            insert_session(
                db, "ses_opencode2", os.fspath(root), "A task",
                {"id": "deepseek-v4-pro"}, time_updated=now_ms,
            )
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch("side_dog.cli.load_herdr_identities", return_value={}),
                patch("side_dog.cli.claude_identities", return_value={}),
                patch("side_dog.cli.load_codex_session_identities", return_value={}),
            ):
                identities = load_agent_identities(root)

            sessions = [
                identity["session_id"]
                for identity in active_agent_identities(identities)
            ]
            self.assertEqual(sessions, ["ses_opencode2"])

    def test_herdr_reported_opencode_is_enriched_with_model_and_effort(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now_ms = int(time.time() * 1000)
            insert_session(
                db, "ses_opencode3", os.fspath(root), "A task",
                {"id": "deepseek-v4-pro", "variant": "max"}, time_updated=now_ms,
            )
            agent = {
                "agent": "opencode",
                "cwd": os.fspath(root),
                "pane_id": "w1:p1",
                "agent_status": "working",
                "terminal_title": "open",
                "agent_session": {"value": "ses_opencode3"},
            }
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch("side_dog.cli.git_worktree_root", return_value=""),
                patch("side_dog.cli.git_common_dir", return_value=""),
            ):
                identities = herdr_identities_for_root(root, [agent])

            identity = identities["ses_opencode3"]
            self.assertEqual(identity["model"], "deepseek-v4-pro")
            self.assertEqual(identity["effort"], "max")
            self.assertEqual(identity["pane_id"], "w1:p1")

    def test_opencode_config_files_are_recognized_as_config(self) -> None:
        self.assertTrue(is_config("opencode.json"))
        self.assertTrue(is_config("opencode.jsonc"))
        self.assertTrue(is_config("some/opencode.jsonc"))
        self.assertFalse(is_config("src/app.py"))


class OpenCodeIngestionTest(TestCase):
    def setUp(self) -> None:
        OPENCODE_LISTING_CACHE.clear()
        git_repository_location.cache_clear()

    tearDown = setUp

    def _ingest(
        self,
        root: Path,
        state: Path,
        data: Path,
        db: Path,
        session_id: str,
        parts: list[tuple[str, dict, int, int]],
    ) -> list[dict]:
        for part_id, part, created, updated in parts:
            insert_part(
                db, part_id, session_id, part,
                time_created=created, time_updated=updated,
            )
        identity = {
            session_id: {
                "agent": "opencode",
                "session_id": session_id,
                "root": os.fspath(root),
                "model": "deepseek-v4-pro",
            }
        }
        stream = OpenCodeStream(
            session_id=session_id,
            db_path=db,
            position=0,
            agent_root=os.fspath(root),
            model="deepseek-v4-pro",
        )
        with patch.dict(
            os.environ,
            {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
        ):
            poll_opencode_events(root, identity, {session_id: stream})
            return latest_events(events_path(root))

    def test_a_bash_test_and_a_file_edit_become_events(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "deepseek-v4-pro"}, time_updated=now,
            )
            events = self._ingest(
                root, state, data, db, session_id,
                [
                    (
                        "prt-test",
                        tool_part(
                            "bash",
                            {
                                "status": "completed",
                                "input": {"command": "python -m unittest"},
                                "metadata": {"exit": 0},
                            },
                        ),
                        now,
                        now,
                    ),
                    (
                        "prt-edit",
                        tool_part(
                            "edit",
                            {
                                "status": "completed",
                                "input": {"filePath": os.fspath(root / "cli.py")},
                            },
                            "call-2",
                        ),
                        now + 1,
                        now + 1,
                    ),
                ],
            )

            self.assertEqual(
                [event["title"] for event in events],
                ["Tests passed", "Wrote file"],
            )
            self.assertEqual(events[0]["agent"], "opencode")
            self.assertEqual(events[0]["model"], "deepseek-v4-pro")
            self.assertEqual(events[-1]["detail"], "cli.py")

    def test_a_running_bash_is_reported_then_completed(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "m"}, time_updated=now,
            )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            baseline = now - 1000
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                insert_part(
                    db, "prt-1", session_id,
                    tool_part("bash", {
                        "status": "running",
                        "input": {"command": "pytest"},
                        "metadata": {},
                    }),
                    time_created=now + 1, time_updated=now + 1,
                )
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                connection = sqlite3.connect(db)
                connection.execute(
                    "UPDATE part SET data = ?, time_updated = ? WHERE id = ?",
                    (
                        json.dumps(tool_part("bash", {
                            "status": "completed",
                            "input": {"command": "pytest"},
                            "metadata": {"exit": 0},
                        })),
                        now + 2,
                        "prt-1",
                    ),
                )
                connection.commit()
                connection.close()
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual(
                [event["status"] for event in events], ["running", "success"]
            )
            self.assertEqual(
                [event["title"] for event in events],
                ["Running tests", "Tests passed"],
            )
            self.assertEqual(events[0]["operation_id"], events[1]["operation_id"])

    def test_a_subagent_session_activity_is_attributed_to_the_parent(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now = int(time.time() * 1000)
            baseline = now - 1000
            parent_id = "ses_parent"
            child_id = "ses_child"
            insert_session(
                db, parent_id, os.fspath(root), "Parent task",
                {"id": "deepseek-v4-pro"}, time_updated=now,
            )
            insert_session(
                db, child_id, os.fspath(root), "Subagent",
                {"id": "deepseek-v4-pro"}, parent_id=parent_id, time_updated=now,
            )
            insert_part(
                db, "prt-child-edit", child_id,
                tool_part("edit", {
                    "status": "completed",
                    "input": {"filePath": os.fspath(root / "a.py")},
                }, "call-child"),
                time_created=now + 1, time_updated=now + 1,
            )
            identity = {
                parent_id: {
                    "agent": "opencode",
                    "session_id": parent_id,
                    "root": os.fspath(root),
                    "model": "deepseek-v4-pro",
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual([event["title"] for event in events], ["Wrote file"])
            # The subagent has no banner; its work is attributed to the parent.
            self.assertEqual(events[0]["session_id"], parent_id)
            self.assertEqual(events[0]["agent"], "opencode")
            self.assertIn(child_id, streams)

    def test_a_grandchild_subagent_is_traversed_and_attributed_to_the_parent(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            now = int(time.time() * 1000)
            baseline = now - 1000
            parent_id = "ses_parent"
            child_id = "ses_child"
            grandchild_id = "ses_grandchild"
            insert_session(
                db, parent_id, os.fspath(root), "Parent",
                {"id": "deepseek-v4-pro"}, time_updated=now,
            )
            insert_session(
                db, child_id, os.fspath(root), "Child",
                {"id": "m"}, parent_id=parent_id, time_updated=now,
            )
            insert_session(
                db, grandchild_id, os.fspath(root), "Grandchild",
                {"id": "m"}, parent_id=child_id, time_updated=now,
            )
            insert_part(
                db, "prt-grandchild", grandchild_id,
                tool_part("edit", {
                    "status": "completed",
                    "input": {"filePath": os.fspath(root / "a.py")},
                }, "call-gc"),
                time_created=now + 1, time_updated=now + 1,
            )
            identity = {
                parent_id: {
                    "agent": "opencode",
                    "session_id": parent_id,
                    "root": os.fspath(root),
                    "model": "deepseek-v4-pro",
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual([event["title"] for event in events], ["Wrote file"])
            self.assertEqual(events[0]["session_id"], parent_id)
            self.assertIn(grandchild_id, streams)

    def test_a_step_finish_with_stop_closes_the_turn(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            baseline = now - 1000
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "m"}, time_updated=now,
            )
            insert_part(
                db, "prt-stop", session_id,
                {"type": "step-finish", "reason": "stop", "tokens": {"total": 1}},
                time_created=now + 1, time_updated=now + 1,
            )
            insert_part(
                db, "prt-tools", session_id,
                {"type": "step-finish", "reason": "tool-calls"},
                time_created=now + 2, time_updated=now + 2,
            )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual(
                [event["title"] for event in events], ["Opencode turn finished"]
            )
            self.assertEqual(events[0]["kind"], "session")

    def test_tool_start_times_come_from_the_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            baseline = now - 1000
            started = now - 500
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "m"}, time_updated=now,
            )
            insert_part(
                db, "prt-timed", session_id,
                tool_part("bash", {
                    "status": "completed",
                    "input": {"command": "pytest"},
                    "metadata": {"exit": 0},
                    "time": {"start": started, "end": now},
                }),
                time_created=started, time_updated=now,
            )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual(events[0]["started_epoch_ms"], started)

    def test_unchanged_cursor_rows_are_not_reprocessed(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            baseline = now - 1000
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "m"}, time_updated=now,
            )
            insert_part(
                db, "prt-edit", session_id,
                tool_part("edit", {
                    "status": "completed",
                    "input": {"filePath": os.fspath(root / "a.py")},
                }, "call-edit"),
                time_created=now + 1, time_updated=now + 1,
            )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                with patch(
                    "side_dog.cli.git_line_changes", return_value=(1, 0)
                ) as changes:
                    poll_opencode_events(
                        root, identity, streams, baseline_ms=baseline
                    )
                    second = poll_opencode_events(
                        root, identity, streams, baseline_ms=baseline
                    )
                events = latest_events(events_path(root))

            self.assertEqual(second, 0)
            self.assertEqual(len(events), 1)
            # The idle edit was not reprocessed, so git diff never re-ran.
            self.assertEqual(changes.call_count, 0)

    def test_context_tools_emit_lightweight_markers(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            baseline = now - 1000
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "m"}, time_updated=now,
            )
            parts = [
                ("prt-read", tool_part("read", {
                    "status": "completed",
                    "input": {"filePath": os.fspath(root / "cli.py")},
                }, "call-read")),
                ("prt-grep", tool_part("grep", {
                    "status": "completed",
                    "input": {"pattern": "findFoo"},
                }, "call-grep")),
                ("prt-glob", tool_part("glob", {
                    "status": "completed",
                    "input": {"pattern": "**/*.py"},
                }, "call-glob")),
                ("prt-web", tool_part("webfetch", {
                    "status": "completed",
                    "input": {"url": "https://example.com/docs"},
                }, "call-web")),
                ("prt-todo", tool_part("todowrite", {
                    "status": "completed",
                    "input": {"todos": [
                        {"content": "Do the thing", "status": "in_progress"},
                    ]},
                }, "call-todo")),
            ]
            for index, (part_id, part) in enumerate(parts):
                insert_part(
                    db, part_id, session_id, part,
                    time_created=now + 1 + index,
                    time_updated=now + 1 + index,
                )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual(
                [event["title"] for event in events],
                [
                    "Read file",
                    "Searched code",
                    "Searched files",
                    "Fetched web page",
                    "Todo updated",
                ],
            )
            self.assertEqual(events[0]["detail"], "cli.py")
            self.assertEqual(events[1]["detail"], "code")
            self.assertEqual(events[2]["detail"], "files")
            self.assertEqual(events[3]["detail"], "web page")
            self.assertEqual(events[4]["detail"], "1 task")
            self.assertEqual(
                {event["kind"] for event in events}, {"search", "todo"}
            )
            # Context-gathering tools carry a marker, never their output.
            self.assertNotIn("Do the thing", json.dumps(events))

    def test_a_new_stream_starts_at_the_watcher_baseline(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "m"}, time_updated=now,
            )
            insert_part(
                db, "prt-old", session_id,
                tool_part("bash", {
                    "status": "completed",
                    "input": {"command": "pytest"},
                    "metadata": {"exit": 0},
                }),
                time_created=now - 5000, time_updated=now - 5000,
            )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                # First attach: the pre-baseline part stays out.
                poll_opencode_events(root, identity, streams, baseline_ms=now)
                events = latest_events(events_path(root))

            self.assertEqual(events, [])
            self.assertEqual(streams[session_id].position, now)

    def test_a_session_discovered_later_keeps_its_recent_activity(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_late"
            baseline = int(time.time() * 1000)
            insert_session(
                db, session_id, os.fspath(root), "late",
                {"id": "m"}, time_updated=baseline + 2000,
            )
            # A quick edit right after the watcher started, before discovery.
            insert_part(
                db, "prt-edit", session_id,
                tool_part("edit", {
                    "status": "completed",
                    "input": {"filePath": os.fspath(root / "a.py")},
                }, "call-edit"),
                time_created=baseline + 1000, time_updated=baseline + 1000,
            )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                # First poll: the session is not yet in the identity list.
                poll_opencode_events(root, {}, streams, baseline_ms=baseline)
                # Later poll: the session is discovered, and its recent edit
                # is still captured because the stream starts at the baseline.
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual([event["title"] for event in events], ["Wrote file"])
            self.assertEqual(streams[session_id].position, baseline + 1000)

    def test_equal_timestamp_updates_are_not_lost(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_ingest"
            now = int(time.time() * 1000)
            baseline = now - 1000
            insert_session(
                db, session_id, os.fspath(root), "do work",
                {"id": "m"}, time_updated=now,
            )
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                insert_part(
                    db, "prt-a", session_id,
                    tool_part("bash", {
                        "status": "completed",
                        "input": {"command": "pytest"},
                        "metadata": {"exit": 0},
                    }),
                    time_created=now + 1, time_updated=now + 1,
                )
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                # A second part sharing the same millisecond as the cursor.
                insert_part(
                    db, "prt-b", session_id,
                    tool_part("edit", {
                        "status": "completed",
                        "input": {"filePath": os.fspath(root / "a.py")},
                    }, "call-b"),
                    time_created=now + 1, time_updated=now + 1,
                )
                poll_opencode_events(root, identity, streams, baseline_ms=baseline)
                events = latest_events(events_path(root))

            self.assertEqual(
                [event["title"] for event in events],
                ["Tests passed", "Wrote file"],
            )

    def test_shared_adapter_uses_one_connection_for_all_watched_roots(self) -> None:
        with TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            roots = tuple(
                (Path(directory) / name).resolve() for name in ("first", "second")
            )
            now = int(time.time() * 1000)
            targets = []
            for index, root in enumerate(roots):
                root.mkdir()
                session_id = f"ses_shared_{index}"
                insert_session(
                    db,
                    session_id,
                    os.fspath(root),
                    "work",
                    {"id": "model"},
                    time_updated=now + 10_000,
                )
                insert_part(
                    db,
                    f"part-{index}",
                    session_id,
                    tool_part(
                        "edit",
                        {
                            "status": "completed",
                            "input": {"filePath": os.fspath(root / "app.py")},
                        },
                    ),
                    time_created=now + 10_000,
                    time_updated=now + 10_000,
                )
                targets.append(
                    PollTarget.from_wire(
                        root,
                        {
                            session_id: {
                                "agent": "opencode",
                                "session_id": session_id,
                                "root": os.fspath(root),
                                "working_root": os.fspath(root),
                            }
                        },
                    )
                )
            adapter = CodingAgentPollAdapter("opencode")
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch("side_dog.cli.sqlite3.connect", wraps=sqlite3.connect) as connect,
            ):
                batch = adapter.poll(tuple(targets))

            self.assertEqual(connect.call_count, 1)
            self.assertEqual(len(batch.events), 2)
            self.assertTrue(
                all(isinstance(event, SafeEvent) for _root, event in batch.events)
            )
            self.assertEqual(
                {
                    stream.session_id
                    for streams in adapter._opencode.values()
                    for stream in streams.values()
                },
                {"ses_shared_0", "ses_shared_1"},
            )

    def test_more_than_processed_limit_at_one_timestamp_settles(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_large_cursor"
            now = int(time.time() * 1000)
            updated = now + 1
            insert_session(
                db,
                session_id,
                os.fspath(root),
                "large cursor",
                {"id": "model"},
                time_updated=updated,
            )
            record_count = 4_200
            connection = sqlite3.connect(db)
            connection.executemany(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        f"part-{index:05d}",
                        session_id,
                        json.dumps({"type": "text"}),
                        updated,
                        updated,
                    )
                    for index in range(record_count)
                ),
            )
            connection.commit()
            connection.close()
            identity = {
                session_id: {
                    "agent": "opencode",
                    "session_id": session_id,
                    "root": os.fspath(root),
                }
            }
            streams: dict[str, OpenCodeStream] = {}
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch("side_dog.cli._poll_opencode_part", return_value=0) as part,
            ):
                poll_opencode_events(root, identity, streams, baseline_ms=now)
                poll_opencode_events(root, identity, streams, baseline_ms=now)

            self.assertEqual(part.call_count, record_count)
            self.assertEqual(len(streams[session_id].processed), 4_096)
            self.assertTrue(streams[session_id].cursor_signature)

    def test_adapter_reports_parse_and_sqlite_health_without_private_text(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_bad_json"
            now = int(time.time() * 1000)
            updated = now + 10_000
            insert_session(
                db,
                session_id,
                os.fspath(root),
                "parse",
                {"id": "model"},
                time_updated=updated,
            )
            connection = sqlite3.connect(db)
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
                ("bad-part", session_id, "{PRIVATE QUERY", updated, updated),
            )
            connection.commit()
            connection.close()
            target = PollTarget.from_wire(
                root,
                {
                    session_id: {
                        "agent": "opencode",
                        "session_id": session_id,
                        "root": os.fspath(root),
                    }
                },
            )
            with patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}):
                parse_batch = CodingAgentPollAdapter("opencode").poll((target,))

            self.assertEqual(parse_batch.stats.parse_errors, 1)
            self.assertEqual(parse_batch.stats.last_error, PollErrorCode.PARSE)
            self.assertNotIn("PRIVATE QUERY", repr(parse_batch.stats))

            broken_data = Path(directory) / "broken-data"
            broken_db = broken_data / "opencode" / "opencode.db"
            broken_db.parent.mkdir(parents=True)
            sqlite3.connect(broken_db).close()
            OPENCODE_LISTING_CACHE.clear()
            with patch.dict(
                os.environ, {"XDG_DATA_HOME": os.fspath(broken_data)}
            ):
                sqlite_batch = CodingAgentPollAdapter("opencode").poll((target,))

            self.assertEqual(sqlite_batch.stats.parse_errors, 0)
            self.assertEqual(sqlite_batch.stats.last_error, PollErrorCode.SQLITE)
            self.assertEqual(sqlite_batch.checkpoints, ())
            self.assertNotIn(os.fspath(broken_db), repr(sqlite_batch.stats))

    def test_discarded_batches_resume_from_last_applied_opencode_checkpoint(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_restart"
            baseline = int(time.time() * 1_000)
            insert_session(
                db,
                session_id,
                os.fspath(root),
                "restart",
                {"id": "model"},
                time_updated=baseline + 1,
            )
            insert_part(
                db,
                "part-first",
                session_id,
                tool_part(
                    "edit",
                    {
                        "status": "completed",
                        "input": {"filePath": os.fspath(root / "first.py")},
                    },
                ),
                time_created=baseline + 1,
                time_updated=baseline + 1,
            )
            target = PollTarget.from_wire(
                root,
                {
                    session_id: {
                        "agent": "opencode",
                        "session_id": session_id,
                        "root": os.fspath(root),
                    }
                },
            )
            store = CheckpointStore(lambda _root: state / "checkpoints.sqlite3")

            def poll_new_adapter():
                adapter = CodingAgentPollAdapter(
                    "opencode", checkpoint_store=store
                )
                return adapter.poll((target,))

            with (
                patch.dict(
                    os.environ,
                    {"XDG_DATA_HOME": os.fspath(data), STATE_ENV: os.fspath(state)},
                ),
                patch("side_dog.cli.time.time", return_value=baseline / 1_000),
            ):
                with patch(
                    "side_dog.cli._open_opencode_source",
                    side_effect=sqlite3.OperationalError("PRIVATE locked path"),
                ):
                    first_discarded = poll_new_adapter()
                self.assertEqual(
                    first_discarded.stats.last_error, PollErrorCode.SQLITE
                )
                self.assertEqual(first_discarded.checkpoints, ())
                durable = store.load(
                    root,
                    "opencode:watch-baseline",
                    "opencode:root-baseline-v1",
                )
                self.assertEqual(durable.position, baseline)  # type: ignore[union-attr]

                recovered = poll_new_adapter()
                self.assertEqual(
                    [event.detail for _event_root, event in recovered.events],
                    ["first.py"],
                )
                for event_root, event in recovered.events:
                    self.assertTrue(append_event_once(event_root, event))
                store.save_many(recovered.checkpoints)

                insert_part(
                    db,
                    "part-later",
                    session_id,
                    tool_part(
                        "edit",
                        {
                            "status": "completed",
                            "input": {"filePath": os.fspath(root / "later.py")},
                        },
                    ),
                    time_created=baseline + 2,
                    time_updated=baseline + 2,
                )
                later_discarded = poll_new_adapter()
                later_recovered = poll_new_adapter()
                self.assertEqual(
                    [event.detail for _root, event in later_discarded.events],
                    ["first.py", "later.py"],
                )
                self.assertEqual(
                    [
                        event.source_event_id
                        for _root, event in later_recovered.events
                    ],
                    [
                        event.source_event_id
                        for _root, event in later_discarded.events
                    ],
                )
                appended = [
                    append_event_once(event_root, event)
                    for event_root, event in later_recovered.events
                ]
                self.assertEqual(appended, [False, True])
                store.save_many(later_recovered.checkpoints)

            with patch.dict(os.environ, {STATE_ENV: os.fspath(state)}):
                events = latest_events(events_path(root))
            self.assertEqual(
                [event["detail"] for event in events],
                ["first.py", "later.py"],
            )
            durable = store.load(
                root, f"opencode:{session_id}", "opencode:parts-v1"
            )
            self.assertEqual(durable.position, baseline + 2)  # type: ignore[union-attr]
            self.assertNotIn(os.fspath(db), repr(recovered.checkpoints))

    def test_checkpoint_failure_keeps_bounded_no_backfill_baseline(self) -> None:
        class BrokenStore:
            def load(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("PRIVATE checkpoint path")

        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            session_id = "ses_no_backfill"
            baseline = int(time.time() * 1_000)
            insert_session(
                db,
                session_id,
                os.fspath(root),
                "old",
                {"id": "model"},
                time_updated=baseline - 1_000,
            )
            insert_part(
                db,
                "part-old",
                session_id,
                tool_part(
                    "edit",
                    {
                        "status": "completed",
                        "input": {"filePath": os.fspath(root / "old.py")},
                    },
                ),
                time_created=baseline - 1_000,
                time_updated=baseline - 1_000,
            )
            target = PollTarget.from_wire(
                root,
                {
                    session_id: {
                        "agent": "opencode",
                        "session_id": session_id,
                        "root": os.fspath(root),
                    }
                },
            )
            adapter = CodingAgentPollAdapter(
                "opencode", checkpoint_store=BrokenStore()  # type: ignore[arg-type]
            )
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch("side_dog.cli.time.time", return_value=baseline / 1_000),
            ):
                batch = adapter.poll((target,))

            self.assertEqual(batch.events, ())
            self.assertEqual(batch.stats.last_error, PollErrorCode.SQLITE)
            self.assertEqual(
                adapter._opencode[root][session_id].position, baseline
            )
            self.assertNotIn("PRIVATE checkpoint", repr(batch.stats))

    def test_empty_root_baseline_recovers_its_first_session(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            baseline = int(time.time() * 1_000)
            store = CheckpointStore(lambda _root: state / "checkpoints.sqlite3")
            empty_target = PollTarget.from_wire(root, {})
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch("side_dog.cli.time.time", return_value=baseline / 1_000),
            ):
                empty_batch = CodingAgentPollAdapter(
                    "opencode", checkpoint_store=store
                ).poll((empty_target,))

            seeded = store.load(
                root,
                "opencode:watch-baseline",
                "opencode:root-baseline-v1",
            )
            self.assertEqual(seeded.position, baseline)  # type: ignore[union-attr]
            self.assertEqual(empty_batch.checkpoints, ())
            store.save_many(empty_batch.checkpoints)

            session_id = "ses_first"
            insert_session(
                db,
                session_id,
                os.fspath(root),
                "first",
                {"id": "model"},
                time_updated=baseline + 1_000,
            )
            insert_part(
                db,
                "part-first",
                session_id,
                tool_part(
                    "edit",
                    {
                        "status": "completed",
                        "input": {"filePath": os.fspath(root / "first.py")},
                    },
                ),
                time_created=baseline + 1_000,
                time_updated=baseline + 1_000,
            )
            target = PollTarget.from_wire(
                root,
                {
                    session_id: {
                        "agent": "opencode",
                        "session_id": session_id,
                        "root": os.fspath(root),
                    }
                },
            )
            with (
                patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                patch(
                    "side_dog.cli.time.time",
                    return_value=(baseline + 2_000) / 1_000,
                ),
            ):
                recovered = CodingAgentPollAdapter(
                    "opencode", checkpoint_store=store
                ).poll((target,))

            self.assertEqual(
                [event.detail for _root, event in recovered.events], ["first.py"]
            )

    def test_cached_listing_cannot_advance_past_new_child_session(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            baseline = int(time.time() * 1_000)
            parent_id = "ses_parent_restart"
            insert_session(
                db,
                parent_id,
                os.fspath(root),
                "parent",
                {"id": "model"},
                time_updated=baseline,
            )
            target = PollTarget.from_wire(
                root,
                {
                    parent_id: {
                        "agent": "opencode",
                        "session_id": parent_id,
                        "root": os.fspath(root),
                    }
                },
            )
            store = CheckpointStore(lambda _root: state / "checkpoints.sqlite3")

            def poll_at(epoch_ms: int):
                with (
                    patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                    patch(
                        "side_dog.cli.time.time", return_value=epoch_ms / 1_000
                    ),
                    patch("side_dog.cli.time.monotonic", return_value=100.0),
                ):
                    return CodingAgentPollAdapter(
                        "opencode", checkpoint_store=store
                    ).poll((target,))

            first = poll_at(baseline)
            store.save_many(first.checkpoints)

            child_id = "ses_child_restart"
            insert_session(
                db,
                child_id,
                os.fspath(root),
                "child",
                {"id": "model"},
                parent_id=parent_id,
                time_updated=baseline + 1_000,
            )
            insert_part(
                db,
                "part-child",
                child_id,
                tool_part(
                    "edit",
                    {
                        "status": "completed",
                        "input": {"filePath": os.fspath(root / "child.py")},
                    },
                ),
                time_created=baseline + 1_000,
                time_updated=baseline + 1_000,
            )
            cached = poll_at(baseline + 2_000)
            self.assertEqual(cached.events, ())
            self.assertNotIn(
                "opencode:root-baseline-v1",
                [checkpoint.source for _root, checkpoint in cached.checkpoints],
            )
            store.save_many(cached.checkpoints)

            OPENCODE_LISTING_CACHE.clear()
            recovered = poll_at(baseline + 3_000)
            self.assertEqual(
                [event.detail for _root, event in recovered.events], ["child.py"]
            )

    def test_fresh_listing_waits_for_an_unowned_top_level_session(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            root.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            baseline = int(time.time() * 1_000)
            known_id = "ses_known"
            waiting_id = "ses_waiting"
            insert_session(
                db,
                known_id,
                os.fspath(root),
                "known",
                {"id": "model"},
                time_updated=baseline,
            )
            store = CheckpointStore(lambda _root: state / "checkpoints.sqlite3")

            def target(*session_ids: str) -> PollTarget:
                return PollTarget.from_wire(
                    root,
                    {
                        session_id: {
                            "agent": "opencode",
                            "session_id": session_id,
                            "root": os.fspath(root),
                        }
                        for session_id in session_ids
                    },
                )

            def poll_at(epoch_ms: int, *session_ids: str):
                with (
                    patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                    patch(
                        "side_dog.cli.time.time", return_value=epoch_ms / 1_000
                    ),
                ):
                    return CodingAgentPollAdapter(
                        "opencode", checkpoint_store=store
                    ).poll((target(*session_ids),))

            first = poll_at(baseline, known_id)
            store.save_many(first.checkpoints)
            insert_session(
                db,
                waiting_id,
                os.fspath(root),
                "waiting",
                {"id": "model"},
                time_updated=baseline + 1_000,
            )
            insert_part(
                db,
                "part-waiting",
                waiting_id,
                tool_part(
                    "edit",
                    {
                        "status": "completed",
                        "input": {"filePath": os.fspath(root / "waiting.py")},
                    },
                ),
                time_created=baseline + 1_000,
                time_updated=baseline + 1_000,
            )

            OPENCODE_LISTING_CACHE.clear()
            incomplete = poll_at(baseline + 2_000, known_id)
            self.assertNotIn(
                "opencode:root-baseline-v1",
                [checkpoint.source for _root, checkpoint in incomplete.checkpoints],
            )
            store.save_many(incomplete.checkpoints)

            OPENCODE_LISTING_CACHE.clear()
            recovered = poll_at(baseline + 3_000, known_id, waiting_id)
            self.assertEqual(
                [event.detail for _root, event in recovered.events], ["waiting.py"]
            )

    def test_fresh_listing_waits_for_an_unowned_sibling_worktree(self) -> None:
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "project").resolve()
            sibling = (Path(directory) / "sibling").resolve()
            root.mkdir()
            sibling.mkdir()
            state = Path(directory) / "state"
            data = Path(directory) / "data"
            db = make_opencode_db(data)
            baseline = int(time.time() * 1_000)
            known_id = "ses_known_worktree"
            waiting_id = "ses_waiting_worktree"
            common = os.fspath(Path(directory) / "repo.git")
            repositories = {
                os.fspath(root): (common, os.fspath(root)),
                os.fspath(sibling): (common, os.fspath(sibling)),
            }
            insert_session(
                db,
                known_id,
                os.fspath(root),
                "known",
                {"id": "model"},
                time_updated=baseline,
            )
            store = CheckpointStore(lambda _root: state / "checkpoints.sqlite3")

            def target(*session_ids: str) -> PollTarget:
                return PollTarget.from_wire(
                    root,
                    {
                        session_id: {
                            "agent": "opencode",
                            "session_id": session_id,
                            "root": os.fspath(
                                sibling if session_id == waiting_id else root
                            ),
                        }
                        for session_id in session_ids
                    },
                )

            def poll_at(epoch_ms: int, *session_ids: str):
                with (
                    patch.dict(os.environ, {"XDG_DATA_HOME": os.fspath(data)}),
                    patch(
                        "side_dog.cli.time.time", return_value=epoch_ms / 1_000
                    ),
                    patch(
                        "side_dog.cli.git_repository_location",
                        side_effect=lambda path: repositories.get(path, ("", "")),
                    ),
                ):
                    return CodingAgentPollAdapter(
                        "opencode", checkpoint_store=store
                    ).poll((target(*session_ids),))

            first = poll_at(baseline, known_id)
            store.save_many(first.checkpoints)
            insert_session(
                db,
                waiting_id,
                os.fspath(sibling),
                "waiting",
                {"id": "model"},
                time_updated=baseline + 1_000,
            )
            insert_part(
                db,
                "part-waiting-worktree",
                waiting_id,
                {"type": "step-finish", "reason": "stop"},
                time_created=baseline + 1_000,
                time_updated=baseline + 1_000,
            )

            OPENCODE_LISTING_CACHE.clear()
            incomplete = poll_at(baseline + 2_000, known_id)
            self.assertNotIn(
                "opencode:root-baseline-v1",
                [checkpoint.source for _root, checkpoint in incomplete.checkpoints],
            )
            store.save_many(incomplete.checkpoints)

            OPENCODE_LISTING_CACHE.clear()
            recovered = poll_at(baseline + 3_000, known_id, waiting_id)
            self.assertEqual(
                [event.title for _root, event in recovered.events],
                ["Opencode turn finished"],
            )
