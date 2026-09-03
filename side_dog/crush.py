"""Narrow, read-only access to Crush's project-local session stores.

Crush databases contain prompts, responses, reasoning, command output, diffs,
and file snapshots. This module deliberately selects only the scalar fields
needed to build Side Dog identities and transient tool observations. Raw
message rows and result payloads never leave SQLite.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


CRUSH_GLOBAL_DATA_ENV = "CRUSH_GLOBAL_DATA"
CRUSH_PROJECT_LIMIT = 256
CRUSH_SESSION_LIMIT = 512
CRUSH_MESSAGE_LIMIT = 4096
CRUSH_OVERLAP_MS = 5 * 60 * 1000

_REQUIRED_SCHEMA = {
    "sessions": {
        "id",
        "parent_session_id",
        "title",
        "updated_at",
        "created_at",
    },
    "messages": {
        "id",
        "session_id",
        "role",
        "parts",
        "model",
        "provider",
        "is_summary_message",
        "created_at",
        "updated_at",
        "finished_at",
    },
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:$-]{0,255}$")


@dataclass(frozen=True, slots=True)
class CrushProject:
    path: Path
    data_dir: Path
    last_accessed_epoch_ms: int = 0

    @property
    def database(self) -> Path:
        return self.data_dir / "crush.db"


@dataclass(frozen=True, slots=True)
class CrushSession:
    session_id: str
    parent_session_id: str
    title: str
    model: str
    provider: str
    updated_epoch_ms: int
    created_epoch_ms: int
    finish_reason: str

    @property
    def finished(self) -> bool:
        return self.finish_reason in {
            "end_turn",
            "max_tokens",
            "canceled",
            "error",
            "content_filter",
        }


@dataclass(frozen=True, slots=True)
class CrushToolLifecycle:
    session_id: str
    message_id: str
    call_id: str
    tool_name: str
    tool_input: Mapping[str, Any]
    status: str
    epoch_ms: int


@dataclass(frozen=True, slots=True)
class CrushTurn:
    session_id: str
    message_id: str
    status: str
    epoch_ms: int


def crush_global_data(environment: Mapping[str, str] | None = None) -> Path | None:
    """Return the directory containing Crush's machine-wide data files."""
    values = os.environ if environment is None else environment
    configured = values.get(CRUSH_GLOBAL_DATA_ENV)
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else None
    xdg = values.get("XDG_DATA_HOME")
    if xdg:
        base = Path(xdg).expanduser()
        if not base.is_absolute():
            return None
    else:
        home = values.get("HOME")
        base = (Path(home).expanduser() if home else Path.home()) / ".local" / "share"
    return base / "crush"


def crush_projects_path(environment: Mapping[str, str] | None = None) -> Path | None:
    data = crush_global_data(environment)
    return None if data is None else data / "projects.json"


def _epoch_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    parsed = int(value)
    if parsed <= 0:
        return 0
    return parsed if parsed >= 100_000_000_000 else parsed * 1000


def _identifier(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return text if _SAFE_IDENTIFIER.fullmatch(text) else ""


def _iso_epoch_ms(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    try:
        return int(
            datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
        )
    except (OverflowError, ValueError):
        return 0


def read_crush_project_snapshot(
    path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    *,
    strict: bool = False,
) -> tuple[tuple[CrushProject, ...], tuple[CrushProject, ...]]:
    """Read Crush's public project index without guessing database paths."""
    selected = path or crush_projects_path(environment)
    if selected is None:
        return (), ()
    try:
        document = json.loads(selected.read_text(encoding="utf-8"))
    except OSError:
        if strict:
            raise
        return (), ()
    except (json.JSONDecodeError, UnicodeDecodeError):
        if strict:
            raise ValueError("invalid Crush project index") from None
        return (), ()
    rows = document.get("projects") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        if strict:
            raise ValueError("invalid Crush project index")
        return (), ()
    projects: list[CrushProject] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_path = row.get("path")
        raw_data_dir = row.get("data_dir")
        if (
            not isinstance(raw_path, str)
            or not raw_path.strip()
            or not isinstance(raw_data_dir, str)
            or not raw_data_dir.strip()
        ):
            continue
        try:
            project_path = Path(raw_path).expanduser()
            data_dir = Path(raw_data_dir).expanduser()
            if not project_path.is_absolute():
                continue
            if not data_dir.is_absolute():
                data_dir = project_path / data_dir
            project_path = project_path.resolve(strict=False)
            data_dir = data_dir.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        projects.append(
            CrushProject(
                project_path,
                data_dir,
                _iso_epoch_ms(row.get("last_accessed")),
            )
        )
    projects.sort(key=lambda project: project.last_accessed_epoch_ms, reverse=True)
    return (
        tuple(projects[:CRUSH_PROJECT_LIMIT]),
        tuple(projects[CRUSH_PROJECT_LIMIT:]),
    )


def read_crush_projects(
    path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    *,
    strict: bool = False,
) -> tuple[CrushProject, ...]:
    return read_crush_project_snapshot(path, environment, strict=strict)[0]


def open_crush_database(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve(strict=False).as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def crush_schema_ready(connection: sqlite3.Connection) -> bool:
    for table, required in _REQUIRED_SCHEMA.items():
        columns = {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            if len(row) > 1
        }
        if not required <= columns:
            return False
    return True


def read_crush_session_snapshot(
    database: Path,
) -> tuple[tuple[CrushSession, ...], bool]:
    """Read identity metadata without selecting message bodies or whole parts."""
    connection = open_crush_database(database)
    try:
        if not crush_schema_ready(connection):
            raise ValueError("unsupported Crush schema")
        truncated = bool(
            connection.execute(
                "SELECT EXISTS(SELECT 1 FROM sessions LIMIT 1 OFFSET ?)",
                (CRUSH_SESSION_LIMIT,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            "WITH RECURSIVE recent(id) AS ("
            "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT ?"
            "), closure(id) AS ("
            "SELECT id FROM recent UNION "
            "SELECT sessions.parent_session_id FROM sessions "
            "JOIN closure ON sessions.id = closure.id "
            "WHERE sessions.parent_session_id IS NOT NULL "
            "AND sessions.parent_session_id != '' LIMIT ?"
            ") "
            "SELECT sessions.id, sessions.parent_session_id, sessions.title, "
            "sessions.updated_at, sessions.created_at, "
            "COALESCE((SELECT messages.model FROM messages "
            "WHERE messages.session_id = sessions.id "
            "AND messages.is_summary_message = 0 AND messages.model IS NOT NULL "
            "ORDER BY messages.updated_at DESC, messages.id DESC LIMIT 1), ''), "
            "COALESCE((SELECT messages.provider FROM messages "
            "WHERE messages.session_id = sessions.id "
            "AND messages.is_summary_message = 0 AND messages.provider IS NOT NULL "
            "ORDER BY messages.updated_at DESC, messages.id DESC LIMIT 1), ''), "
            "COALESCE((SELECT json_extract(part.value, '$.data.reason') "
            "FROM (SELECT role, parts FROM messages "
            "WHERE messages.session_id = sessions.id "
            "AND is_summary_message = 0 "
            "ORDER BY messages.updated_at DESC, messages.id DESC LIMIT 1) latest "
            "JOIN json_each(CASE WHEN json_valid(latest.parts) "
            "THEN latest.parts ELSE '[]' END) AS part "
            "WHERE latest.role = 'assistant' AND part.type = 'object' "
            "AND json_extract(part.value, '$.type') = 'finish' LIMIT 1), '') "
            "FROM sessions JOIN closure ON closure.id = sessions.id "
            "ORDER BY sessions.updated_at DESC",
            (CRUSH_SESSION_LIMIT, CRUSH_SESSION_LIMIT * 2),
        ).fetchall()
    finally:
        connection.close()
    sessions: list[CrushSession] = []
    for row in rows:
        session_id = _identifier(row[0])
        raw_parent = row[1]
        parent_session_id = _identifier(raw_parent)
        if not session_id or (raw_parent not in (None, "") and not parent_session_id):
            continue
        sessions.append(
            CrushSession(
                session_id=session_id,
                parent_session_id=parent_session_id,
                title=str(row[2] or "").strip(),
                model=str(row[5] or ""),
                provider=str(row[6] or ""),
                updated_epoch_ms=_epoch_ms(row[3]),
                created_epoch_ms=_epoch_ms(row[4]),
                finish_reason=str(row[7] or "").casefold(),
            )
        )
    records = {session.session_id: session for session in sessions}

    def has_complete_parent_chain(session: CrushSession) -> bool:
        current = session
        seen: set[str] = set()
        while current.session_id not in seen:
            seen.add(current.session_id)
            if not current.parent_session_id:
                return True
            parent = records.get(current.parent_session_id)
            if parent is None:
                return False
            current = parent
        return False

    # A truncated or corrupt chain must not turn a child prompt into a public
    # top-level label. Keep only sessions whose ancestry reaches a loaded root.
    result = tuple(
        session for session in sessions if has_complete_parent_chain(session)
    )
    return result, not truncated


def read_crush_sessions(database: Path) -> tuple[CrushSession, ...]:
    return read_crush_session_snapshot(database)[0]


def _projected_tool_input(
    tool_name: str,
    command: Any,
    working_dir: Any,
    path: Any,
    todo_count: Any,
) -> Mapping[str, Any] | None:
    name = tool_name.casefold()
    if name in {"bash", "shell", "terminal"}:
        if not isinstance(command, str) or not command:
            return None
        summary: dict[str, Any] = {"command": command}
        if isinstance(working_dir, str) and working_dir:
            summary["working_dir"] = working_dir
        return summary
    if name in {"edit", "write", "view", "read"}:
        return {"file_path": path} if isinstance(path, str) and path else None
    if name in {"grep", "glob", "fetch", "webfetch", "agent"}:
        return {}
    if name in {"todo", "todo_write", "todowrite"}:
        count = todo_count if isinstance(todo_count, int) else 0
        return {"todo_count": max(0, count)}
    return None


def read_crush_activity(
    database: Path,
    positions: Mapping[str, int],
) -> tuple[
    tuple[CrushToolLifecycle, ...],
    tuple[CrushTurn, ...],
    Mapping[str, int],
]:
    """Read bounded lifecycle scalars, overlapping updated rows for restarts.

    The overlap recovers a call whose result was written after Side Dog's last
    checkpoint. Callers deduplicate the repeated states with stable source IDs.
    """
    requested = {
        session_id: max(0, position)
        for session_id, position in positions.items()
        if _identifier(session_id) == session_id
        and isinstance(position, int)
        and not isinstance(position, bool)
    }
    if not requested:
        return (), (), {}
    connection = open_crush_database(database)
    try:
        if not crush_schema_ready(connection):
            raise ValueError("unsupported Crush schema")
        rows = connection.execute(
            "WITH requested(session_id, position) AS ("
            "SELECT key, CAST(value AS INTEGER) FROM json_each(?)), "
            "message_rows AS ("
            "SELECT messages.id, messages.session_id, messages.role, "
            "messages.created_at, messages.updated_at, messages.finished_at, "
            "messages.parts, requested.position, "
            "CASE WHEN messages.updated_at < 100000000000 "
            "THEN messages.updated_at * 1000 ELSE messages.updated_at END AS cursor_epoch "
            "FROM messages JOIN requested "
            "ON requested.session_id = messages.session_id "
            "WHERE messages.is_summary_message = 0 "
            "AND (CASE WHEN messages.updated_at < 100000000000 "
            "THEN messages.updated_at * 1000 ELSE messages.updated_at END) "
            ">= MAX(0, requested.position - ?)), "
            "new_seed AS (SELECT * FROM message_rows "
            "WHERE cursor_epoch > position "
            "ORDER BY cursor_epoch, id LIMIT ?), "
            "cutoff AS (SELECT MAX(cursor_epoch) AS epoch FROM new_seed), "
            "new_page AS (SELECT * FROM message_rows "
            "WHERE cursor_epoch > position "
            "AND cursor_epoch <= (SELECT epoch FROM cutoff)), "
            "overlap_ranked AS (SELECT message_rows.*, "
            "ROW_NUMBER() OVER (PARTITION BY session_id "
            "ORDER BY cursor_epoch DESC, id DESC) AS overlap_rank "
            "FROM message_rows "
            "WHERE cursor_epoch <= position), "
            "overlap AS (SELECT id, session_id, role, created_at, updated_at, "
            "finished_at, parts, position, cursor_epoch FROM overlap_ranked "
            "WHERE overlap_rank <= ?), "
            "equal_cursor_lifecycle AS (SELECT DISTINCT message_rows.id, "
            "message_rows.session_id, message_rows.role, message_rows.created_at, "
            "message_rows.updated_at, message_rows.finished_at, message_rows.parts, "
            "message_rows.position, message_rows.cursor_epoch FROM message_rows "
            "JOIN json_each(CASE WHEN json_valid(message_rows.parts) "
            "THEN message_rows.parts ELSE '[]' END) AS part "
            "WHERE message_rows.cursor_epoch = message_rows.position "
            "AND part.type = 'object' "
            "AND json_extract(part.value, '$.type') IN "
            "('tool_call', 'tool_result', 'finish', 'shell_command')), "
            "result_seed AS (SELECT * FROM new_page "
            "UNION SELECT * FROM equal_cursor_lifecycle), "
            "result_calls AS (SELECT DISTINCT result_seed.session_id, "
            "json_extract(part.value, '$.data.tool_call_id') AS call_id "
            "FROM result_seed JOIN json_each(CASE WHEN json_valid(result_seed.parts) "
            "THEN result_seed.parts ELSE '[]' END) AS part "
            "WHERE part.type = 'object' "
            "AND json_extract(part.value, '$.type') = 'tool_result' "
            "AND typeof(json_extract(part.value, '$.data.tool_call_id')) = 'text'), "
            "paired_calls AS (SELECT DISTINCT message_rows.id, "
            "message_rows.session_id, message_rows.role, message_rows.created_at, "
            "message_rows.updated_at, message_rows.finished_at, message_rows.parts, "
            "message_rows.position, message_rows.cursor_epoch FROM message_rows "
            "JOIN result_calls ON result_calls.session_id = message_rows.session_id "
            "JOIN json_each(CASE WHEN json_valid(message_rows.parts) "
            "THEN message_rows.parts ELSE '[]' END) AS part "
            "WHERE message_rows.cursor_epoch <= message_rows.position "
            "AND part.type = 'object' "
            "AND json_extract(part.value, '$.type') = 'tool_call' "
            "AND json_extract(part.value, '$.data.id') = result_calls.call_id), "
            "recent AS (SELECT * FROM overlap UNION SELECT * FROM result_seed "
            "UNION SELECT * FROM paired_calls), "
            "lifecycle_parts AS (SELECT recent.id, recent.session_id, recent.role, "
            "recent.created_at, recent.updated_at, recent.finished_at, "
            "recent.cursor_epoch, "
            "CAST(part.key AS INTEGER) AS part_index, "
            "json_extract(part.value, '$.type') AS part_type, "
            "json_extract(part.value, '$.data.id') AS call_id, "
            "json_extract(part.value, '$.data.name') AS tool_name, "
            "json_extract(part.value, '$.data.input') AS tool_input, "
            "json_extract(part.value, '$.data.finished') AS input_finished, "
            "json_extract(part.value, '$.data.tool_call_id') AS result_call_id, "
            "json_extract(part.value, '$.data.is_error') AS is_error, "
            "json_extract(part.value, '$.data.reason') AS finish_reason, "
            "json_extract(part.value, '$.data.time') AS finish_time, "
            "json_extract(part.value, '$.data.command') AS shell_command, "
            "json_extract(part.value, '$.data.exit_code') AS shell_exit_code "
            "FROM recent LEFT JOIN json_each(CASE WHEN json_valid(recent.parts) "
            "THEN recent.parts ELSE '[]' END) AS part "
            "ON part.type = 'object' "
            "AND json_extract(part.value, '$.type') IN "
            "('tool_call', 'tool_result', 'finish', 'shell_command')) "
            "SELECT id, session_id, role, created_at, updated_at, finished_at, "
            "cursor_epoch, part_index, part_type, call_id, tool_name, "
            "CASE WHEN LOWER(tool_name) IN ('bash', 'shell', 'terminal') "
            "AND json_valid(tool_input) "
            "THEN json_extract(tool_input, '$.command') END, "
            "CASE WHEN LOWER(tool_name) IN ('bash', 'shell', 'terminal') "
            "AND json_valid(tool_input) "
            "THEN json_extract(tool_input, '$.working_dir') END, "
            "CASE WHEN LOWER(tool_name) IN ('edit', 'write', 'view', 'read') "
            "AND json_valid(tool_input) THEN COALESCE("
            "json_extract(tool_input, '$.file_path'), "
            "json_extract(tool_input, '$.path')) END, "
            "CASE WHEN LOWER(tool_name) IN ('todo', 'todo_write', 'todowrite') "
            "AND json_valid(tool_input) "
            "AND json_type(tool_input, '$.todos') = 'array' "
            "THEN json_array_length(tool_input, '$.todos') ELSE 0 END, "
            "input_finished, result_call_id, is_error, finish_reason, finish_time, "
            "shell_command, shell_exit_code FROM lifecycle_parts "
            "ORDER BY cursor_epoch, id, part_index",
            (
                json.dumps(requested),
                CRUSH_OVERLAP_MS,
                CRUSH_MESSAGE_LIMIT,
                CRUSH_MESSAGE_LIMIT,
            ),
        ).fetchall()
    finally:
        connection.close()

    calls: dict[tuple[str, str], tuple[str, str, Mapping[str, Any], int]] = {}
    results: dict[tuple[str, str], tuple[bool | None, int]] = {}
    turns: list[CrushTurn] = []
    shell_calls: list[CrushToolLifecycle] = []
    boundaries = dict(requested)
    for row in rows:
        (
            message_id,
            session_id,
            role,
            created_at,
            updated_at,
            finished_at,
            cursor_epoch,
            part_index,
            part_type,
            call_id,
            tool_name,
            tool_command,
            tool_working_dir,
            tool_path,
            todo_count,
            input_finished,
            result_call_id,
            is_error,
            finish_reason,
            finish_time,
            shell_command,
            shell_exit_code,
        ) = row
        session_id = _identifier(session_id)
        if not session_id or session_id not in requested:
            continue
        epoch_ms = max(
            _epoch_ms(updated_at), _epoch_ms(created_at), _epoch_ms(finished_at)
        )
        boundaries[session_id] = max(
            boundaries[session_id],
            int(cursor_epoch) if isinstance(cursor_epoch, (int, float)) else 0,
        )
        message_id = _identifier(message_id)
        if not message_id:
            continue
        position = requested[session_id]
        key: tuple[str, str]
        if part_type == "tool_call":
            if input_finished not in (True, 1):
                continue
            call_id = _identifier(call_id)
            if not call_id or not isinstance(tool_name, str):
                continue
            tool_input = _projected_tool_input(
                tool_name,
                tool_command,
                tool_working_dir,
                tool_path,
                todo_count,
            )
            if tool_input is not None:
                key = (session_id, call_id)
                calls[key] = (message_id, tool_name, tool_input, epoch_ms)
        elif part_type == "tool_result":
            result_call_id = _identifier(result_call_id)
            if result_call_id:
                failed = bool(is_error) if is_error in (False, True, 0, 1) else None
                results[(session_id, result_call_id)] = (failed, epoch_ms)
        elif part_type == "finish" and role == "assistant":
            reason = str(finish_reason or "").casefold()
            if reason == "tool_use":
                continue
            if reason not in {
                "end_turn",
                "max_tokens",
                "canceled",
                "error",
                "content_filter",
            }:
                continue
            status = (
                "failed"
                if reason in {"error", "content_filter"}
                else "unknown"
                if reason == "canceled"
                else "success"
            )
            event_epoch = max(epoch_ms, _epoch_ms(finish_time))
            if event_epoch >= position:
                turns.append(CrushTurn(session_id, message_id, status, event_epoch))
        elif part_type == "shell_command" and isinstance(shell_command, str):
            event_epoch = epoch_ms
            if event_epoch < position:
                continue
            known_exit = isinstance(shell_exit_code, int) and not isinstance(
                shell_exit_code, bool
            )
            status = (
                "unknown"
                if not known_exit
                else "failed"
                if shell_exit_code != 0
                else "success"
            )
            shell_calls.append(
                CrushToolLifecycle(
                    session_id,
                    message_id,
                    f"shell:{message_id}:{part_index}",
                    "bash",
                    {"command": shell_command},
                    status,
                    event_epoch,
                )
            )

    lifecycle = list(shell_calls)
    for key, (message_id, name, tool_input, call_epoch) in calls.items():
        session_id, call_id = key
        result = results.get(key)
        status = (
            "running"
            if result is None
            else "unknown"
            if result[0] is None
            else "failed"
            if result[0]
            else "success"
        )
        epoch_ms = max(call_epoch, result[1] if result is not None else 0)
        if epoch_ms < requested[session_id]:
            continue
        lifecycle.append(
            CrushToolLifecycle(
                session_id,
                message_id,
                call_id,
                name,
                tool_input,
                status,
                epoch_ms,
            )
        )
    lifecycle.sort(key=lambda item: (item.epoch_ms, item.message_id, item.call_id))
    turns.sort(key=lambda item: (item.epoch_ms, item.message_id))
    return tuple(lifecycle), tuple(turns), boundaries
