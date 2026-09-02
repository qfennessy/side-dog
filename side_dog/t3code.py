"""Narrow, read-only access to T3 Code's projected local state.

T3 Code is a launch surface, not a coding-agent identity.  This module reads
only projection metadata and explicitly extracted tool fields.  It never
queries messages, the raw orchestration event store, provider logs, or whole
activity payloads.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping


T3CODE_HOME_ENV = "T3CODE_HOME"
T3CODE_ACTIVITY_SOURCE = "t3code:projected-activity"
T3CODE_TURN_SOURCE = "t3code:projected-turns"
T3CODE_SESSION_LIMIT = 256

_REQUIRED_SCHEMA = {
    "projection_projects": {"project_id", "workspace_root"},
    "projection_threads": {
        "thread_id",
        "project_id",
        "title",
        "worktree_path",
        "model_selection_json",
        "updated_at",
        "deleted_at",
    },
    "projection_thread_sessions": {
        "thread_id",
        "status",
        "provider_name",
        "active_turn_id",
        "updated_at",
    },
    "provider_session_runtime": {
        "thread_id",
        "provider_name",
        "status",
        "last_seen_at",
        "resume_cursor_json",
    },
    "projection_thread_activities": {
        "activity_id",
        "thread_id",
        "turn_id",
        "kind",
        "payload_json",
        "created_at",
        "sequence",
    },
    "projection_turns": {
        "row_id",
        "thread_id",
        "turn_id",
        "state",
        "completed_at",
    },
}

_PROVIDER_ALIASES = {
    "claudeagent": "claude-code",
    "claude-code": "claude-code",
    "claude": "claude-code",
    "codex": "codex",
    "cursor": "cursor",
    "grok": "grok",
    "grok-build": "grok",
    "opencode": "opencode",
}


@dataclass(frozen=True, slots=True)
class T3CodeSession:
    thread_id: str
    provider: str
    native_session_id: str
    title: str
    workspace_root: str
    working_root: str
    status: str
    model: str
    effort: str
    updated_epoch_ms: int


@dataclass(frozen=True, slots=True)
class T3CodePollRequest:
    thread_id: str
    activity_position: int | None
    turn_position: int | None


@dataclass(frozen=True, slots=True)
class T3CodePollRow:
    row_type: str
    source_id: str
    thread_id: str
    turn_id: str
    kind: str
    created_at: str
    sequence: int | None
    maximum_position: int
    minimum_open_turn: int | None
    item_type: str
    status: str
    tool_call_id: str
    command: str
    paths: tuple[str, ...]


def t3code_home(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    configured = values.get(T3CODE_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    home = values.get("HOME")
    return (Path(home).expanduser() if home else Path.home()) / ".t3"


def t3code_database_path(environment: Mapping[str, str] | None = None) -> Path:
    return t3code_home(environment) / "userdata" / "state.sqlite"


def open_t3code_database(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve(strict=False).as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def t3code_schema_ready(connection: sqlite3.Connection) -> bool:
    for table, required in _REQUIRED_SCHEMA.items():
        columns = {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            if len(row) > 1
        }
        if not required <= columns:
            return False
    return True


def normalize_t3code_provider(value: object) -> str:
    text = str(value or "").strip().casefold().replace("_", "-")
    return _PROVIDER_ALIASES.get(text, "")


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _epoch_ms(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (OverflowError, ValueError):
        return 0


def read_t3code_sessions(path: Path) -> list[T3CodeSession]:
    connection = open_t3code_database(path)
    try:
        if not t3code_schema_ready(connection):
            raise ValueError("unsupported T3 Code schema")
        rows = connection.execute(
            """
            SELECT
              threads.thread_id,
              COALESCE(runtime.provider_name, sessions.provider_name, ''),
              CASE
                WHEN COALESCE(runtime.provider_name, sessions.provider_name, '') = 'codex'
                  THEN json_extract(runtime.resume_cursor_json, '$.threadId')
                WHEN lower(COALESCE(runtime.provider_name, sessions.provider_name, ''))
                     IN ('claudeagent', 'claude', 'claude-code')
                  THEN COALESCE(
                    json_extract(runtime.resume_cursor_json, '$.resume'),
                    json_extract(runtime.resume_cursor_json, '$.sessionId')
                  )
                ELSE json_extract(runtime.resume_cursor_json, '$.sessionId')
              END,
              threads.title,
              projects.workspace_root,
              COALESCE(NULLIF(threads.worktree_path, ''), projects.workspace_root),
              sessions.active_turn_id,
              COALESCE(runtime.status, sessions.status, ''),
              json_extract(threads.model_selection_json, '$.model'),
              COALESCE(
                CASE
                WHEN json_type(threads.model_selection_json, '$.options') = 'array'
                THEN (
                  SELECT json_extract(option.value, '$.value')
                  FROM json_each(
                    threads.model_selection_json,
                    '$.options'
                  ) AS option
                  WHERE json_extract(option.value, '$.id')
                    IN ('reasoningEffort', 'reasoning_effort', 'effort')
                  LIMIT 1
                ) END,
                json_extract(threads.model_selection_json, '$.options.reasoningEffort'),
                json_extract(threads.model_selection_json, '$.options.reasoning_effort'),
                json_extract(threads.model_selection_json, '$.options.effort')
              ),
              COALESCE(runtime.last_seen_at, sessions.updated_at, threads.updated_at)
            FROM projection_threads AS threads
            JOIN projection_projects AS projects
              ON projects.project_id = threads.project_id
            LEFT JOIN projection_thread_sessions AS sessions
              ON sessions.thread_id = threads.thread_id
            LEFT JOIN provider_session_runtime AS runtime
              ON runtime.thread_id = threads.thread_id
            WHERE threads.deleted_at IS NULL
            ORDER BY COALESCE(runtime.last_seen_at, sessions.updated_at, threads.updated_at) DESC
            LIMIT ?
            """,
            (T3CODE_SESSION_LIMIT,),
        ).fetchall()
    finally:
        connection.close()

    sessions: list[T3CodeSession] = []
    for row in rows:
        provider = normalize_t3code_provider(row[1])
        thread_id = _text(row[0]).strip()
        workspace_root = _text(row[4]).strip()
        working_root = _text(row[5]).strip() or workspace_root
        if not provider or not thread_id or not working_root:
            continue
        runtime_status = _text(row[7]).casefold()
        if _text(row[6]):
            status = "working"
        elif runtime_status in {"starting", "connecting", "running"}:
            status = "working"
        elif runtime_status in {"error", "failed"}:
            status = "blocked"
        elif runtime_status in {"stopped", "completed", "exited"}:
            status = "done"
        elif runtime_status in {"ready", "idle"}:
            status = "idle"
        else:
            status = "unknown"
        sessions.append(
            T3CodeSession(
                thread_id=thread_id,
                provider=provider,
                native_session_id=_text(row[2]).strip(),
                title=_text(row[3]).strip(),
                workspace_root=workspace_root,
                working_root=working_root,
                status=status,
                model=_text(row[8]).strip(),
                effort=_text(row[9]).strip(),
                updated_epoch_ms=_epoch_ms(row[10]),
            )
        )
    return sessions


def read_t3code_poll_rows(
    path: Path, requests: tuple[T3CodePollRequest, ...]
) -> list[T3CodePollRow]:
    if not requests:
        return []
    values = ",".join("(?, ?, ?)" for _request in requests)
    parameters: list[object] = []
    for request in requests:
        parameters.extend(
            (
                request.thread_id,
                -1 if request.activity_position is None else request.activity_position,
                -1 if request.turn_position is None else request.turn_position,
            )
        )
    query = f"""
        WITH requested(thread_id, activity_position, turn_position) AS (
          VALUES {values}
        )
        SELECT
          'activity',
          activities.activity_id,
          requested.thread_id,
          activities.turn_id,
          activities.kind,
          activities.created_at,
          activities.sequence,
          COALESCE((
            SELECT MAX(all_activity.sequence)
            FROM projection_thread_activities AS all_activity
            WHERE all_activity.thread_id = requested.thread_id
          ), 0),
          NULL,
          json_extract(activities.payload_json, '$.itemType'),
          COALESCE(
            json_extract(activities.payload_json, '$.status'),
            json_extract(activities.payload_json, '$.data.item.status')
          ),
          COALESCE(
            json_extract(activities.payload_json, '$.toolCallId'),
            json_extract(activities.payload_json, '$.data.toolCallId')
          ),
          COALESCE(
            json_extract(activities.payload_json, '$.data.command'),
            json_extract(activities.payload_json, '$.data.item.command')
          ),
          COALESCE(
            json_extract(activities.payload_json, '$.data.path'),
            json_extract(activities.payload_json, '$.data.filePath'),
            json_extract(activities.payload_json, '$.data.file_path')
          ),
          (
            SELECT json_group_array(json_extract(file.value, '$.path'))
            FROM json_each(activities.payload_json, '$.data.files') AS file
            WHERE json_type(file.value, '$.path') = 'text'
          )
        FROM requested
        LEFT JOIN projection_thread_activities AS activities
          ON activities.thread_id = requested.thread_id
         AND requested.activity_position >= 0
         AND activities.sequence >= requested.activity_position
         AND activities.kind IN ('tool.started', 'tool.updated', 'tool.completed')

        UNION ALL

        SELECT
          'turn',
          CASE WHEN turns.row_id IS NULL THEN NULL ELSE 'turn:' || turns.row_id END,
          requested.thread_id,
          turns.turn_id,
          'turn.completed',
          turns.completed_at,
          turns.row_id,
          COALESCE((
            SELECT MAX(all_turns.row_id)
            FROM projection_turns AS all_turns
            WHERE all_turns.thread_id = requested.thread_id
          ), 0),
          (
            SELECT MIN(open_turn.row_id)
            FROM projection_turns AS open_turn
            WHERE open_turn.thread_id = requested.thread_id
              AND open_turn.completed_at IS NULL
          ),
          NULL, NULL, NULL, NULL, NULL, NULL
        FROM requested
        LEFT JOIN projection_turns AS turns
          ON turns.thread_id = requested.thread_id
         AND requested.turn_position >= 0
         AND turns.row_id >= requested.turn_position
         AND turns.completed_at IS NOT NULL

        ORDER BY 3, 1, 7
    """
    connection = open_t3code_database(path)
    try:
        if not t3code_schema_ready(connection):
            raise ValueError("unsupported T3 Code schema")
        rows = connection.execute(query, tuple(parameters)).fetchall()
    finally:
        connection.close()

    result: list[T3CodePollRow] = []
    for row in rows:
        row_type = _text(row[0])
        source_id = _text(row[1])
        extracted_paths: list[object] = []
        direct_path = _text(row[13]).strip()
        if direct_path:
            extracted_paths.append(direct_path)
        encoded_paths = _text(row[14])
        if encoded_paths:
            try:
                decoded_paths = json.loads(encoded_paths)
            except json.JSONDecodeError:
                decoded_paths = []
            if isinstance(decoded_paths, list):
                extracted_paths.extend(decoded_paths)
        paths = tuple(
            dict.fromkeys(
                path.strip()
                for value in extracted_paths
                if (path := _text(value)) and path.strip()
            )
        )
        result.append(
            T3CodePollRow(
                row_type=row_type,
                source_id=source_id,
                thread_id=_text(row[2]),
                turn_id=_text(row[3]),
                kind=_text(row[4]),
                created_at=_text(row[5]),
                sequence=_integer(row[6]),
                maximum_position=max(0, _integer(row[7]) or 0),
                minimum_open_turn=_integer(row[8]),
                item_type=_text(row[9]),
                status=_text(row[10]),
                tool_call_id=_text(row[11]),
                command=_text(row[12]),
                paths=paths,
            )
        )
    return result


__all__ = [
    "T3CODE_ACTIVITY_SOURCE",
    "T3CODE_HOME_ENV",
    "T3CODE_TURN_SOURCE",
    "T3CodePollRequest",
    "T3CodePollRow",
    "T3CodeSession",
    "normalize_t3code_provider",
    "open_t3code_database",
    "read_t3code_poll_rows",
    "read_t3code_sessions",
    "t3code_database_path",
    "t3code_home",
    "t3code_schema_ready",
]
