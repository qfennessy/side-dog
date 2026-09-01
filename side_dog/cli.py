from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import signal
import sqlite3
import stat as stat_module
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, Callable, Iterable
from urllib.parse import unquote, urlsplit

from side_dog.config import (
    config_display,
    config_ignores,
    config_limit,
    config_pins,
    find_space,
    load_config,
    load_spaces,
    migrate_display_settings,
    path_is_ignored,
    save_space,
    spaces_path,
)
from side_dog.model import (
    MILESTONE_KINDS,
    SOURCE_KEY,
    SOURCE_LABEL,
    activity_unit_local_date,
    actor_label,
    agent_label,
    build_activity_units,
    carry_forward_merge_state,
    coalesce_operations,
    display_conventional_subject,
    display_merge_state,
    display_model,
    event_epoch,
    event_source_key,
    event_source_label,
    github_detail,
    github_burst_numbers,
    github_ci_phase,
    github_fingerprint,
    identity_for_event,
    latest_delivery_context,
    local_date_for_epoch,
    normalize_agent,
    normalize_github_pr,
)


SCHEMA = "side-dog-activity-v1"
STATE_ENV = "SIDE_DOG_STATE_DIR"
DEFAULT_STATE = Path.home() / ".local" / "state" / "side-dog"
EDIT_TOOLS = {"Write", "Edit", "NotebookEdit"}
SHELL_WRAPPERS = {"command", "env", "exec", "nohup", "sudo", "time", "xargs"}
# Programs whose non-zero exit is an answer rather than a failure.
QUIET_EXIT_PROGRAMS = {
    "ack",
    "ag",
    "cmp",
    "diff",
    "egrep",
    "fgrep",
    "find",
    "grep",
    "pgrep",
    "rg",
    "test",
    "which",
}
CONFIG_NAMES = {
    "AGENTS.md",
    "CLAUDE.md",
    "Dockerfile",
    "Makefile",
    "compose.yml",
    "compose.yaml",
    "package.json",
    "pyproject.toml",
    "settings.json",
    "settings.local.json",
    "tsconfig.json",
}
CONFIG_SUFFIXES = {
    ".cfg",
    ".conf",
    ".ini",
    ".json",
    ".lock",
    ".toml",
    ".yaml",
    ".yml",
}
IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
    "venv",
}

# What a snapshot writes down for a file git still names but the disk no longer
# has. It is not a size and a time because there is no file left to measure.
DELETED_FILE = (-1, -1)

ANSI = {
    "reset": "\x1b[0m",
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "inverse": "\x1b[7m",
    "blue": "\x1b[38;5;75m",
    "cyan": "\x1b[38;5;80m",
    "green": "\x1b[38;5;78m",
    "magenta": "\x1b[38;5;176m",
    "red": "\x1b[38;5;203m",
    "yellow": "\x1b[38;5;221m",
}

# Root colors are deliberately attached to root names and source badges instead
# of detached swatches or full-row fills. This keeps ownership explicit without
# making the accent look like progress or status, and leaves semantic status
# foregrounds readable on both dark and light terminal themes. Assignment is by
# canonical root order, not the mutable branch/PR label; roots beyond the
# palette cycle predictably.
# One color per watched root, shared by the block at the start of its lines,
# its badge, and its name in the header, so the header reads as the legend.
ROOT_PALETTE = (39, 40, 203, 170, 184, 44, 141, 208, 75, 78, 167, 111)
# Near-black, so a root name reads on any of those bright colors.
ROOT_NAME_INK = "\x1b[38;5;16m"

GITHUB_PR_FIELDS = (
    "number,url,title,state,isDraft,headRefName,reviewDecision,mergeStateStatus,"
    "mergeable,statusCheckRollup,createdAt,updatedAt,closedAt,mergedAt"
)
FILTER_ORDER = ("all", "milestones", "files")
COMMANDS = ("init", "hook", "watch", "panel", "tmux", "demo")
COLUMN_MIN_WIDTH = 42
PROJECT_URL = "https://github.com/qfennessy/side-dog"
PANEL_URL_PREFIX = "Side Dog panel: "
DISPLAY_NOTICE_SECONDS = 2.0
WORKTREE_SCAN_SECONDS = 5.0
# What `side-dog watch` means when no folder is named. argparse hands this exact
# list back for a bare `watch` and a fresh one for `watch .`, so identity is what
# tells "look wherever agents are working" apart from "look here", while both
# still read as the current folder to anyone printing the parsed arguments.
WATCH_DEFAULT_PROJECTS = ["."]
# How many folders may share one pane when the configuration file does not say.
# Eight fits a tall terminal; a wide screen full of worktrees may want more.
WATCH_ROOT_LIMIT = 8
HERDR_SNAPSHOT_TTL_SECONDS = 1.0
# How recently something must have happened for a worktree to earn a column.
FOLDER_ACTIVE_WINDOW_MS = 24 * 60 * 60 * 1000
CODEX_METADATA_CACHE: dict[str, tuple[int, dict[str, str]]] = {}
CLAUDE_METADATA_CACHE: dict[str, tuple[int, dict[str, str]]] = {}
PI_METADATA_CACHE: dict[str, tuple[int, dict[str, str]]] = {}
# The coding agents Side Dog gives a row to, named as each source names them.
# Herdr reports raw agent names; the renderer works in normalized names.
HERDR_CODING_AGENTS = {"claude", "codex", "pi"}
DISPLAY_CODING_AGENTS = {"claude-code", "codex", "pi"}
SESSION_PATH_CACHE: dict[str, Path] = {}
SESSION_PATH_MISSES: dict[str, float] = {}
SESSION_PATH_CACHE_LIMIT = 128
SESSION_PATH_RETRY_SECONDS = 2.0
SOURCE_COLOR_INDEX = "_side_dog_source_color_index"
HERDR_SNAPSHOT_LOCK = threading.Lock()
HERDR_SNAPSHOT_CACHE: tuple[float, dict[str, Any], str | None] | None = None


def canonical_root(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def invoked_within_herdr(environment: dict[str, str] | None = None) -> bool:
    """Whether this command inherited a live Herdr session context."""
    values = os.environ if environment is None else environment
    return values.get("HERDR_ENV") == "1" or bool(values.get("HERDR_SOCKET_PATH"))


def display_root(root: Path) -> str:
    try:
        return os.fspath(Path("~") / root.relative_to(Path.home()))
    except ValueError:
        return os.fspath(root)


def event_source_color_index(event: dict[str, Any]) -> int | None:
    value = event.get(SOURCE_COLOR_INDEX)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def root_color(color_index: int) -> str:
    code = ROOT_PALETTE[color_index % len(ROOT_PALETTE)]
    return f"\x1b[48;5;{code}m"


@dataclass
class DisplayNotice:
    """Short-lived explanation of the most recent display change."""

    message: str = ""
    expires_at: float = 0.0

    def show(
        self,
        message: str,
        now: float,
        duration: float = DISPLAY_NOTICE_SECONDS,
    ) -> None:
        self.message = message
        self.expires_at = now + duration

    def current(self, now: float) -> str | None:
        return self.message if self.message and now < self.expires_at else None


def web_panel_notice(url: str) -> str:
    return f"Web panel at {url} — it closes when Side Dog does."


def append_search_byte(search: str, pending: bytes, key: bytes) -> tuple[str, bytes]:
    """Add one byte of typed input to a search, decoding UTF-8 as it arrives.

    A terminal delivers a non-ASCII character as several bytes, one read at a
    time, so a byte that does not decode yet is held until the rest turn up.
    """
    buffered = pending + key
    try:
        text = buffered.decode()
    except UnicodeDecodeError:
        return (search, buffered) if len(buffered) < 4 else (search, b"")
    return (search + text if text.isprintable() else search), b""


def search_notice(search: str) -> str:
    if search:
        return f"Showing only lines matching “{search}”. Esc clears it."
    return "Search cleared — every line is back."


def worker_notice(count: int) -> str:
    """Report the helpers a session has running; herdr only sees the session."""
    if count <= 0:
        return ""
    return f" · {count} worker" + ("" if count == 1 else "s")


def worktree_retire_notice(paths: list[Path]) -> str:
    names = ", ".join(path.name for path in paths)
    if len(paths) == 1:
        return f"{names} is finished — dropped from the pane."
    return f"{names} are finished — dropped from the pane."


def worktree_follow_notice(paths: list[Path]) -> str:
    names = ", ".join(path.name for path in paths)
    if len(paths) == 1:
        return f"Now watching new worktree {names}."
    return f"Now watching new worktrees {names}."


def expanded_history_notice(expanded: bool) -> str:
    if expanded:
        return "Expanded — every event on its own line, with full detail."
    return "Compact — related file writes and delivery steps are grouped."


def event_filter_notice(event_filter: str) -> str:
    return {
        "milestones": "Milestones only — commits, pushes, PRs, tests, branches.",
        "files": "File writes only — everything else is hidden.",
        "all": "Everything — file writes and milestones together.",
    }[event_filter]


def root_focus_notice(
    focused_root_index: int | None,
    labels: list[str],
    layout: str,
) -> str:
    if focused_root_index is not None:
        return f"Showing only {labels[focused_root_index]}."
    if layout == "columns":
        return "All folders — one column each when the pane is wide enough."
    if layout == "timeline":
        return "All folders — one shared list."
    return "All folders — wide panes use columns, narrow panes one list."


def ordering_notice(newest_first: bool) -> str:
    if newest_first:
        return "Newest first — new events appear at the top."
    return "Oldest first — new events appear at the bottom."


def pause_notice(paused: bool) -> str:
    if paused:
        return "Paused — collection continues; display updates are held."
    return "Live — held updates are now visible."


def root_color_index(root_index: int) -> int:
    return root_index % len(ROOT_PALETTE)


def root_summary_label(summary: str) -> str:
    # A summary can end in an activity meter; the label is what comes before it.
    summary = summary.rstrip(ACTIVITY_LEVELS + " ")
    if " @ " in summary:
        return summary.split(" @ ", 1)[0]
    if summary.startswith("PR #"):
        return " ".join(summary.split(maxsplit=2)[:2])
    if " · PR #" in summary:
        return summary.split(" · PR #", 1)[0]
    return summary


def style_root_name(
    name: str,
    color_index: int,
    activity_state: str = "unknown",
    restore: str = "",
) -> str:
    activity_style = {
        "working": ANSI["bold"],
        "inactive": ANSI["dim"],
    }.get(activity_state, "")
    return (
        f"{activity_style}{root_color(color_index)}"
        f"{ROOT_NAME_INK}{ANSI['bold']}{name}{ANSI['reset']}"
        f"{activity_style}{restore}"
    )


def style_source_label(
    text: str,
    event: dict[str, Any],
    color: bool,
    restore: str = "",
) -> str:
    label = event_source_label(event)
    color_index = event_source_color_index(event)
    if not color or not label or color_index is None:
        return text
    marker = f"[{label}]"
    badge = (
        f"{root_color(color_index)}{ROOT_NAME_INK}{ANSI['bold']}"
        f"{marker}{ANSI['reset']}{restore}"
    )
    return text.replace(marker, badge, 1)


def label_summary(
    event: dict[str, Any], summary: str, show_source: bool = True
) -> str:
    label = event_source_label(event) if show_source else ""
    return f"[{label}] {summary}" if label else summary


def project_key(root: Path) -> str:
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    return f"{root.name}-{digest}"


def watch_root_limit() -> int:
    """How many folders may share the pane, from the configuration file.

    Read rather than cached: the file is tiny, and the two callers ask a few
    times a minute at most.
    """
    return config_limit(load_config(), WATCH_ROOT_LIMIT)


def display_settings_path() -> Path:
    return state_root() / "display.json"


def load_display_settings() -> dict[str, Any]:
    """Read the display toggles from the last run, if they are still readable."""
    try:
        settings = json.loads(display_settings_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def save_display_settings(
    *, newest_first: bool, expanded_history: bool, event_filter: str
) -> None:
    path = display_settings_path()
    payload = {
        "newest_first": bool(newest_first),
        "expanded_history": bool(expanded_history),
        "event_filter": event_filter,
    }
    try:
        ensure_private_dir(path.parent)
        path.write_text(json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass


def state_root() -> Path:
    return Path(os.environ.get(STATE_ENV, DEFAULT_STATE)).expanduser()


def project_state(root: Path) -> Path:
    return state_root() / "projects" / project_key(root)


def events_path(root: Path) -> Path:
    return project_state(root) / "events.jsonl"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def append_event(root: Path, event: dict[str, Any]) -> None:
    destination = events_path(root)
    ensure_private_dir(destination.parent)
    now = datetime.now(timezone.utc)
    record = {
        "schema": SCHEMA,
        "timestamp": now.isoformat(timespec="milliseconds"),
        "epoch_ms": int(now.timestamp() * 1000),
        "agent": "claude-code",
        "project": os.fspath(root),
        **event,
    }
    payload = (
        json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def native_index_path(root: Path) -> Path:
    return events_path(root).with_name("native-events.sqlite3")


def native_index_connection(root: Path) -> sqlite3.Connection:
    destination = native_index_path(root)
    ensure_private_dir(destination.parent)
    connection = sqlite3.connect(destination, timeout=2.0)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS native_events "
            "(source_event_id TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS native_streams ("
            "session_id TEXT NOT NULL, transcript_path TEXT NOT NULL, "
            "position INTEGER NOT NULL, "
            "PRIMARY KEY(session_id, transcript_path)) WITHOUT ROWID"
        )
    return connection


def load_native_stream_position(root: Path, session_id: str, path: Path) -> int:
    connection = native_index_connection(root)
    try:
        row = connection.execute(
            "SELECT position FROM native_streams "
            "WHERE session_id = ? AND transcript_path = ?",
            (session_id, os.fspath(path)),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not isinstance(row[0], int):
        return 0
    return max(0, row[0])


def save_native_stream_position(
    root: Path, session_id: str, path: Path, position: int
) -> None:
    connection = native_index_connection(root)
    try:
        with connection:
            connection.execute(
                "INSERT INTO native_streams(session_id, transcript_path, position) "
                "VALUES (?, ?, ?) ON CONFLICT(session_id, transcript_path) "
                "DO UPDATE SET position = excluded.position",
                (session_id, os.fspath(path), max(0, position)),
            )
    finally:
        connection.close()


def native_event_count(root: Path, session_id: str) -> int:
    connection = native_index_connection(root)
    try:
        row = connection.execute(
            "SELECT count(*) FROM native_events WHERE source_event_id LIKE ?",
            (f"codex:{session_id}:%",),
        ).fetchone()
    finally:
        connection.close()
    return int(row[0]) if row is not None else 0


def append_event_once(root: Path, event: dict[str, Any]) -> bool:
    """Append a native agent event once, even with multiple Side Dog views open."""
    source_event_id = event.get("source_event_id")
    if not isinstance(source_event_id, str) or not source_event_id:
        append_event(root, event)
        return True
    connection = native_index_connection(root)
    try:
        with connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO native_events(source_event_id) VALUES (?)",
                (source_event_id,),
            )
            if cursor.rowcount == 0:
                return False
            append_event(root, event)
            return True
    finally:
        connection.close()


def hook_context(payload: dict[str, Any]) -> dict[str, str]:
    context = {
        "session_id": str(payload.get("session_id", "unknown")),
        "agent": normalize_agent(payload.get("agent")),
    }
    prompt_id = payload.get("prompt_id")
    if isinstance(prompt_id, str) and prompt_id:
        context["turn_id"] = prompt_id
    model = payload.get("model")
    if isinstance(model, str) and model:
        context["model"] = model
    effort = payload.get("effort") or payload.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        context["effort"] = effort
    for field, environment_name in (
        ("herdr_pane_id", "HERDR_PANE_ID"),
        ("herdr_tab_id", "HERDR_TAB_ID"),
        ("herdr_workspace_id", "HERDR_WORKSPACE_ID"),
    ):
        value = os.environ.get(environment_name)
        if value:
            context[field] = value
    return context


def relative_display(raw_path: Any, root: Path) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        return "unknown file"
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = root / path
        return os.fspath(path.resolve(strict=False).relative_to(root))
    except (OSError, ValueError):
        return Path(raw_path).name or "outside project"


def edit_path(tool_input: Any, root: Path) -> str:
    if not isinstance(tool_input, dict):
        return "unknown file"
    for key in ("file_path", "notebook_path", "path"):
        if key in tool_input:
            return relative_display(tool_input[key], root)
    return "unknown file"


def is_config(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.name in CONFIG_NAMES
        or candidate.suffix.lower() in CONFIG_SUFFIXES
        or any(part in {".claude", ".codex", ".github"} for part in candidate.parts)
    )


def _safe_arg(command: str, pattern: str, fallback: str) -> str:
    match = re.search(pattern, command, flags=re.IGNORECASE)
    if not match:
        return fallback
    value = match.group(1).strip("'\"")
    if not value or value.startswith("-") or len(value) > 80:
        return fallback
    return value


def _safe_title_flag(command: str, command_words: tuple[str, ...]) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    lowered = [token.casefold() for token in tokens]
    for index in range(len(tokens) - len(command_words) + 1):
        if tuple(lowered[index : index + len(command_words)]) != command_words:
            continue
        for offset in range(index + len(command_words), len(tokens)):
            token = tokens[offset]
            if token in {";", "&&", "||", "|"}:
                break
            if token == "--title" and offset + 1 < len(tokens):
                value = tokens[offset + 1]
            elif token.startswith("--title="):
                value = token.partition("=")[2]
            else:
                continue
            value = " ".join(value.split())
            if (
                not value
                or len(value) > 120
                or value.startswith(("$", "-"))
                or any(ord(character) < 32 for character in value)
            ):
                return None
            return value
    return None


def _shell_search_text(command: str) -> str:
    """Mask quoted argument data while preserving shell command positions."""
    output = list(command)
    quote = ""
    escaped = False
    for index, character in enumerate(command):
        if quote:
            output[index] = " "
            if escaped:
                escaped = False
            elif quote == '"' and character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif character in {"'", '"'}:
            quote = character
            output[index] = " "
    return "".join(output)


def classify_commands(command: str) -> list[tuple[str, str, str]]:
    collapsed = " ".join(command.split())
    if not collapsed:
        return []
    searchable = _shell_search_text(collapsed)

    test_patterns = (
        r"\b(?:uv\s+run\s+)?pytest\b",
        r"\bpython(?:3)?\s+-m\s+(?:pytest|unittest)\b",
        r"\b(?:npm|pnpm|yarn|bun)\b[^;&|]{0,80}\b(?:test|vitest|jest)\b",
        r"\b(?:cargo|go)\s+test\b",
        r"\bmake\s+(?:test|check)\b",
        r"\b(?:vitest|jest|rspec|mix\s+test)\b",
    )
    matches: list[tuple[int, tuple[str, str, str]]] = []
    test_match = next(
        (
            match
            for pattern in test_patterns
            if (match := re.search(pattern, searchable, re.IGNORECASE))
        ),
        None,
    )
    if test_match:
        runner = _safe_arg(
            searchable,
            r"\b(pytest|unittest|vitest|jest|cargo test|go test|rspec|mix test)\b",
            "test suite",
        )
        matches.append((test_match.start(), ("test", "Running tests", runner)))

    rules: tuple[tuple[str, str, str, str], ...] = (
        (
            r"\bgit\s+worktree\s+add\b",
            "worktree",
            "Creating worktree",
            "git worktree add",
        ),
        (
            r"\bgit\s+worktree\s+add\b[^;&|]{0,240}\s-(?:b|B)\s+",
            "branch",
            "Creating branch",
            "git branch",
        ),
        (
            r"\bgit\s+worktree\s+(?:remove|prune)\b",
            "worktree",
            "Removing worktree",
            "git worktree",
        ),
        (
            r"\bgit\s+(?:switch\s+-c|checkout\s+-b)\s+",
            "branch",
            "Creating branch",
            "git branch",
        ),
        (
            r"\bgit\s+branch\s+(?!-|--list\b|--show-current\b)",
            "branch",
            "Creating branch",
            "git branch",
        ),
        (r"\bgit\s+commit\b", "commit", "Creating commit", "git commit"),
        (r"\bgit\s+push\b", "push", "Pushing branch", "git push"),
        (r"\bgh\s+pr\s+create\b", "pr", "Opening pull request", "gh pr create"),
        (r"\bgh\s+pr\s+merge\b", "merge", "Merging pull request", "gh pr merge"),
        (r"\bgh\s+issue\s+create\b", "issue", "Opening issue", "gh issue create"),
        (r"\bgh\s+issue\s+close\b", "issue", "Closing issue", "gh issue close"),
        (r"\bgh\s+issue\s+reopen\b", "issue", "Reopening issue", "gh issue reopen"),
    )
    for pattern, kind, title, detail in rules:
        match = re.search(pattern, searchable, re.IGNORECASE)
        if match:
            if kind == "branch" and "worktree" in pattern:
                detail = _safe_arg(
                    searchable,
                    r"\bgit\s+worktree\s+add\b[^;&|]{0,240}\s-(?:b|B)\s+([^\s;&|]+)",
                    detail,
                )
            elif kind == "pr" and "create" in pattern:
                detail = _safe_title_flag(command, ("gh", "pr", "create")) or detail
            elif kind == "issue" and "create" in pattern:
                detail = _safe_title_flag(command, ("gh", "issue", "create")) or detail
            elif kind == "issue" and "close" in pattern:
                detail = _safe_arg(
                    searchable,
                    r"\bgh\s+issue\s+close\s+#?(\d+)",
                    detail,
                )
                if detail.isdigit():
                    detail = f"issue #{detail}"
            elif kind == "issue" and "reopen" in pattern:
                detail = _safe_arg(
                    searchable,
                    r"\bgh\s+issue\s+reopen\s+#?(\d+)",
                    detail,
                )
                if detail.isdigit():
                    detail = f"issue #{detail}"
            matches.append((match.start(), (kind, title, detail)))
    matches.sort(key=lambda item: item[0])
    return [item for _, item in matches]


def shell_command_is_compound(command: str) -> bool:
    """Return whether a shell command's final status is ambiguous to Side Dog."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return any(token and set(token) <= set(";&|") for token in lexer)
    except ValueError:
        return True


def command_program(command: str) -> str:
    """Name the program a command runs, without repeating its arguments.

    Side Dog never records command text, so a failure reports the program only.
    Environment assignments are skipped because they carry values, and a path
    is reduced to its last segment so a home directory does not leak.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if not token or "=" in token or token.startswith("-"):
            continue
        name = token.rsplit("/", 1)[-1].strip("\"'")
        if not name or name in SHELL_WRAPPERS:
            continue
        return name[:40]
    return "command"


def operation_id(payload: dict[str, Any]) -> str:
    raw = payload.get("tool_use_id")
    if isinstance(raw, str) and raw:
        return raw
    session = str(payload.get("session_id", "unknown"))
    material = json.dumps(payload.get("tool_input", {}), sort_keys=True, default=str)
    return hashlib.sha256(f"{session}:{material}".encode()).hexdigest()[:16]


def normalized_tool_events(
    payload: dict[str, Any], root: Path, *, status: str
) -> list[dict[str, Any]]:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    context = hook_context(payload)
    identifier = operation_id(payload)

    if tool_name in EDIT_TOOLS:
        path = edit_path(tool_input, root)
        config = is_config(path)
        counts = git_line_changes(root, path) if status == "success" else None
        if status == "running":
            title = "Writing config" if config else "Writing file"
        elif status == "failed":
            title = "Config write failed" if config else "File write failed"
        else:
            title = "Wrote config" if config else "Wrote file"
        return [
            {
                **context,
                "operation_id": identifier,
                "group_id": identifier,
                "kind": "config" if config else "file",
                "status": status,
                "title": title,
                "detail": path,
                **(
                    {"lines_added": counts[0], "lines_removed": counts[1]}
                    if counts
                    else {}
                ),
            }
        ]

    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return []
    command = tool_input.get("command")
    if not isinstance(command, str):
        return []
    classified = classify_commands(command)
    if not classified:
        return failed_command_events(command, context, identifier, status)
    event_status = status
    if status != "running" and shell_command_is_compound(command):
        event_status = "unknown"
    events: list[dict[str, Any]] = []
    for index, (kind, running_title, detail) in enumerate(classified):
        if status == "running":
            title = running_title
        elif event_status == "failed":
            failed = {
                "test": "Tests failed",
                "worktree": "Worktree update failed",
                "branch": "Branch creation failed",
                "commit": "Commit failed",
                "push": "Push failed",
                "pr": "Pull request creation failed",
                "merge": "Pull request merge failed",
                "issue": "Issue update failed",
            }
            title = failed[kind]
        elif event_status == "success":
            completed = {
                "test": "Tests passed",
                "worktree": "Worktree updated",
                "branch": "Branch created",
                "commit": "Commit created",
                "push": "Branch pushed",
                "pr": "PR create command succeeded",
                "merge": "PR merge command succeeded",
                "issue": running_title.replace("Opening", "Opened")
                .replace("Closing", "Closed")
                .replace("Reopening", "Reopened"),
            }
            title = completed[kind]
        else:
            finished = {
                "test": "Tests finished",
                "worktree": "Worktree command finished",
                "branch": "Branch command finished",
                "commit": "Commit command finished",
                "push": "Push command finished",
                "pr": "PR create command finished",
                "merge": "PR merge command finished",
                "issue": "Issue command finished",
            }
            title = finished[kind]
        extra: dict[str, Any] = {}
        if kind == "commit" and event_status == "success":
            git_state = load_git_state(root)
            if git_state is not None:
                extra["git_oid"] = git_state["oid"]
                detail = git_commit_detail(root, git_state)
        events.append(
            {
                **context,
                **extra,
                "operation_id": f"{identifier}:{index}:{kind}",
                "group_id": identifier,
                "kind": kind,
                "status": event_status,
                "title": title,
                "detail": detail,
            }
        )
    return events


def failed_command_events(
    command: str,
    context: dict[str, str],
    identifier: str,
    status: str,
) -> list[dict[str, Any]]:
    """Report a command that failed even when its work is not worth an event.

    Only a single command qualifies. Side Dog cannot say which half of
    `build && deploy` failed, and across a day of real sessions every
    compound failure was either ambiguous or a search that simply found
    nothing, so those stay out of the pane.
    """
    if status != "failed" or shell_command_is_compound(command):
        return []
    program = command_program(command)
    if program in QUIET_EXIT_PROGRAMS:
        return []
    return [
        {
            **context,
            "operation_id": f"{identifier}:0:command",
            "group_id": identifier,
            "kind": "command",
            "status": "failed",
            "title": "Command failed",
            "detail": program,
        }
    ]


def emit_tool_event(payload: dict[str, Any], root: Path, *, status: str) -> None:
    for event in normalized_tool_events(payload, root, status=status):
        append_event(root, event)


def hook(explicit_root: str | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        # This command is installed only as a Claude Code native hook. Do not
        # allow input data to misattribute a Claude event to another agent.
        payload["agent"] = "claude-code"
        root = canonical_root(explicit_root or str(payload.get("cwd") or os.getcwd()))
        event_name = str(payload.get("hook_event_name", ""))
        context = hook_context(payload)

        if event_name == "PreToolUse":
            emit_tool_event(payload, root, status="running")
        elif event_name == "PostToolUse":
            emit_tool_event(payload, root, status="success")
        elif event_name == "PostToolUseFailure":
            emit_tool_event(payload, root, status="failed")
        elif event_name == "SessionStart":
            source = str(payload.get("source", "start"))
            append_event(
                root,
                {
                    **context,
                    "kind": "session",
                    "status": "success",
                    "title": "Claude session active",
                    "detail": source,
                },
            )
        elif event_name == "Stop":
            append_event(
                root,
                {
                    **context,
                    "kind": "session",
                    "status": "success",
                    "title": "Claude turn finished",
                    "detail": "",
                },
            )
        elif event_name == "SessionEnd":
            reason = str(payload.get("reason", "complete"))
            append_event(
                root,
                {
                    **context,
                    "kind": "session",
                    "status": "success",
                    "title": "Claude session ended",
                    "detail": reason,
                },
            )
        elif event_name == "ConfigChange":
            append_event(
                root,
                {
                    **context,
                    "kind": "config",
                    "status": "success",
                    "title": "Claude config changed",
                    "detail": str(payload.get("source", "configuration")),
                },
            )
    except Exception:
        # Observability must never interrupt or alter the coding agent.
        return 0
    return 0


def command_for_hook(root: Path) -> str:
    executable = shutil.which("side-dog")
    if executable:
        base = f"{shlex.quote(str(Path(executable).resolve()))} hook"
    else:
        base = (
            f"{shlex.quote(sys.executable)} "
            f"{shlex.quote(os.fspath(Path(__file__).resolve()))} hook"
        )
    return f"SIDE_DOG_MANAGED=1 {base} --root {shlex.quote(os.fspath(root))}"


def hook_entry(command: str, *, matcher: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 5,
                "statusMessage": "Updating Side Dog",
            }
        ]
    }
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def desired_hooks(command: str) -> dict[str, list[dict[str, Any]]]:
    tool_matcher = "^(Bash|Write|Edit|NotebookEdit)$"
    return {
        "SessionStart": [hook_entry(command)],
        "PreToolUse": [hook_entry(command, matcher=tool_matcher)],
        "PostToolUse": [hook_entry(command, matcher=tool_matcher)],
        "PostToolUseFailure": [hook_entry(command, matcher=tool_matcher)],
        "ConfigChange": [hook_entry(command)],
        "Stop": [hook_entry(command)],
        "SessionEnd": [hook_entry(command)],
    }


def is_side_dog_hook_command(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if tokens[:1] == ["SIDE_DOG_MANAGED=1"]:
        tokens = tokens[1:]
    if len(tokens) < 3 or "--root" not in tokens:
        return False
    executable = Path(tokens[0]).name
    if executable == "side-dog":
        return tokens[1] == "hook"
    if executable.startswith("python") and len(tokens) >= 4:
        script = Path(tokens[1])
        return (
            script.name == "cli.py"
            and script.parent.name == "side_dog"
            and tokens[2] == "hook"
        )
    return False


def is_side_dog_entry(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    hooks = value.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(item, dict) and is_side_dog_hook_command(item.get("command"))
        for item in hooks
    )


def init_claude(project: str, *, print_only: bool = False) -> int:
    root = canonical_root(project)
    if not root.is_dir():
        raise SystemExit(f"no such folder: {root}")
    settings = root / ".claude" / "settings.local.json"
    if settings.parent.is_symlink() or settings.is_symlink():
        raise SystemExit("refusing to write Claude settings through a symlink")
    command = command_for_hook(root)
    document: dict[str, Any] = {}
    if settings.exists():
        try:
            loaded = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"refusing to change invalid local settings: {error}")
        if not isinstance(loaded, dict):
            raise SystemExit("refusing to change non-object local settings")
        document = loaded
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(
            "refusing to change local settings with a non-object hooks value"
        )

    for event_name, existing in list(hooks.items()):
        if isinstance(existing, list):
            cleaned = [entry for entry in existing if not is_side_dog_entry(entry)]
            if len(cleaned) != len(existing) and not cleaned:
                del hooks[event_name]
            else:
                hooks[event_name] = cleaned

    for event_name, additions in desired_hooks(command).items():
        existing = hooks.setdefault(event_name, [])
        if not isinstance(existing, list):
            raise SystemExit(f"refusing to change non-list hook event: {event_name}")
        hooks[event_name] = existing + additions

    rendered = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if print_only:
        print(rendered, end="")
        return 0
    settings.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings.with_name(f".{settings.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, rendered.encode())
    finally:
        os.close(descriptor)
    os.replace(temporary, settings)
    print(f"Installed Claude Code hooks in {settings}")
    print("Restart Claude Code, then run `side-dog watch .` in a narrow right pane.")
    return 0


def iter_project_files(root: Path) -> Iterable[tuple[Path, os.stat_result]]:
    """Walk the folder once, handing back the stat each file was judged by.

    The caller needs the size and modification time this already looked at.
    Returning it halves the work: a repository of ten thousand files was being
    stat-ed twice on every scan, and that scan runs while the pane is drawn.
    """
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in IGNORED_DIRS]
        current_path = Path(current)
        for name in files:
            path = current_path / name
            try:
                stat = path.lstat()
                if stat_module.S_ISLNK(stat.st_mode) or stat.st_size > 5_000_000:
                    continue
            except OSError:
                continue
            yield path, stat


def git_path_prefix(root: Path) -> str:
    """Where this folder sits inside its repository, the way git spells it.

    Empty at the top of a repository. `git status --porcelain` names files from
    the top whatever folder you run it in, so watching a subfolder needs this
    to turn git's `sub/app.py` back into the `app.py` the rest of Side Dog uses.
    """
    if (root / ".git").exists():
        return ""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=root,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    # Only git's newline comes off. A folder is allowed to be called " dir ",
    # and stripping its spaces would leave a prefix that matches nothing.
    return os.fsdecode(completed.stdout).removesuffix("\n")


def git_changed_paths(root: Path) -> list[str] | None:
    """Paths git says differ from the last commit, or None outside a repository.

    Git keeps an index and is written in C, so it answers in a fraction of the
    time a walk takes: 137 ms against 983 ms on a ten thousand file repository.
    It also answers the better question - what actually differs - rather than
    handing back ten thousand modification times to compare.

    Names come back relative to this folder. Git hands back raw bytes, because
    a filename on this machine does not have to be text git can read, so the
    bytes are decoded the way the filesystem decodes them and never strictly.

    Ignored files count. A `.env` or a generated config is still somebody's
    work, and the old walk saw them. A whole ignored folder does not: git names
    it once, as a folder, and a build directory of ten thousand files is the
    cost this function exists to avoid.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
                "-z",
                "--",
                ".",
            ],
            cwd=root,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    paths: list[str] = []
    fields = completed.stdout.split(b"\0")
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], os.fsdecode(entry[3:])
        if status == b"!!" and path.endswith("/"):
            # A whole ignored folder, named once instead of file by file.
            continue
        if status[:1] == b"R":
            # A rename spends a second field on where the file came from.
            if index < len(fields):
                paths.append(os.fsdecode(fields[index]))
                index += 1
        paths.append(path)
    prefix = git_path_prefix(root)
    if not prefix:
        return paths
    return [name[len(prefix) :] for name in paths if name.startswith(prefix)]


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """What each interesting file in this folder looks like right now.

    Asks git which files differ and stats only those. A folder git does not
    know about is walked instead, which is what always used to happen.
    """
    changed = git_changed_paths(root)
    if changed is None:
        return walk_snapshot(root)
    result: dict[str, tuple[int, int]] = {}
    for name in changed:
        if not name or any(part in IGNORED_DIRS for part in Path(name).parts):
            continue
        try:
            stat = (root / name).lstat()
        except OSError:
            # Git names a file it knows was deleted, and there is nothing left
            # to measure. Write down the hole instead, or deleting a file that
            # was untouched when Side Dog started would pass without a word -
            # it was never in the snapshot to go missing from.
            result[name] = DELETED_FILE
            continue
        if stat_module.S_ISLNK(stat.st_mode) or stat.st_size > 5_000_000:
            continue
        result[name] = (stat.st_mtime_ns, stat.st_size)
    return result


def walk_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for path, stat in iter_project_files(root):
        try:
            result[os.fspath(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
        except ValueError:
            continue
    return result


def load_git_state(root: Path) -> dict[str, str] | None:
    if shutil.which("git") is None:
        return None
    try:
        completed = subprocess.run(
            [
                "git",
                "rev-parse",
                "HEAD",
                "--symbolic-full-name",
                "HEAD",
                "--git-common-dir",
                "--show-toplevel",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0 or not lines:
        return None
    oid = lines[0]
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", oid):
        return None
    reference = lines[1] if len(lines) > 1 else ""
    branch = (
        reference.removeprefix("refs/heads/")
        if reference and reference != "HEAD"
        else "detached"
    )
    common_dir = lines[2] if len(lines) > 2 else ""
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = (root / common_path).resolve()
    worktree_root = canonical_root(lines[3]) if len(lines) > 3 else root
    repository = common_path.parent.name if common_path.name == ".git" else root.name
    return {
        "oid": oid,
        "short_oid": oid[:7],
        "branch": branch,
        "common_dir": os.fspath(common_path),
        "worktree_root": os.fspath(worktree_root),
        "repository": repository,
    }


@lru_cache(maxsize=128)
def git_repository_location(path: str) -> tuple[str, str]:
    state = load_git_state(canonical_root(path))
    if state is None:
        return "", ""
    return state.get("common_dir", ""), state.get("worktree_root", "")


def git_common_dir(path: str) -> str:
    return git_repository_location(path)[0]


def git_worktree_root(path: str) -> str:
    return git_repository_location(path)[1]


def git_line_changes(root: Path, path: str) -> tuple[int, int] | None:
    """Lines added and removed in a file, against the last commit.

    This is the number `git status` would give you, so a file edited five times
    reports how big the change is rather than how big the last write was.
    """
    try:
        completed = subprocess.run(
            ["git", "diff", "--numstat", "HEAD", "--", path],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    fields = completed.stdout.split("\t")
    if len(fields) < 2:
        return None
    try:
        return int(fields[0]), int(fields[1])
    except ValueError:
        # A binary file reports "-" for both counts.
        return None


def git_worktree_entries(root: Path) -> list[tuple[Path, str, str]]:
    """Every checkout of this repository, with its branch and its commit.

    One listing carries both, which matters: this repository has 154 worktrees,
    and asking git about them one at a time cost seconds every few seconds.
    """
    try:
        completed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    entries: list[tuple[Path, str, str]] = []
    pending: Path | None = None
    head = ""
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            if pending is not None:
                entries.append((pending, "", head))
            head = ""
            try:
                candidate = canonical_root(line[len("worktree ") :])
            except OSError:
                pending = None
                continue
            pending = candidate if candidate.is_dir() else None
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :].strip()
        elif line.startswith("branch ") and pending is not None:
            entries.append((pending, line[len("branch ") :].strip(), head))
            pending = None
            head = ""
        elif not line.strip() and pending is not None:
            entries.append((pending, "", head))
            pending = None
            head = ""
    if pending is not None:
        entries.append((pending, "", head))
    return entries


def commit_times(root: Path, revisions: Iterable[str]) -> dict[str, int]:
    """When each of these commits was made, in one question rather than many.

    A detached checkout has no branch to look up, and this repository keeps
    thirteen of them; one git process each was half a second.
    """
    wanted = sorted({revision for revision in revisions if revision})
    if not wanted:
        return {}
    try:
        completed = subprocess.run(
            ["git", "log", "--no-walk", "--format=%H %ct", *wanted],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    times: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        revision, _, stamp = line.partition(" ")
        try:
            times[revision.strip()] = int(stamp) * 1000
        except ValueError:
            continue
    return times


def git_worktree_paths(root: Path) -> list[Path]:
    """List every checkout of the repository that contains root."""
    return [path for path, _, _ in git_worktree_entries(root)]


def branch_commit_times(root: Path) -> dict[str, int]:
    """When every branch in this repository last got a commit, in one question.

    Asking per worktree meant one git process each. Here that was 154 of them,
    37 ms apiece, on the loop that also draws the pane.
    """
    try:
        completed = subprocess.run(
            ["git", "for-each-ref", "--format=%(committerdate:unix) %(refname)"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    times: dict[str, int] = {}
    for line in completed.stdout.splitlines():
        stamp, _, refname = line.partition(" ")
        try:
            times[refname.strip()] = int(stamp) * 1000
        except ValueError:
            continue
    return times


def git_commit_detail(root: Path, state: dict[str, str]) -> str:
    subject = ""
    try:
        completed = subprocess.run(
            ["git", "log", "-1", "--format=%s", state["oid"]],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode == 0:
            subject = completed.stdout.strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    prefix = state["short_oid"]
    return f"{prefix} · {subject}" if subject else prefix


def read_new_events(path: Path, position: int) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    try:
        size = path.stat().st_size
        if size < position:
            position = 0
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(position)
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("schema") == SCHEMA:
                    records.append(value)
            return records, handle.tell()
    except OSError:
        return [], position


def latest_events(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    records, _ = read_new_events(path, 0)
    return records[-limit:]


def crop(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if terminal_cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    budget = width - 1
    cropped: list[str] = []
    used = 0
    for cluster in display_clusters(text):
        cluster_width = terminal_cell_width(cluster)
        if used + cluster_width > budget:
            break
        cropped.append(cluster)
        used += cluster_width
    return "".join(cropped) + "…"


def display_clusters(text: str) -> Iterable[str]:
    start = 0
    index = 1
    while index < len(text):
        character = text[index]
        codepoint = ord(character)
        is_extender = bool(unicodedata.combining(character)) or (
            0xFE00 <= codepoint <= 0xFE0F
            or 0x1F3FB <= codepoint <= 0x1F3FF
            or 0xE0020 <= codepoint <= 0xE007F
        )
        if is_extender:
            index += 1
            continue
        if character == "\u200d" and index + 1 < len(text):
            index += 2
            continue
        yield text[start:index]
        start = index
        index += 1
    if start < len(text):
        yield text[start:]


def terminal_cell_width(text: str) -> int:
    from wcwidth import wcswidth

    measured = wcswidth(text)
    return measured if measured >= 0 else len(text)


def event_style(event: dict[str, Any]) -> tuple[str, str]:
    status = event.get("status")
    kind = event.get("kind")
    if status == "failed":
        return "×", ANSI["red"]
    if status == "running":
        return "●", ANSI["yellow"]
    if status == "unknown":
        return "?", ANSI["yellow"]
    if kind == "github":
        github_state = event.get("github_state")
        if github_state == "MERGED":
            return "⇉", ANSI["green"]
        if github_state == "CLOSED":
            return "×", ANSI["yellow"]
        return "↗", ANSI["blue"]
    styles = {
        "file": ("✎", ANSI["cyan"]),
        "config": ("⚙", ANSI["magenta"]),
        "test": ("✓", ANSI["green"]),
        "branch": ("⑂", ANSI["blue"]),
        "worktree": ("⌘", ANSI["blue"]),
        "commit": ("◆", ANSI["magenta"]),
        "push": ("↑", ANSI["cyan"]),
        "pr": ("↗", ANSI["blue"]),
        "merge": ("⇉", ANSI["green"]),
        "issue": ("◈", ANSI["yellow"]),
        "session": ("◇", ANSI["blue"]),
        "command": ("×", ANSI["red"]),
    }
    return styles.get(str(kind), ("·", ANSI["dim"]))


def transcript_lines(handle: IO[bytes]) -> Iterable[bytes]:
    """Yield whole lines and leave the handle parked before a partial one.

    Agents append transcripts while Side Dog reads them, so the final line can
    still be mid-write. Stopping before it keeps the caller's saved position on
    a record boundary instead of skipping that record for good.
    """
    while True:
        line_start = handle.tell()
        raw_line = handle.readline()
        if not raw_line:
            return
        if not raw_line.endswith(b"\n"):
            handle.seek(line_start)
            return
        yield raw_line


def clear_session_path_cache() -> None:
    SESSION_PATH_CACHE.clear()
    SESSION_PATH_MISSES.clear()


def _remember_session_path(cache: dict[str, Any], key: str, value: Any) -> None:
    cache.pop(key, None)
    while len(cache) >= SESSION_PATH_CACHE_LIMIT:
        del cache[next(iter(cache))]
    cache[key] = value


def resolve_session_path(key: str, locate: Callable[[], Path | None]) -> Path | None:
    """Cache transcripts that exist and keep retrying the ones still unwritten.

    Agents announce a session before its transcript file lands on disk, so a
    lookup cached forever would leave that session without model or effort for
    the life of the watcher.
    """
    cached = SESSION_PATH_CACHE.get(key)
    if cached is not None:
        if cached.exists():
            return cached
        del SESSION_PATH_CACHE[key]
    now = time.monotonic()
    last_miss = SESSION_PATH_MISSES.get(key)
    if last_miss is not None and now - last_miss < SESSION_PATH_RETRY_SECONDS:
        return None
    path = locate()
    if path is None:
        _remember_session_path(SESSION_PATH_MISSES, key, now)
        return None
    SESSION_PATH_MISSES.pop(key, None)
    _remember_session_path(SESSION_PATH_CACHE, key, path)
    return path


def codex_session_path(session_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", session_id):
        return None
    return resolve_session_path(
        f"codex:{session_id}", lambda: _locate_codex_session(session_id)
    )


def _locate_codex_session(session_id: str) -> Path | None:
    configured_root = os.environ.get("CODEX_HOME")
    codex_root = (
        Path(configured_root).expanduser()
        if configured_root
        else Path.home() / ".codex"
    )
    patterns = (
        codex_root / "sessions",
        codex_root / "archived_sessions",
    )
    for directory in patterns:
        try:
            match = next(directory.rglob(f"*{session_id}.jsonl"), None)
        except OSError:
            continue
        if match is not None:
            return match
    return None


def load_codex_metadata(session_id: str) -> dict[str, str]:
    path = codex_session_path(session_id)
    if path is None:
        return {}
    cache_key = os.fspath(path)
    position, metadata = CODEX_METADATA_CACHE.get(cache_key, (0, {}))
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            if position > size:
                position, metadata = 0, {}
            handle.seek(position)
            for raw_line in transcript_lines(handle):
                if b'"turn_context"' not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "turn_context":
                    continue
                context = record.get("payload")
                if not isinstance(context, dict):
                    continue
                model = context.get("model")
                effort = context.get("effort") or context.get("reasoning_effort")
                if isinstance(model, str) and model:
                    metadata["model"] = model
                if isinstance(effort, str) and effort:
                    metadata["effort"] = effort
            position = handle.tell()
    except OSError:
        return dict(metadata)
    CODEX_METADATA_CACHE[cache_key] = (position, metadata)
    return dict(metadata)


def pi_session_path(session_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", session_id):
        return None
    return resolve_session_path(
        f"pi:{session_id}", lambda: _locate_pi_session(session_id)
    )


def _locate_pi_session(session_id: str) -> Path | None:
    root = pi_sessions_root()
    try:
        return next(root.rglob(f"*{session_id}.jsonl"), None)
    except OSError:
        return None


def load_pi_metadata(session_id: str) -> dict[str, str]:
    """Model and reasoning effort for a Pi session, read from its transcript.

    Pi records the chosen model as `model_change` records and the reasoning
    effort as `thinking_level_change` records, so the latest of each names the
    session the way `turn_context` names a Codex one.
    """
    path = pi_session_path(session_id)
    if path is None:
        return {}
    cache_key = os.fspath(path)
    position, metadata = PI_METADATA_CACHE.get(cache_key, (0, {}))
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            if position > size:
                position, metadata = 0, {}
            handle.seek(position)
            for raw_line in transcript_lines(handle):
                if (
                    b'"model_change"' not in raw_line
                    and b'"thinking_level_change"' not in raw_line
                ):
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "model_change":
                    model = record.get("modelId") or record.get("model")
                    if isinstance(model, str) and model:
                        metadata["model"] = model
                elif record.get("type") == "thinking_level_change":
                    effort = record.get("thinkingLevel")
                    if isinstance(effort, str) and effort:
                        metadata["effort"] = effort
            position = handle.tell()
    except OSError:
        return dict(metadata)
    PI_METADATA_CACHE[cache_key] = (position, metadata)
    return dict(metadata)


@dataclass
class NativeAgentStream:
    session_id: str
    path: Path
    position: int
    agent_root: str = ""
    model: str = ""
    effort: str = ""
    turn_id: str = ""
    pending_commands: deque[tuple[str, str, str]] = field(default_factory=deque)
    completed_commands: deque[tuple[str, str, str, str]] = field(
        default_factory=lambda: deque(maxlen=256)
    )


def _json_value_after(source: str, field: str) -> Any:
    match = re.search(
        rf"(?P<quote>[\"']?)\b{re.escape(field)}\b(?P=quote)\s*:\s*",
        source,
    )
    if match is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(source[match.end() :])
    except json.JSONDecodeError:
        return None
    return value


def codex_exec_request(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Extract only command and cwd from a native Codex exec request."""
    if payload.get("type") != "custom_tool_call" or payload.get("name") != "exec":
        return None
    raw = payload.get("input")
    decoded: Any = raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if not isinstance(decoded, dict):
            command = _json_value_after(raw, "cmd")
            workdir = _json_value_after(raw, "workdir")
            if isinstance(command, str):
                return command, workdir if isinstance(workdir, str) else ""
            return None
    if not isinstance(decoded, dict):
        return None
    command = decoded.get("cmd")
    workdir = decoded.get("workdir")
    if not isinstance(command, str):
        return None
    return command, workdir if isinstance(workdir, str) else ""


def _codex_command_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    if len(value) >= 3 and value[1] in {"-c", "-lc"}:
        return value[2]
    return shlex.join(value)


def _codex_cwd(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if value.startswith("file:"):
        parsed = urlsplit(value)
        return unquote(parsed.path)
    return value


def _native_path_matches_root(
    root: Path, raw_path: str, fallback_root: str = ""
) -> bool:
    if not raw_path:
        try:
            return bool(fallback_root) and canonical_root(fallback_root) == root
        except (OSError, ValueError):
            return False
    try:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() and fallback_root:
            candidate = Path(fallback_root).expanduser() / candidate
        path = candidate.resolve(strict=False)
        probe = path if path.is_dir() else path.parent
        reported_root = git_worktree_root(os.fspath(probe))
        if reported_root:
            return canonical_root(reported_root) == root
        return path == root or path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _record_time(record: dict[str, Any], epoch_field: str | None = None) -> dict[str, Any]:
    epoch = record.get(epoch_field) if epoch_field else None
    if isinstance(epoch, int):
        instant = datetime.fromtimestamp(epoch / 1000, timezone.utc)
        return {
            "timestamp": instant.isoformat(timespec="milliseconds"),
            "epoch_ms": epoch,
        }
    raw = record.get("timestamp")
    if isinstance(raw, str):
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return {}
        return {"timestamp": raw, "epoch_ms": int(instant.timestamp() * 1000)}
    return {}


def _stream_context(stream: NativeAgentStream) -> dict[str, str]:
    context = {"agent": "codex", "session_id": stream.session_id}
    if stream.model:
        context["model"] = stream.model
    if stream.effort:
        context["effort"] = stream.effort
    if stream.turn_id:
        context["prompt_id"] = stream.turn_id
    return context


def _command_key(command: str, workdir: str) -> tuple[str, str]:
    directory = os.fspath(Path(workdir).expanduser()) if workdir else ""
    return " ".join(command.split()), directory


def _matching_pending_operation(
    stream: NativeAgentStream, command: str, workdir: str
) -> str | None:
    wanted_command, wanted_workdir = _command_key(command, workdir)
    for pending in list(stream.pending_commands):
        pending_command, pending_workdir, operation = pending
        if pending_command != wanted_command:
            continue
        if pending_workdir and wanted_workdir and pending_workdir != wanted_workdir:
            continue
        stream.pending_commands.remove(pending)
        return operation
    return None


def _pending_command_for_call(
    stream: NativeAgentStream, call_id: str
) -> tuple[str, str] | None:
    for pending in list(stream.pending_commands):
        command, workdir, operation = pending
        if operation != call_id:
            continue
        stream.pending_commands.remove(pending)
        return command, workdir
    return None


def _matching_completed_operation(
    stream: NativeAgentStream, command: str, workdir: str
) -> tuple[str, str] | None:
    wanted_command, wanted_workdir = _command_key(command, workdir)
    for completed in list(stream.completed_commands):
        prior_command, prior_workdir, operation, status = completed
        if prior_command != wanted_command:
            continue
        if prior_workdir and wanted_workdir and prior_workdir != wanted_workdir:
            continue
        stream.completed_commands.remove(completed)
        return operation, status
    return None


def _custom_tool_output_status(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    decoded: Any = output
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            decoded = None
    exit_code = decoded.get("exit_code") if isinstance(decoded, dict) else None
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "success" if exit_code == 0 else "failed"
    status = str(payload.get("status") or "").casefold()
    if status in {"completed", "success", "succeeded"}:
        return "success"
    if status in {"failed", "error", "cancelled"}:
        return "failed"
    return "unknown"


def _append_native_tool_events(
    root: Path,
    payload: dict[str, Any],
    status: str,
    source_prefix: str,
    timing: dict[str, Any],
) -> int:
    count = 0
    for index, event in enumerate(normalized_tool_events(payload, root, status=status)):
        native = {
            **event,
            **timing,
            "source_event_id": f"{source_prefix}:{index}",
        }
        count += int(append_event_once(root, native))
    return count


def _poll_codex_record(
    root: Path, stream: NativeAgentStream, record: dict[str, Any]
) -> int:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return 0
    record_type = record.get("type")
    if record_type == "turn_context":
        model = payload.get("model")
        effort = payload.get("effort") or payload.get("reasoning_effort")
        if isinstance(model, str):
            stream.model = model
        if isinstance(effort, str):
            stream.effort = effort
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str):
            stream.turn_id = turn_id
        return 0
    if record_type == "response_item":
        if payload.get("type") == "custom_tool_call_output":
            call_id = payload.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                return 0
            pending = _pending_command_for_call(stream, call_id)
            if pending is None:
                return 0
            command, workdir = pending
            if not _native_path_matches_root(root, workdir, stream.agent_root):
                return 0
            status = _custom_tool_output_status(payload)
            stream.completed_commands.append((command, workdir, call_id, status))
            tool_payload = {
                **_stream_context(stream),
                "tool_use_id": call_id,
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
            return _append_native_tool_events(
                root,
                tool_payload,
                status,
                f"codex:{stream.session_id}:call:{call_id}:output",
                _record_time(record),
            )
        request = codex_exec_request(payload)
        if request is None:
            return 0
        command, workdir = request
        call_id = payload.get("call_id") or payload.get("id")
        if not isinstance(call_id, str) or not call_id:
            return 0
        stream.pending_commands.append((*_command_key(command, workdir), call_id))
        if not _native_path_matches_root(root, workdir, stream.agent_root):
            return 0
        tool_payload = {
            **_stream_context(stream),
            "tool_use_id": call_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        return _append_native_tool_events(
            root,
            tool_payload,
            "running",
            f"codex:{stream.session_id}:call:{call_id}:running",
            _record_time(record),
        )
    if record_type != "event_msg" or payload.get("type") != "item_completed":
        return 0
    item = payload.get("item")
    if not isinstance(item, dict):
        return 0
    turn_id = payload.get("turn_id") or record.get("turn_id")
    if isinstance(turn_id, str):
        stream.turn_id = turn_id
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        return 0
    item_type = item.get("type")
    timing = _record_time(payload, "completed_at_ms") or _record_time(record)
    started = payload.get("started_at_ms")
    if isinstance(started, int):
        timing["started_epoch_ms"] = started
    if item_type == "SubAgentActivity":
        if not _native_path_matches_root(root, "", stream.agent_root):
            return 0
        activity = str(item.get("kind") or "").casefold()
        lifecycle = {
            "started": ("running", "Subagent started"),
            "interacted": ("running", "Subagent active"),
            "completed": ("success", "Subagent completed"),
            "failed": ("failed", "Subagent failed"),
            "cancelled": ("failed", "Subagent cancelled"),
        }
        if activity not in lifecycle:
            return 0
        status, title = lifecycle[activity]
        raw_agent_path = item.get("agent_path")
        detail = (
            Path(raw_agent_path).name
            if isinstance(raw_agent_path, str) and raw_agent_path
            else "subagent"
        )
        detail = " ".join(detail.split())[:80] or "subagent"
        agent_reference = str(
            item.get("agent_thread_id") or raw_agent_path or item_id
        )
        identifier = f"subagent:{agent_reference}"
        return int(
            append_event_once(
                root,
                {
                    **hook_context(_stream_context(stream)),
                    **timing,
                    "operation_id": identifier,
                    "group_id": identifier,
                    "kind": "session",
                    "status": status,
                    "title": title,
                    "detail": detail,
                    "source_event_id": (
                        f"codex:{stream.session_id}:item:{item_id}:subagent"
                    ),
                },
            )
        )
    if item_type == "CommandExecution":
        command = _codex_command_text(item.get("command"))
        if command is None:
            return 0
        workdir = _codex_cwd(item.get("cwd"))
        if not _native_path_matches_root(root, workdir, stream.agent_root):
            return 0
        exit_code = item.get("exit_code")
        if exit_code == 0:
            status = "success"
        elif isinstance(exit_code, int) or item.get("status") == "failed":
            status = "failed"
        else:
            status = "unknown"
        operation = _matching_pending_operation(stream, command, workdir)
        if operation is None:
            completed = _matching_completed_operation(stream, command, workdir)
            if completed is not None:
                operation, completed_status = completed
                if completed_status == status:
                    return 0
            else:
                operation = item_id
        tool_payload = {
            **_stream_context(stream),
            "tool_use_id": operation,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        return _append_native_tool_events(
            root,
            tool_payload,
            status,
            f"codex:{stream.session_id}:item:{item_id}:complete",
            timing,
        )
    if item_type != "FileChange":
        return 0
    changes = item.get("changes")
    if not isinstance(changes, dict):
        return 0
    status = "failed" if item.get("status") == "failed" else "success"
    count = 0
    for index, (raw_path, change) in enumerate(changes.items()):
        if not isinstance(raw_path, str) or not _native_path_matches_root(
            root, raw_path, stream.agent_root
        ):
            continue
        path = relative_display(raw_path, root)
        config = is_config(path)
        change_kind = change.get("type") if isinstance(change, dict) else "update"
        if status == "failed":
            title = "Config write failed" if config else "File write failed"
        elif change_kind == "delete":
            title = "Removed config" if config else "Removed file"
        else:
            title = "Wrote config" if config else "Wrote file"
        counts = git_line_changes(root, path) if status == "success" else None
        count += int(
            append_event_once(
                root,
                {
                    **hook_context(_stream_context(stream)),
                    **timing,
                    "operation_id": f"{item_id}:{index}:file",
                    "group_id": item_id,
                    "kind": "config" if config else "file",
                    "status": status,
                    "title": title,
                    "detail": path,
                    **(
                        {"lines_added": counts[0], "lines_removed": counts[1]}
                        if counts
                        else {}
                    ),
                    "source_event_id": (
                        f"codex:{stream.session_id}:item:{item_id}:{index}:complete"
                    ),
                },
            )
        )
    return count


def sync_codex_streams(
    root: Path,
    identities: dict[str, dict[str, str]],
    streams: dict[str, NativeAgentStream],
) -> dict[str, int]:
    attached: dict[str, int] = {}
    for identity in identities.values():
        if identity.get("agent") != "codex":
            continue
        session_id = identity.get("session_id")
        if not session_id:
            continue
        if session_id in streams:
            streams[session_id].agent_root = identity.get(
                "root", streams[session_id].agent_root
            )
            streams[session_id].model = identity.get("model", streams[session_id].model)
            streams[session_id].effort = identity.get("effort", streams[session_id].effort)
            continue
        path = codex_session_path(session_id)
        if path is None:
            continue
        position = load_native_stream_position(root, session_id, path)
        streams[session_id] = NativeAgentStream(
            session_id=session_id,
            path=path,
            position=position,
            agent_root=identity.get("root", ""),
            model=identity.get("model", ""),
            effort=identity.get("effort", ""),
        )
        attached[session_id] = position
    return attached


def announce_native_history(
    root: Path, stream: NativeAgentStream, initial_position: int
) -> None:
    indexed = native_event_count(root, stream.session_id)
    if indexed == 0:
        return
    backfilled = initial_position == 0
    event_word = "event" if indexed == 1 else "events"
    milestone_id = f"codex:{stream.session_id}:history-backfill-complete-v3"
    append_event_once(
        root,
        {
            **hook_context(_stream_context(stream)),
            "turn_id": "",
            "operation_id": milestone_id,
            "group_id": milestone_id,
            "kind": "session",
            "status": "success",
            "title": "Side Dog caught up on earlier activity",
            "detail": (
                f"{indexed} earlier {event_word} added"
                if backfilled
                else f"{indexed} earlier {event_word} already saved"
            ),
            "source_event_id": milestone_id,
        },
    )


def poll_native_agent_events(
    root: Path,
    identities: dict[str, dict[str, str]],
    streams: dict[str, NativeAgentStream],
) -> int:
    """Ingest privacy-filtered native Codex events; Claude arrives via hooks."""
    attached = sync_codex_streams(root, identities, streams)
    count = 0
    for stream in streams.values():
        try:
            with stream.path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                if stream.position > size:
                    stream.position = 0
                handle.seek(stream.position)
                while True:
                    line_start = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line:
                        stream.position = handle.tell()
                        break
                    if not raw_line.endswith(b"\n"):
                        stream.position = line_start
                        break
                    try:
                        record = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        stream.position = handle.tell()
                        continue
                    if isinstance(record, dict):
                        count += _poll_codex_record(root, stream, record)
                    stream.position = handle.tell()
        except OSError:
            continue
        save_native_stream_position(
            root, stream.session_id, stream.path, stream.position
        )
        if stream.session_id in attached:
            announce_native_history(root, stream, attached[stream.session_id])
    return count


def claude_session_path(session_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", session_id):
        return None
    return resolve_session_path(
        f"claude:{session_id}", lambda: _locate_claude_session(session_id)
    )


def _locate_claude_session(session_id: str) -> Path | None:
    directory = Path.home() / ".claude" / "projects"
    try:
        return next(directory.rglob(f"{session_id}.jsonl"), None)
    except OSError:
        return None


def load_claude_metadata(session_id: str) -> dict[str, str]:
    path = claude_session_path(session_id)
    if path is None:
        return {}
    cache_key = os.fspath(path)
    position, metadata = CLAUDE_METADATA_CACHE.get(cache_key, (0, {}))
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            if position > size:
                position, metadata = 0, {}
            handle.seek(position)
            for raw_line in transcript_lines(handle):
                if b'"model"' not in raw_line and b'"effort"' not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                message = record.get("message")
                message = message if isinstance(message, dict) else {}
                model = message.get("model") or record.get("model")
                effort = (
                    record.get("effort")
                    or record.get("reasoning_effort")
                    or message.get("effort")
                    or message.get("reasoning_effort")
                )
                if isinstance(model, str) and model:
                    metadata["model"] = model
                if isinstance(effort, str) and effort:
                    metadata["effort"] = effort
            position = handle.tell()
    except OSError:
        return dict(metadata)
    CLAUDE_METADATA_CACHE[cache_key] = (position, metadata)
    return dict(metadata)


CLAUDE_WORKING_SECONDS = 60.0
CLAUDE_SURFACES = {
    "cli": "terminal",
    "claude-desktop": "desktop",
    "claude-vscode": "VS Code",
}


def claude_session_registry() -> list[dict[str, Any]]:
    """Every Claude Code session running on this machine, whatever launched it.

    Claude Code writes one file per live session to ~/.claude/sessions, holding
    its process id, session id and working folder. Terminal, desktop app and
    editor sessions all register the same way, so this sees agents Herdr
    cannot: Herdr only knows about terminal panes.
    """
    directory = Path.home() / ".claude" / "sessions"
    sessions: list[dict[str, Any]] = []
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return []
    for path in files:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or not record.get("sessionId"):
            continue
        if not process_is_alive(record.get("pid")):
            # A session that died without tidying up leaves its file behind.
            continue
        sessions.append(record)
    return sessions


def process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def claude_session_status(session_id: str, now: float | None = None) -> str:
    """Working while its transcript is still being written, otherwise idle."""
    path = claude_session_path(session_id)
    if path is None:
        return "unknown"
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return "unknown"
    return "working" if age <= CLAUDE_WORKING_SECONDS else "idle"


def claude_identities(root: Path) -> dict[str, dict[str, str]]:
    """Claude sessions working in this folder, read from Claude's own registry."""
    identities: dict[str, dict[str, str]] = {}
    watched_common_dir = git_common_dir(os.fspath(root))
    for record in claude_session_registry():
        raw_cwd = record.get("cwd")
        if not isinstance(raw_cwd, str):
            continue
        try:
            agent_root = canonical_root(raw_cwd)
            reported = git_worktree_root(os.fspath(agent_root))
            associated = canonical_root(reported) if reported else agent_root
            same_repository = bool(
                watched_common_dir
                and git_common_dir(os.fspath(agent_root)) == watched_common_dir
            )
            if associated != root and not same_repository:
                continue
        except OSError:
            continue
        session_id = str(record["sessionId"])
        entrypoint = str(record.get("entrypoint") or "")
        identities[session_id] = {
            "agent": "claude-code",
            "root": os.fspath(associated),
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "working_root": os.fspath(agent_root),
            "session_id": session_id,
            "status": claude_session_status(session_id),
            "label": CLAUDE_SURFACES.get(entrypoint, entrypoint or "Claude"),
            **load_claude_metadata(session_id),
        }
    return identities


def herdr_agents(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    agents = snapshot.get("agents")
    if not isinstance(agents, list):
        return []
    return [
        agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("agent") in HERDR_CODING_AGENTS
    ]


def herdr_workspaces(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Herdr's workspaces, which it already labels after the repository."""
    workspaces = snapshot.get("workspaces")
    if not isinstance(workspaces, list):
        return []
    return [
        workspace
        for workspace in workspaces
        if isinstance(workspace, dict) and workspace.get("label")
    ]


def clear_herdr_snapshot_cache() -> None:
    global HERDR_SNAPSHOT_CACHE
    with HERDR_SNAPSHOT_LOCK:
        HERDR_SNAPSHOT_CACHE = None


def herdr_snapshot() -> dict[str, Any]:
    """Herdr's whole snapshot: its panes, the agents in them, its workspaces.

    Identities want the agents, named spaces want the workspaces, and discovery
    wants both, so one reading is shared rather than one taken each. Every
    failure - no Herdr, a timeout, a shape that has moved on - is an empty
    snapshot, because Herdr is one of three sources and never a prerequisite.
    """
    return read_herdr_snapshot()[0]


def read_herdr_snapshot() -> tuple[dict[str, Any], str | None]:
    """One release-shaped Herdr snapshot a second, shared by every caller."""
    global HERDR_SNAPSHOT_CACHE
    now = time.monotonic()
    with HERDR_SNAPSHOT_LOCK:
        cached = HERDR_SNAPSHOT_CACHE
        if cached is not None and now - cached[0] < HERDR_SNAPSHOT_TTL_SECONDS:
            return cached[1], cached[2]
        result: tuple[dict[str, Any], str | None]
        if shutil.which("herdr") is None:
            result = ({}, "Herdr is not installed or is not on PATH")
        else:
            try:
                completed = subprocess.run(
                    ["herdr", "api", "snapshot"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if completed.returncode != 0:
                    detail = completed.stderr.strip().splitlines()
                    result = (
                        {},
                        detail[-1] if detail else "Herdr snapshot request failed",
                    )
                else:
                    document = json.loads(completed.stdout)
                    snapshot = document["result"]["snapshot"]
                    if not isinstance(snapshot, dict):
                        raise TypeError("Herdr snapshot must be an object")
                    if not isinstance(snapshot.get("agents"), list):
                        raise TypeError("Herdr snapshot agents must be a list")
                    result = (snapshot, None)
            except (
                OSError,
                subprocess.TimeoutExpired,
                json.JSONDecodeError,
                KeyError,
                TypeError,
            ) as error:
                result = ({}, f"Herdr snapshot unavailable: {error}")
        HERDR_SNAPSHOT_CACHE = (time.monotonic(), result[0], result[1])
        return result


def load_herdr_snapshot() -> tuple[list[dict[str, Any]], str | None]:
    """Herdr's agents, and why there are none when there are none."""
    snapshot, error = read_herdr_snapshot()
    agents = snapshot.get("agents")
    if not isinstance(agents, list):
        return [], error
    return [agent for agent in agents if isinstance(agent, dict)], error


def _herdr_agent_root(agent: dict[str, Any]) -> Path | None:
    if agent.get("agent") not in HERDR_CODING_AGENTS:
        return None
    raw_cwd = agent.get("foreground_cwd") or agent.get("cwd")
    if not isinstance(raw_cwd, str):
        return None
    try:
        working_root = canonical_root(raw_cwd)
        reported = git_worktree_root(os.fspath(working_root))
        return canonical_root(reported) if reported else working_root
    except OSError:
        return None


def herdr_session_roots() -> tuple[list[Path], str | None]:
    """Return live coding-agent roots, with working agents taking precedence."""
    agents, error = load_herdr_snapshot()
    inherited_workspace = os.environ.get("HERDR_WORKSPACE_ID", "")
    priority = {"working": 0, "blocked": 1, "done": 2, "idle": 3, "unknown": 4}
    ranked: dict[Path, int] = {}
    for agent in agents:
        if inherited_workspace and agent.get("workspace_id") != inherited_workspace:
            continue
        root = _herdr_agent_root(agent)
        if root is None or root_is_missing(root):
            continue
        status = str(agent.get("agent_status", "unknown")).casefold()
        ranked[root] = min(ranked.get(root, 99), priority.get(status, 4))
    return sorted(ranked, key=lambda root: (ranked[root], os.fspath(root))), error


def herdr_identities_for_root(
    root: Path, agents: Iterable[dict[str, Any]]
) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    watched_common_dir = git_common_dir(os.fspath(root))
    for agent in agents:
        if agent.get("agent") not in HERDR_CODING_AGENTS:
            # Herdr sees every pane; only coding agents get a row.
            continue
        raw_cwd = agent.get("foreground_cwd") or agent.get("cwd")
        if not isinstance(raw_cwd, str):
            continue
        try:
            agent_root = canonical_root(raw_cwd)
            reported_worktree_root = git_worktree_root(os.fspath(agent_root))
            associated_root = (
                canonical_root(reported_worktree_root)
                if reported_worktree_root
                else agent_root
            )
            same_root = associated_root == root
            same_repository = bool(
                watched_common_dir
                and git_common_dir(os.fspath(agent_root)) == watched_common_dir
            )
            if not same_root and not same_repository:
                continue
        except OSError:
            continue
        pane_id = str(agent.get("pane_id", ""))
        identity = {
            "agent": normalize_agent(agent.get("agent")),
            "root": os.fspath(associated_root),
            "pane_id": pane_id,
            "workspace_id": str(agent.get("workspace_id", "")),
            "tab_id": str(agent.get("tab_id", "")),
            "working_root": os.fspath(agent_root),
            "status": str(agent.get("agent_status", "unknown")),
            "label": str(
                agent.get("terminal_title_stripped")
                or agent.get("terminal_title")
                or pane_id
                or agent_label(agent.get("agent"))
            ),
        }
        session = agent.get("agent_session")
        if isinstance(session, dict) and isinstance(session.get("value"), str):
            session_id = session["value"]
            identity["session_id"] = session_id
            if identity["agent"] == "codex":
                identity.update(load_codex_metadata(session_id))
            elif identity["agent"] == "claude-code":
                identity.update(load_claude_metadata(session_id))
            elif identity["agent"] == "pi":
                identity.update(load_pi_metadata(session_id))
            identities[session_id] = identity
        if pane_id:
            identities[f"pane:{pane_id}"] = identity
    return identities


def load_herdr_identities(root: Path) -> dict[str, dict[str, str]]:
    agents, _ = load_herdr_snapshot()
    return herdr_identities_for_root(root, agents)


def load_github_pr(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    if shutil.which("gh") is None:
        return None, "gh is not installed"
    environment = dict(os.environ)
    environment.update({"GH_PAGER": "cat", "NO_COLOR": "1"})
    try:
        completed = subprocess.run(
            ["gh", "pr", "view", "--json", GITHUB_PR_FIELDS],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    if completed.returncode != 0:
        message = completed.stderr.strip().splitlines()
        return None, message[-1] if message else "no pull request for current branch"
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, "gh returned invalid JSON"
    if not isinstance(raw, dict) or not isinstance(raw.get("number"), int):
        return None, "gh returned an incomplete pull request"
    return normalize_github_pr(raw), None


def is_definitive_no_pr(error: str | None) -> bool:
    if not error:
        return False
    message = error.casefold()
    return any(
        marker in message
        for marker in (
            "no pull requests found for branch",
            "no pull requests found for the current branch",
            "could not resolve to a pullrequest",
        )
    )


def display_github_detail(status: dict[str, Any]) -> str:
    display_status = dict(status)
    display_status["title"] = display_conventional_subject(status.get("title"))
    return github_detail(display_status)


def github_progress_title(
    number: Any, status: dict[str, Any], previous: dict[str, Any]
) -> str | None:
    """Name what moved, so a line says more than "status updated"."""
    phase = github_ci_phase(status)
    if phase != github_ci_phase(previous):
        return {
            "pending": f"PR #{number} checks started",
            "passed": f"PR #{number} checks passed",
            "failed": f"PR #{number} checks failed",
        }[phase]
    review = str(status.get("review") or "")
    if review != str(previous.get("review") or ""):
        return {
            "APPROVED": f"PR #{number} approved",
            "CHANGES_REQUESTED": f"PR #{number} changes requested",
        }.get(review)
    return None


def github_event(
    status: dict[str, Any], previous: dict[str, Any] | None, context: dict[str, Any]
) -> dict[str, Any]:
    number = status["number"]
    state = status["state"]
    if previous is None:
        title = f"PR #{number} confirmed"
    elif state != previous.get("state"):
        verb = {"MERGED": "merged", "CLOSED": "closed", "OPEN": "reopened"}.get(
            state, "changed"
        )
        title = f"PR #{number} {verb}"
    else:
        title = (
            github_progress_title(number, status, previous)
            or f"PR #{number} status updated"
        )
    fingerprint = github_fingerprint(status)
    return {
        **context,
        "agent": context.get("agent", "github"),
        "operation_id": f"github-pr-{number}:{fingerprint}",
        "group_id": context.get("group_id", f"github-pr-{number}"),
        "kind": "github",
        "status": "success",
        "title": title,
        "detail": github_detail(status),
        "github_state": state,
        "github": status,
        "github_fingerprint": fingerprint,
    }


def matches_session_filter(
    event: dict[str, Any], identity: dict[str, str], session_filter: str | None
) -> bool:
    if not session_filter:
        return True
    needle = session_filter.casefold()
    values = (
        str(event.get("session_id", "")),
        identity.get("pane_id", ""),
        identity.get("label", ""),
    )
    return any(needle in value.casefold() for value in values)


def format_duration(event: dict[str, Any], now_ms: int) -> str:
    started = event.get("started_epoch_ms")
    ended = event.get("epoch_ms")
    if not isinstance(started, int):
        return ""
    if event.get("status") == "running":
        ended = now_ms
    if not isinstance(ended, int) or ended < started:
        return ""
    seconds = (ended - started) / 1000
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, remainder = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def display_time(event: dict[str, Any]) -> str:
    def render_timestamp(value: Any) -> str:
        try:
            return datetime.fromisoformat(str(value)).astimezone().strftime("%H:%M")
        except ValueError:
            return "--:--"

    first = event.get("first_timestamp")
    latest = render_timestamp(event.get("timestamp", ""))
    if first and int(event.get("repeat_count", 1)) > 1:
        return f"{render_timestamp(first)}→{latest}"
    return latest


def display_title(event: dict[str, Any]) -> str:
    title = str(event.get("title", "Activity"))
    compact = {
        "File changed": "changed",
        "Config changed": "changed",
        "File removed": "removed",
        "Config removed": "removed",
        "Writing file": "writing",
        "Writing config": "writing",
        "Wrote file": "wrote",
        "Wrote config": "wrote",
        "File write failed": "write failed",
        "Config write failed": "write failed",
        "Running tests": "running",
        "Tests passed": "passed",
        "Tests failed": "failed",
        "Commit created": "committed",
        "Branch pushed": "pushed",
        "Creating commit": "committing",
        "Pushing branch": "pushing",
    }
    return compact.get(title, title)


def line_change_summary(event: dict[str, Any]) -> str:
    """How much of a file changed, against the last commit."""
    added = event.get("lines_added")
    removed = event.get("lines_removed")
    if not isinstance(added, int) or not isinstance(removed, int):
        return ""
    return f"+{added}/-{removed}"


def display_detail(event: dict[str, Any]) -> str:
    github_status = event.get("github")
    if event.get("kind") == "github" and isinstance(github_status, dict):
        return display_github_detail(github_status)
    detail = str(event.get("detail", ""))
    if event.get("kind") in {"commit", "pr"}:
        return display_conventional_subject(detail)
    if changes := line_change_summary(event):
        return f"{detail} · {changes}"
    return detail


def render_event_line(
    event: dict[str, Any],
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
    show_source: bool = True,
    search: str = "",
) -> str:
    when = display_time(event)
    icon, style = event_style(event)
    detail = display_detail(event)
    title = display_title(event)
    duration = format_duration(event, now_ms)
    actor = actor_label(event, identities)
    summary = f"{title} · {detail}" if detail else title
    if actor:
        summary = f"{actor} · {summary}"
    summary = label_summary(event, summary, show_source)
    if duration:
        summary += f" · {duration}"
    repeats = int(event.get("repeat_count", 1))
    suffix = ""
    if repeats > 1:
        suffix = f" · ×{repeats}"
    summary_width = max(4, width - len(when) - 6)
    summary = (
        crop_to_match(summary, max(1, summary_width - len(suffix)), search) + suffix
    )
    if color:
        summary = style_source_label(summary, event, color)
        return (
            f"│ {ANSI['dim']}{when}{ANSI['reset']} "
            f"{style}{icon}{ANSI['reset']} {summary}"
        )
    return f"│ {when} {icon} {summary}"


def group_duration(events: list[dict[str, Any]], now_ms: int) -> str:
    starts = [
        value
        for event in events
        if isinstance(
            value := event.get("started_epoch_ms", event.get("epoch_ms")), int
        )
    ]
    ends = [
        now_ms if event.get("status") == "running" else event.get("epoch_ms")
        for event in events
    ]
    ends = [value for value in ends if isinstance(value, int)]
    if not starts or not ends:
        return ""
    return format_duration(
        {
            "started_epoch_ms": min(starts),
            "epoch_ms": max(ends),
            "status": "success",
        },
        now_ms,
    )


def render_filesystem_burst(
    unit: dict[str, Any], width: int, color: bool, show_source: bool = True
) -> list[str]:
    events = unit["events"]
    latest = max(events, key=event_epoch)
    burst = unit["summary"]
    count = int(burst["count"])
    time_event = {
        "first_timestamp": burst["first_timestamp"],
        "timestamp": burst["timestamp"],
        "repeat_count": count,
    }
    when = display_time(time_event)
    actions = []
    if burst["changes"]:
        actions.append(f"{burst['changes']} changed")
    if burst["removals"]:
        actions.append(f"{burst['removals']} removed")
    paths = burst["paths"]
    lines = f"Files · {' · '.join(actions)} · {len(paths)} paths"
    if burst.get("lines_added") or burst.get("lines_removed"):
        lines += f" · +{burst['lines_added']}/-{burst['lines_removed']}"
    summary = label_summary(latest, lines, show_source)
    summary = crop(summary, max(4, width - len(when) - 6))
    if color:
        summary = style_source_label(summary, latest, color, ANSI["dim"])
        heading = (
            f"│ {ANSI['dim']}{when}{ANSI['reset']} "
            f"{ANSI['cyan']}✎{ANSI['reset']} {ANSI['dim']}{summary}{ANSI['reset']}"
        )
    else:
        heading = f"│ {when} ✎ {summary}"
    top_paths = paths[:3]
    details = [
        f"{path} ×{path_count}" if path_count > 1 else path
        for path, path_count in top_paths
    ]
    if len(paths) > len(top_paths):
        details.append(f"+{len(paths) - len(top_paths)} more")
    detail = crop(" · ".join(details), max(4, width - 6))
    if color:
        child = f"│   {ANSI['dim']}{detail}{ANSI['reset']}"
    else:
        child = f"│   {detail}"
    return [heading, child]


def milestone_label(event: dict[str, Any]) -> str:
    kind = str(event.get("kind", "activity"))
    status = str(event.get("status", "success"))
    labels = {
        "test": {
            "success": "Tests passed",
            "failed": "Tests failed",
            "running": "Tests running",
            "unknown": "Tests finished",
        },
        "commit": {
            "success": "Commit",
            "failed": "Commit failed",
            "running": "Committing",
        },
        "push": {"success": "Push", "failed": "Push failed", "running": "Pushing"},
        "pr": {
            "success": "Pull request created",
            "failed": "PR creation failed",
            "running": "Creating PR",
        },
        "merge": {
            "success": "Pull request merged",
            "failed": "PR merge failed",
            "running": "Merging PR",
        },
        "branch": {
            "success": "Branch",
            "failed": "Branch failed",
            "running": "Creating branch",
        },
        "worktree": {
            "success": "Worktree",
            "failed": "Worktree failed",
            "running": "Updating worktree",
        },
    }
    return labels.get(kind, {}).get(status, str(event.get("title", "Activity")))


def render_milestone_card(
    event: dict[str, Any],
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
    show_source: bool = True,
) -> list[str]:
    when = display_time(event)
    icon, style = event_style(event)
    actor = actor_label(event, identities)
    label = milestone_label(event)
    heading = f"{actor} · {label}" if actor else label
    source = event_source_label(event) if show_source else ""
    source_prefix = f"[{source}] " if source else ""
    duration = format_duration(event, now_ms)
    detail = display_detail(event)
    summary_width = max(4, width - len(when) - 6)
    duration_suffix = f" · {duration}" if duration else ""
    content_width = max(4, summary_width - len(duration_suffix))
    core_width = max(1, content_width - len(source_prefix))
    if detail:
        minimum_detail = min(len(detail), max(1, core_width // 2))
        if len(heading) + 3 + minimum_detail > core_width:
            heading = label
        heading_budget = core_width - minimum_detail - 3
        if heading_budget >= 1:
            heading = crop(heading, heading_budget)
            detail = crop(detail, max(1, core_width - len(heading) - 3))
            core = f"{heading} · {detail}"
        else:
            core = crop(detail, core_width)
    else:
        core = crop(heading, core_width)
    summary = crop(source_prefix + core, content_width)
    summary = crop(summary + duration_suffix, summary_width)
    if color:
        summary = style_source_label(summary, event, color, ANSI["bold"])
        return [
            f"│ {ANSI['dim']}{when}{ANSI['reset']} "
            f"{style}{icon}{ANSI['reset']} {ANSI['bold']}{summary}{ANSI['reset']}"
        ]
    return [f"│ {when} {icon} {summary}"]


def render_pipeline_card(
    unit: dict[str, Any],
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
    show_source: bool = True,
) -> list[str]:
    events = unit["events"]
    ordered = sorted(events, key=event_epoch)
    when = display_time(max(ordered, key=event_epoch))
    actor = actor_label(ordered[-1], identities)
    heading = str(unit["title"])
    if actor:
        heading = f"{actor} · {heading}"
    heading = label_summary(ordered[-1], heading, show_source)
    duration = group_duration(events, now_ms)
    if duration:
        heading += f" · {duration}"
    pipeline = " → ".join(str(stage) for stage in unit["stages"])
    heading = crop(heading, max(4, width - len(when) - 6))
    pipeline = crop(pipeline, max(4, width - 6))
    if color:
        heading = style_source_label(
            heading, ordered[-1], color, ANSI["bold"] + ANSI["blue"]
        )
        return [
            f"│ {ANSI['dim']}{when}{ANSI['reset']} "
            f"{ANSI['bold']}{ANSI['blue']}┌ {heading}{ANSI['reset']}",
            f"│   {ANSI['bold']}{pipeline}{ANSI['reset']}",
        ]
    return [f"│ {when} ┌ {heading}", f"│   {pipeline}"]


def render_github_burst(
    unit: dict[str, Any], width: int, color: bool
) -> list[str]:
    events = unit["events"]
    latest = max(events, key=event_epoch)
    when = display_time(latest)
    numbers = github_burst_numbers(events)
    listing = " ".join(f"#{number}" for number in numbers)
    summary = f"PRs · {len(events)} confirmed"
    if listing:
        summary += f" · {listing}"
    summary = crop(summary, max(4, width - len(when) - 6))
    if not color:
        return [f"│ {when} ↗ {summary}"]
    return [
        f"│ {ANSI['dim']}{when}{ANSI['reset']} "
        f"{ANSI['blue']}↗{ANSI['reset']} {ANSI['dim']}{summary}{ANSI['reset']}"
    ]


def unit_color_index(unit: dict[str, Any]) -> int | None:
    events = unit["events"]
    return event_source_color_index(events[0]) if events else None


def apply_root_gutter(
    lines: list[str], color_index: int | None, color: bool
) -> list[str]:
    """Paint the left edge so a line keeps its root once the badge is dropped.

    The block takes over the two cells the line already spent on its border and
    the space after it, so a wider, brighter marker costs no width.
    """
    if not color or color_index is None:
        return lines
    tint = root_color(color_index)
    return [
        f"{tint}  {ANSI['reset']}{line[2:]}" if line.startswith("│ ") else line
        for line in lines
    ]


def unit_source_label(unit: dict[str, Any]) -> str:
    events = unit["events"]
    if unit["type"] == "github_burst" and len(events) > 1:
        # The collapsed sweep spans roots and shows no badge of its own.
        return ""
    return event_source_label(events[0]) if events else ""


def render_activity_unit(
    unit: dict[str, Any],
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
    show_source: bool = True,
    search: str = "",
) -> list[str]:
    events = unit["events"]
    if unit["type"] == "pipeline":
        return render_pipeline_card(
            unit, width, color, now_ms, identities, show_source
        )
    if unit["type"] == "filesystem_burst" and len(events) > 1:
        return render_filesystem_burst(unit, width, color, show_source)
    if unit["type"] == "github_burst" and len(events) > 1:
        return render_github_burst(unit, width, color)
    event = events[0]
    if event.get("kind") in MILESTONE_KINDS:
        return render_milestone_card(
            event, width, color, now_ms, identities, show_source
        )
    return [
        render_event_line(event, width, color, now_ms, identities, show_source, search)
    ]


def render_date_separator(day: date, today: date, width: int, color: bool) -> str:
    label = f"{day:%a %b} {day.day}"
    if day == today:
        label = f"Today · {label}"
    else:
        label = f"{label}, {day.year}"
    separator = f"├─ {label} "
    separator += "─" * max(0, width - len(separator))
    separator = crop(separator, width)
    if color:
        return f"{ANSI['bold']}{ANSI['blue']}{separator}{ANSI['reset']}"
    return separator


def event_search_text(event: dict[str, Any]) -> str:
    source = event_source_key(event)
    return " ".join(
        str(value)
        for value in (
            display_title(event),
            display_detail(event),
            event.get("agent"),
            event_source_label(event),
            # A single folder and each column carry no label, so take the name
            # from the folder path instead; the folder name is searchable text.
            Path(source).name if source else "",
        )
        if value
    )


def crop_to_match(text: str, width: int, search: str) -> str:
    """Crop a line so the searched text stays on screen.

    Cropping from the right can remove the only reason a line is showing, which
    makes a filtered pane look like it is lying. When the match sits past the
    edge, show the end of the text instead and mark the missing front.
    """
    if not search or terminal_cell_width(text) <= width:
        return crop(text, width)
    found = text.casefold().find(search.casefold())
    if found < 0 or terminal_cell_width(text[: found + len(search)]) <= width:
        return crop(text, width)
    end = found + len(search)
    start = max(0, end - max(1, width - 1))
    return crop(("…" if start else "") + text[start:end], width)


def event_matches_search(event: dict[str, Any], search: str) -> bool:
    return search.casefold() in event_search_text(event).casefold()


def render_timeline_activity(
    events: list[dict[str, Any]],
    line_budget: int,
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
    expanded_history: bool,
    event_filter: str,
    local_timezone: tzinfo | None = None,
    newest_first: bool = True,
    search: str = "",
) -> tuple[list[str], int]:
    if search:
        # Show the matching events themselves. Grouped into a burst or a task
        # card, a match hides behind "+4 more" and the line looks unrelated to
        # what was typed.
        events = [event for event in events if event_matches_search(event, search)]
        expanded_history = True
    units = build_activity_units(events, expanded_history, local_timezone)
    if event_filter == "milestones":
        units = [
            unit
            for unit in units
            if unit["type"] == "pipeline"
            or any(event.get("kind") in MILESTONE_KINDS for event in unit["events"])
        ]
    elif event_filter == "files":
        units = [
            unit
            for unit in units
            if unit["type"] == "filesystem_burst"
            or all(event.get("kind") in {"file", "config"} for event in unit["events"])
        ]
    # Always choose the newest units that fit in the viewport. The alternate
    # order reverses those complete rendered units so live activity stays
    # visible at the bottom without disturbing chronology inside a unit.
    units.sort(key=lambda unit: (int(unit["epoch"]), int(unit["index"])), reverse=True)
    candidates = units
    selected: list[tuple[date | None, dict[str, Any], list[str]]] = []
    remaining = max(1, line_budget)
    selected_units = 0
    selected_day: date | None = None
    today = local_date_for_epoch(now_ms, local_timezone)
    for unit in candidates:
        lines = render_activity_unit(
            unit, width, color, now_ms, identities, search=search
        )
        unit_day = activity_unit_local_date(unit, local_timezone)
        needs_separator = unit_day is not None and unit_day != selected_day
        separator_cost = int(needs_separator and today is not None)
        if len(lines) + separator_cost <= remaining:
            selected.append((unit_day, unit, lines))
            remaining -= len(lines) + separator_cost
            selected_units += 1
            selected_day = unit_day
        elif not selected:
            if separator_cost and remaining > 1:
                selected.append((unit_day, unit, lines[: remaining - 1]))
                selected_day = unit_day
            elif not separator_cost:
                selected.append((unit_day, unit, lines[:remaining]))
            else:
                continue
            selected_units += 1
            remaining = 0
        if remaining <= 0:
            break
    if not newest_first:
        selected.reverse()
    # The badge repeats the same answer on every line of a run. Drop it after
    # the first line and let the tinted left edge carry the root instead. With
    # no color there is no edge to read, so the badge stays on every line.
    previous_source = ""
    for index, (unit_day, unit, lines) in enumerate(selected):
        source = unit_source_label(unit)
        if color and index and source and source == previous_source:
            lines = render_activity_unit(
                unit, width, color, now_ms, identities, show_source=False, search=search
            )
        lines = apply_root_gutter(lines, unit_color_index(unit), color)
        selected[index] = (unit_day, unit, lines)
        previous_source = source
    hidden = max(0, len(candidates) - selected_units)
    rendered: list[str] = []
    displayed_day: date | None = None
    for unit_day, _unit, lines in selected:
        if unit_day is not None and unit_day != displayed_day and today is not None:
            rendered.append(render_date_separator(unit_day, today, width, color))
        rendered.extend(lines)
        displayed_day = unit_day
    return rendered, hidden


def render_github_banner(status: dict[str, Any], width: int, color: bool) -> str:
    number = status.get("number")
    prefix = f" PR #{number} " if number else " GitHub "
    text = crop(prefix + display_github_detail(status), width)
    if not color:
        return text
    failed = int(status.get("checks_failed") or 0) > 0
    conflicting = (
        status.get("mergeable") == "CONFLICTING" or status.get("merge_state") == "DIRTY"
    )
    pending = int(status.get("checks_pending") or 0) > 0
    if failed or conflicting or status.get("review") == "CHANGES_REQUESTED":
        style = ANSI["red"]
    elif status.get("coverage") == "PARTIAL" or pending:
        style = ANSI["yellow"]
    elif status.get("state") == "MERGED" or (
        status.get("state") == "OPEN" and status.get("merge_state") == "CLEAN"
    ):
        style = ANSI["green"]
    elif status.get("state") == "CLOSED":
        style = ANSI["yellow"]
    else:
        style = ANSI["blue"]
    return f"{ANSI['bold']}{style}{text}{ANSI['reset']}"


def render_git_banner(state: dict[str, str], width: int, color: bool) -> str:
    text = crop(
        f" Git {state['branch']} · ◆ {state['short_oid']}",
        width,
    )
    if not color:
        return text
    return f"{ANSI['dim']}{text}{ANSI['reset']}"


def active_agent_identities(
    identities: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    unique: dict[str, dict[str, str]] = {}
    for identity in identities.values():
        agent = normalize_agent(identity.get("agent"))
        if agent not in DISPLAY_CODING_AGENTS:
            continue
        # Pane-less agents share a label - two desktop sessions are both
        # "desktop" - so the session id is what keeps them apart.
        key = (
            identity.get("pane_id")
            or identity.get("session_id")
            or f"{agent}:{identity.get('label', '')}"
        )
        unique[key] = identity
    return sorted(
        unique.values(),
        key=lambda identity: (
            agent_label(identity.get("agent")),
            identity.get("pane_id", ""),
        ),
    )


def render_agent_banners(
    identities: dict[str, dict[str, str]], width: int, color: bool
) -> list[str]:
    lines: list[str] = []
    for identity in active_agent_identities(identities):
        agent = agent_label(identity.get("agent"))
        model = display_model(identity.get("model")) or "model ?"
        effort = identity.get("effort") or "effort ?"
        status = identity.get("status") or "unknown"
        text = crop(f" Agent {agent} · {model} · {effort} · {status}", width)
        if color:
            text = f"{ANSI['dim']}{text}{ANSI['reset']}"
        lines.append(text)
    return lines


def render_context_banners(
    identities: dict[str, dict[str, str]],
    git_status: dict[str, str] | None,
    width: int,
    color: bool,
) -> list[str]:
    agents = active_agent_identities(identities)
    lines: list[str] = []
    for index, identity in enumerate(agents):
        agent = agent_label(identity.get("agent"))
        source_label = identity.get(SOURCE_LABEL, "").strip()
        label = identity.get("label", "").strip()
        model = display_model(identity.get("model")) or "model ?"
        effort = identity.get("effort") or "effort ?"
        status = identity.get("status") or "unknown"
        context = (
            f" · {label}" if label and label.casefold() != agent.casefold() else ""
        )
        source = f"[{source_label}] " if source_label else ""
        text = f" {source}{agent}{context} · {model} · {effort} · {status}"
        if index == 0 and git_status:
            text += f"  │  {git_status['branch']} @ {git_status['short_oid']}"
        text = crop(text, width)
        if color:
            text = style_source_label(text, identity, color, ANSI["dim"])
            text = f"{ANSI['dim']}{text}{ANSI['reset']}"
        lines.append(text)
    if not agents and git_status:
        lines.append(render_git_banner(git_status, width, color))
    return lines


def watch_root_activity_state(state: "WatchRootState") -> str:
    identities = {
        key: identity
        for key, identity in state.identities.items()
        if not identity.get("root") or identity.get("root") == os.fspath(state.root)
    }
    statuses = {
        str(identity.get("status") or "unknown").casefold()
        for identity in active_agent_identities(identities)
    }
    if "working" in statuses:
        return "working"
    if statuses and statuses <= {"idle", "done"}:
        return "inactive"
    return "unknown"


ACTIVITY_LEVELS = "▁▂▃▄▅▆▇█"
ACTIVITY_WINDOW_MINUTES = 10


def activity_count(
    records: Iterable[dict[str, Any]],
    now_ms: int,
    minutes: int = ACTIVITY_WINDOW_MINUTES,
) -> int:
    """How many events a folder saw in the recent window."""
    window = minutes * 60_000
    return sum(1 for record in records if 0 <= now_ms - event_epoch(record) < window)


def activity_meter(count: int, busiest: int) -> str:
    """One cell that grows with activity: blank when quiet, full when busiest.

    Every folder is measured against the same busiest count, so two meters on
    one line can be compared with each other rather than only with themselves.
    """
    if count <= 0 or busiest <= 0:
        return " "
    steps = len(ACTIVITY_LEVELS)
    level = min(steps, max(1, -(-count * steps // busiest)))
    return ACTIVITY_LEVELS[level - 1]


def root_summary_priority(
    summary: str, activity_state: str, shown_labels: frozenset[str]
) -> int:
    """Rank a folder for the header.

    A folder with events on screen comes first, because its color block is
    visible and the header is the only place that names the color.
    """
    on_screen = root_summary_label(summary) in shown_labels
    working = activity_state == "working"
    if on_screen and working:
        return 3
    if on_screen:
        return 2
    if working:
        return 1
    return 0


def root_summary_line(summaries: tuple[str, ...], kept: list[int], total: int) -> str:
    text = " " + " · ".join(summaries[index] for index in kept)
    quiet = total - len(kept)
    if quiet:
        text += f"{' ·' if kept else ''} +{quiet} quiet"
    return text


def fit_root_summaries(
    summaries: tuple[str, ...],
    activity_states: tuple[str, ...],
    width: int,
    shown_labels: frozenset[str] = frozenset(),
) -> list[int]:
    """Choose the folders worth naming, keeping the order they are watched in.

    Naming three of eleven folders and stopping mid-word says less than naming
    the ones on screen and counting the rest.
    """
    total = len(summaries)

    def priority_of(index: int) -> int:
        return root_summary_priority(
            summaries[index],
            activity_states[index] if len(activity_states) == total else "unknown",
            shown_labels,
        )

    order = sorted(range(total), key=lambda index: (-priority_of(index), index))
    kept: list[int] = []
    for rank in sorted({priority_of(index) for index in order}, reverse=True):
        skipped = False
        for index in (i for i in order if priority_of(i) == rank):
            candidate = sorted([*kept, index])
            if len(root_summary_line(summaries, candidate, total)) <= width:
                kept = candidate
            else:
                skipped = True
        if skipped:
            # A busier folder did not fit, so a quieter one must not take its
            # place; the count of what is missing says the rest.
            break
    return kept


def render_root_summaries(
    summaries: tuple[str, ...],
    activity_states: tuple[str, ...],
    width: int,
    color: bool,
    color_indexes: tuple[int, ...] = (),
    shown_labels: frozenset[str] = frozenset(),
) -> str:
    total = len(summaries)
    kept = fit_root_summaries(summaries, activity_states, width, shown_labels)
    full_text = root_summary_line(summaries, kept, total)
    if len(activity_states) == total:
        activity_states = tuple(activity_states[index] for index in kept)
    if len(color_indexes) == total:
        color_indexes = tuple(color_indexes[index] for index in kept)
    summaries = tuple(summaries[index] for index in kept)
    visible_text = crop(full_text, width)
    if not color:
        return visible_text

    spans: list[tuple[int, int, str]] = []
    offset = 1
    for index, summary in enumerate(summaries):
        if index:
            offset += 3
        activity_state = (
            activity_states[index]
            if len(activity_states) == len(summaries)
            else "unknown"
        )
        spans.append((offset, offset + len(summary), activity_state))
        offset += len(summary)

    chunks: list[str] = []
    cursor = 0
    for index, (start, end, activity_state) in enumerate(spans):
        if start >= len(visible_text):
            break
        if cursor < start:
            chunks.append(visible_text[cursor:start])
        segment = visible_text[start : min(end, len(visible_text))]
        activity_style = {
            "working": ANSI["bold"],
            "inactive": ANSI["dim"],
        }.get(activity_state, "")
        if len(color_indexes) == len(summaries):
            name = root_summary_label(summaries[index])
            visible_name = segment[: len(name)]
            remainder = segment[len(visible_name) :]
            segment = style_root_name(
                visible_name,
                color_indexes[index],
                activity_state,
                activity_style,
            )
            segment += f"{remainder}{ANSI['reset']}"
        elif activity_style:
            segment = f"{activity_style}{segment}{ANSI['reset']}"
        chunks.append(segment)
        cursor = min(end, len(visible_text))
    if cursor < len(visible_text):
        chunks.append(visible_text[cursor:])
    return "".join(chunks)


def display_identities(
    records: list[dict[str, Any]], identities: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    combined = dict(identities)
    for event in records:
        session_id = str(event.get("session_id", ""))
        agent = normalize_agent(event.get("agent"))
        if not session_id or agent not in DISPLAY_CODING_AGENTS:
            continue
        identity = dict(identity_for_event(event, identities))
        identity["agent"] = agent
        for field in ("model", "effort"):
            value = event.get(field)
            if isinstance(value, str) and value:
                identity[field] = value
        for field in (SOURCE_LABEL, SOURCE_COLOR_INDEX):
            value = event.get(field)
            if isinstance(value, (str, int)) and str(value):
                identity[field] = str(value)
        combined[session_id] = identity
    return combined


def render_help(
    width: int, color: bool, newest_first: bool = True, root_count: int = 1
) -> list[str]:
    heading = "┌ Help"
    if color:
        heading = f"{ANSI['bold']}{ANSI['blue']}{heading}{ANSI['reset']}"
    order_note = (
        "Newest activity is at the top"
        if newest_first
        else "Newest activity is at the bottom"
    )
    entries = [
        "│ ?       toggle this help",
        "│ e       toggle compact / expanded detail",
        "│ f       cycle all / milestones / files",
        "│ p       pause / resume the display",
        "│ /       show only lines matching what you type; Esc clears it",
        "│ C       open the browser panel for these folders",
        "│ r       toggle newest-first / oldest-first order",
    ]
    if root_count > 1:
        entries.extend(
            (
                "│",
                "│ Folder colors: the block starting a line, that folder's",
                "│ badge, and its name in the header all share one color.",
                "│",
                "│ Views (default: auto)",
                "│ All     wide pane: a column per folder; narrow: one list",
                "│ Focus   one folder fills the pane",
                "│ a       show all folders again",
                "│ Tab     move to the next folder",
                f"│ 1-{min(root_count, 9)}     jump to a folder by position",
                "│ --layout auto|columns|timeline selects the startup layout",
            )
        )
    entries.extend(
        (
            "│ Esc     close this help",
            "│ R       reload Side Dog with the same folders and flags",
        "│ q       quit Side Dog (Ctrl-C also works)",
            "│",
            f"│ {order_note}; runs of file writes fold into one line.",
            "│ A task card links one agent turn: edits, tests, commits, pushes.",
            "│ Outcomes: ✓ worked · × failed · … running · ? could not tell.",
            "│ Only the folders you watch are shown; every event is saved to disk.",
            "│ PR/CI text: blue open · yellow pending · green clean/merged · red failed.",
            f"│ Side Dog: {PROJECT_URL}",
            "└ Press ? or Esc to return",
        )
    )
    return [heading, *(crop(entry, width) for entry in entries)]


def render_display_notice(message: str, width: int, color: bool) -> list[str]:
    """Render a temporary, non-modal explanation above the timeline."""

    width = max(8, width)
    title = "┌ View changed "
    top = title + "─" * max(0, width - terminal_cell_width(title))
    content = crop(message, max(1, width - 4))
    body = f"│ {content}"
    body += " " * max(0, width - terminal_cell_width(body) - 1) + "│"
    bottom = "└" + "─" * max(0, width - 1)
    if not color:
        return [top, body, bottom]
    return [
        f"{ANSI['bold']}{top}{ANSI['reset']}",
        f"{ANSI['dim']}│{ANSI['reset']} {content}"
        + " " * max(0, width - terminal_cell_width(f"│ {content}") - 1)
        + f"{ANSI['dim']}│{ANSI['reset']}",
        f"{ANSI['dim']}{bottom}{ANSI['reset']}",
    ]


def focus_header(
    project_name: str, focus_label: str, available_width: int
) -> tuple[str, str]:
    """Keep the active focus readable before spending space on context."""
    prefix = " SIDE DOG  "
    focus_prefix = "FOCUS: "
    value_width = max(
        1,
        available_width
        - terminal_cell_width(prefix)
        - terminal_cell_width(focus_prefix),
    )
    visible_value = crop(focus_label, value_width)
    focus = f"{focus_prefix}{visible_value}"
    header = f"{prefix}{focus}"
    remaining = available_width - terminal_cell_width(header)
    if remaining >= 4:
        header += crop(f" · {project_name} ", remaining)
    return header, focus


def style_focus_header(header: str, focus: str) -> str:
    before, marker, after = header.partition(focus)
    return (
        f"{ANSI['bold']}{ANSI['blue']}{before}{ANSI['inverse']}{marker}"
        f"{ANSI['reset']}{ANSI['bold']}{ANSI['blue']}{after}"
    )


def render(
    records: list[dict[str, Any]],
    root: Path,
    width: int,
    height: int,
    color: bool,
    identities: dict[str, dict[str, str]] | None = None,
    session_filter: str | None = None,
    github_status: dict[str, Any] | None = None,
    git_status: dict[str, str] | None = None,
    show_help: bool = False,
    expanded_history: bool = False,
    event_filter: str = "all",
    paused: bool = False,
    new_event_count: int = 0,
    newest_first: bool = True,
    root_count: int = 1,
    focused_root_label: str | None = None,
    root_summaries: tuple[str, ...] = (),
    root_activity_states: tuple[str, ...] = (),
    root_summary_color_indexes: tuple[int, ...] = (),
    display_notice: str | None = None,
    search: str = "",
    worker_count: int = 0,
    repository_context: str | None = None,
    discovered: bool = False,
) -> str:
    identities = identities or {}
    width = max(28, min(width, 160))
    shown_identities = display_identities(records, identities)
    banner_identities = (
        identities if active_agent_identities(identities) else shown_identities
    )
    agents = len(active_agent_identities(banner_identities))
    project_name = (
        (repository_context or "several folders")
        if root_count > 1
        else git_status.get("repository", root.name)
        if git_status
        else root.name
    )
    clock = time.strftime("%H:%M:%S")
    available_width = max(1, width - terminal_cell_width(clock) - 1)
    if root_count > 1:
        header, focus = focus_header(
            project_name, focused_root_label or "ALL", available_width
        )
    else:
        focus = ""
        header = crop(f" SIDE DOG  {project_name} ", available_width)
    line = (
        "─"
        * max(
            0,
            width
            - terminal_cell_width(header)
            - terminal_cell_width(clock)
            - 1,
        )
        + f" {clock}"
    )
    if color:
        styled_header = (
            style_focus_header(header, focus)
            if focus
            else f"{ANSI['bold']}{ANSI['blue']}{header}"
        )
        output = [f"{styled_header}{line}{ANSI['reset']}"]
    else:
        output = [header + line]
    if root_count > 1:
        # "found" marks folders discovery chose; folders you named go unmarked.
        counted = f"{root_count} found folders" if discovered else f"{root_count} folders"
        scope = (
            f"{focused_root_label} · 1 of {counted}"
            if focused_root_label
            else counted
        )
        noun = "agent" if agents == 1 else "agents"
        watching = crop(
            f" Watching {scope} · {agents} {noun}{worker_notice(worker_count)}", width
        )
    else:
        gone = " · folder is gone" if root_is_missing(root) else ""
        count = activity_count(records, int(time.time() * 1000))
        meter = activity_meter(count, count)
        watching = crop(f" Watching {display_root(root)}{gone} {meter}", width)
    output.append(f"{ANSI['dim']}{watching}{ANSI['reset']}" if color else watching)
    if root_summaries:
        output.append(
            render_root_summaries(
                root_summaries,
                root_activity_states,
                width,
                color,
                root_summary_color_indexes,
                frozenset(
                    label
                    for record in records
                    if (label := event_source_label(record))
                ),
            )
        )
    elif github_status:
        output.append(render_github_banner(github_status, width, color))
    context_banners = render_context_banners(
        banner_identities, git_status if root_count == 1 else None, width, color
    )
    output.extend(context_banners)
    if display_notice and not show_help:
        output.extend(render_display_notice(display_notice, width, color))
    if show_help:
        output.extend(render_help(width, color, newest_first, root_count))
        footer = crop(" ? / Esc close help · q quit ", width)
        output.append(f"{ANSI['dim']}{footer}{ANSI['reset']}" if color else footer)
        return "\n".join(output[:height])
    available = max(1, height - len(output) - 2)
    coalesced = coalesce_operations(records)
    timeline: list[dict[str, Any]] = []
    for event in coalesced:
        identity = identity_for_event(event, identities)
        if not matches_session_filter(event, identity, session_filter):
            continue
        timeline.append(event)

    if not timeline:
        message = crop("waiting for coding-agent activity…", width - 2)
        output.append(f"  {message}")
    else:
        now_ms = int(time.time() * 1000)
        timeline_lines, hidden = render_timeline_activity(
            timeline,
            available,
            width,
            color,
            now_ms,
            shown_identities,
            expanded_history,
            event_filter,
            newest_first=newest_first,
            search=search,
        )
        detail_label = "expanded" if expanded_history else "compact"
        order_label = "newest first" if newest_first else "oldest first"
        timeline_header = f"┌ {order_label} · {detail_label} · {event_filter}"
        if search:
            timeline_header += f" · /{search}"
        if hidden:
            hidden_direction = "below" if newest_first else "above"
            timeline_header += f" · {hidden} {hidden_direction}"
        if paused:
            timeline_header += f" · PAUSED · {new_event_count} new"
        timeline_header = crop(timeline_header, width)
        if color:
            timeline_header = (
                f"{ANSI['bold']}{ANSI['blue']}{timeline_header}{ANSI['reset']}"
            )
        output.append(timeline_header)
        output.extend(timeline_lines)
    pause_action = "resume" if paused else "pause"
    detail_action = "compact" if expanded_history else "expand"
    order_action = "oldest" if newest_first else "newest"
    root_actions = (
        f" a all · Tab folder · 1-{min(root_count, 9)} jump ·" if root_count > 1 else ""
    )
    footer = crop(
        f"{root_actions} r {order_action} · e {detail_action} · f {event_filter} · p {pause_action} · / find · C web · R reload · ? help · q quit ",
        width,
    )
    output.append((f"{ANSI['dim']}{footer}{ANSI['reset']}" if color else footer))
    return "\n".join(output[:height])


@dataclass
class WatchRootState:
    root: Path
    path: Path
    records: deque[dict[str, Any]]
    position: int
    known_files: dict[str, tuple[int, int]]
    git_status: dict[str, str] | None
    last_hook_writes: dict[str, float]
    identities: dict[str, dict[str, str]]
    github_status: dict[str, Any] | None
    last_github_fingerprint: str | None
    last_scan: float
    last_git_refresh: float
    last_herdr_refresh: float
    last_github_refresh: float
    native_streams: dict[str, NativeAgentStream] = field(default_factory=dict)
    present: bool = True
    baselined: bool = False
    scan_seconds: float = 0.0
    workers: list[str] = field(default_factory=list)


def root_column_widths(width: int, root_count: int) -> list[int]:
    if root_count < 2:
        return []
    column_width, remainder = divmod(width, root_count)
    if column_width < COLUMN_MIN_WIDTH:
        return []
    return [
        column_width + (1 if index < remainder else 0) for index in range(root_count)
    ]


def folders_worth_a_column(states: list["WatchRootState"]) -> list[int]:
    """Folders with something to show, so an empty one does not take the width.

    A worktree is adopted the moment it appears, before the agent has written
    anything, which is right for collecting and wrong for the layout: half the
    pane goes to a column that says "waiting". It earns its column once it has
    activity or an agent of its own.
    """
    return [
        index
        for index, state in enumerate(states)
        if state.records
        # Herdr reports an agent for every worktree of the repository it is in,
        # so a column has to be earned by an agent actually sitting in it.
        or any(
            identity_belongs_to_root(identity, state.root)
            for identity in active_agent_identities(state.identities)
        )
    ]


def should_render_root_columns(
    layout: str,
    width: int,
    root_count: int,
    focused_root_index: int | None,
    show_help: bool,
) -> bool:
    return bool(
        layout in {"auto", "columns"}
        and focused_root_index is None
        and not show_help
        and root_column_widths(width, root_count)
    )


def identity_belongs_to_root(identity: dict[str, str], root: Path) -> bool:
    raw_working_root = identity.get("working_root")
    if not raw_working_root:
        return False
    try:
        working_root = canonical_root(raw_working_root)
        canonical = canonical_root(root)
        return working_root == canonical or working_root.is_relative_to(canonical)
    except (OSError, ValueError):
        return False


def watch_root_column_identities(
    states: list[WatchRootState],
) -> list[dict[str, dict[str, str]]]:
    assignments: list[dict[str, dict[str, str]]] = [dict() for _ in states]
    collected: dict[str, tuple[dict[str, str], set[int], set[str]]] = {}
    for state_index, state in enumerate(states):
        for key, identity in state.identities.items():
            identity_key = (
                identity.get("pane_id")
                or identity.get("session_id")
                or ":".join(
                    (
                        identity.get("agent", ""),
                        identity.get("working_root", ""),
                        identity.get("label", ""),
                    )
                )
            )
            current = collected.get(identity_key)
            if current is None:
                collected[identity_key] = (identity, {state_index}, {key})
            else:
                current[1].add(state_index)
                current[2].add(key)
    for identity, appearances, keys in collected.values():
        exact = [
            index
            for index, state in enumerate(states)
            if identity_belongs_to_root(identity, state.root)
        ]
        target = (
            max(exact, key=lambda index: len(states[index].root.parts))
            if exact
            else min(appearances)
        )
        for key in keys:
            assignments[target][key] = identity
    return assignments


def root_column_title(
    state: WatchRootState,
    label: str,
    records: Iterable[dict[str, Any]] | None = None,
    busiest: int = 0,
) -> str:
    summary = watch_root_summary(
        state, label, int(time.time() * 1000), records, busiest
    )
    if label != state.root.name:
        summary = summary.replace(label, f"{label} · {state.root.name}", 1)
    return summary


def render_root_column(
    state: WatchRootState,
    label: str,
    records: list[dict[str, Any]],
    identities: dict[str, dict[str, str]],
    color_index: int,
    width: int,
    height: int,
    color: bool,
    *,
    session_filter: str | None,
    expanded_history: bool,
    event_filter: str,
    paused: bool,
    new_event_count: int,
    newest_first: bool,
    search: str = "",
    busiest: int = 0,
) -> list[str]:
    identities = {
        key: {
            **identity,
            SOURCE_LABEL: label,
            SOURCE_COLOR_INDEX: str(color_index),
        }
        for key, identity in identities.items()
    }
    title = crop(f"┌ {root_column_title(state, label, records, busiest)} ", width)
    title += "─" * max(0, width - terminal_cell_width(title))
    if color:
        prefix = f"{ANSI['bold']}{ANSI['blue']}┌ "
        name_start = 2
        visible_name = title[name_start : name_start + len(label)]
        remainder = title[name_start + len(visible_name) :]
        title = (
            f"{prefix}{style_root_name(visible_name, color_index)}"
            f"{ANSI['bold']}{ANSI['blue']}{remainder}{ANSI['reset']}"
        )
    output = [title]
    shown_identities = display_identities(records, identities)
    banner_identities = (
        identities if active_agent_identities(identities) else shown_identities
    )
    agent_lines = render_context_banners(
        banner_identities, None, max(1, width - 2), color
    )
    if agent_lines:
        output.extend(f"│ {line.strip()}" for line in agent_lines)
    else:
        output.append("│ no active agent")

    coalesced = coalesce_operations(records)
    timeline = [
        event
        for event in coalesced
        if matches_session_filter(
            event, identity_for_event(event, identities), session_filter
        )
    ]
    detail_label = "expanded" if expanded_history else "compact"
    order_label = "newest" if newest_first else "oldest"
    timeline_header = f"├ {order_label} · {detail_label} · {event_filter}"
    if search:
        timeline_header += f" · /{search}"
    if paused:
        timeline_header += f" · PAUSED · {new_event_count} new"
    available = max(1, height - len(output) - 2)
    timeline_lines: list[str] = []
    hidden = 0
    if timeline:
        timeline_lines, hidden = render_timeline_activity(
            timeline,
            available,
            width,
            color,
            int(time.time() * 1000),
            shown_identities,
            expanded_history,
            event_filter,
            newest_first=newest_first,
            search=search,
        )
    if hidden:
        direction = "below" if newest_first else "above"
        timeline_header += f" · {hidden} {direction}"
    timeline_header = crop(timeline_header, width)
    if color:
        timeline_header = (
            f"{ANSI['bold']}{ANSI['blue']}{timeline_header}{ANSI['reset']}"
        )
    output.append(timeline_header)
    if timeline_lines:
        output.extend(timeline_lines)
    else:
        output.append(crop("│ waiting for coding-agent activity…", width))
    while len(output) < max(1, height - 1):
        output.append("│")
    bottom = "└" + "─" * max(0, width - 1)
    output.append(f"{ANSI['dim']}{bottom}{ANSI['reset']}" if color else bottom)
    return output[:height]


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def pad_visible(text: str, width: int) -> str:
    visible_width = terminal_cell_width(ANSI_ESCAPE.sub("", text))
    return text + " " * max(0, width - visible_width)


def shared_column_edge(text: str) -> str:
    position = 0
    while match := ANSI_ESCAPE.match(text, position):
        position = match.end()
    replacements = {"┌": "┬", "├": "┼", "└": "┴"}
    if position < len(text) and text[position] in replacements:
        return text[:position] + replacements[text[position]] + text[position + 1 :]
    return text


def render_root_columns(
    states: list[WatchRootState],
    labels: list[str],
    paused_records: dict[str, list[dict[str, Any]]] | None,
    width: int,
    height: int,
    color: bool,
    *,
    session_filter: str | None,
    expanded_history: bool,
    event_filter: str,
    paused: bool,
    new_event_counts: dict[str, int] | None,
    newest_first: bool,
    display_notice: str | None = None,
    search: str = "",
    discovered: bool = False,
) -> str:
    shown = folders_worth_a_column(states)
    if len(shown) < 2:
        shown = list(range(len(states)))
    widths = root_column_widths(width, len(shown))
    if not widths:
        raise ValueError("watched folders do not fit in columns")
    scale_now = int(time.time() * 1000)
    busiest = max(
        (activity_count(state.records, scale_now) for state in states), default=0
    )
    column_states = [states[index] for index in shown]
    column_labels = [labels[index] for index in shown]
    column_identities = watch_root_column_identities(column_states)
    column_records = [
        aggregate_watch_records([state], [label], paused_records, None)
        for state, label in zip(column_states, column_labels, strict=True)
    ]
    agent_count = sum(
        len(
            active_agent_identities(
                identities
                if active_agent_identities(identities)
                else display_identities(records, identities)
            )
        )
        for records, identities in zip(column_records, column_identities, strict=True)
    )
    noun = "agent" if agent_count == 1 else "agents"
    clock = time.strftime("%H:%M:%S")
    heading, focus = focus_header(
        f"{watch_repository_context(states)} · columns",
        "ALL",
        max(1, width - terminal_cell_width(clock) - 1),
    )
    heading += (
        "─"
        * max(
            0,
            width
            - terminal_cell_width(heading)
            - terminal_cell_width(clock)
            - 1,
        )
        + f" {clock}"
    )
    styled_heading = f"{style_focus_header(heading, focus)}{ANSI['reset']}"
    output = [
        styled_heading if color else heading,
        crop(
            f" Watching {len(states)}"
            f"{' found' if discovered else ''} folders · {agent_count} {noun}"
            f"{worker_notice(len({name for s in states for name in s.workers}))}",
            width,
        ),
    ]
    if display_notice:
        output.extend(render_display_notice(display_notice, width, color))
    column_height = max(4, height - len(output) - 1)
    blocks: list[list[str]] = []
    for position, (state, label, records, identities, column_width) in enumerate(
        zip(
            column_states,
            column_labels,
            column_records,
            column_identities,
            widths,
            strict=True,
        )
    ):
        # The color follows the folder, not its place in the row, so a folder
        # keeps the same color whether or not a quieter one is shown beside it.
        color_index = root_color_index(shown[position])
        for record in records:
            record[SOURCE_COLOR_INDEX] = color_index
        blocks.append(
            render_root_column(
                state,
                label,
                records,
                identities,
                color_index,
                column_width,
                column_height,
                color,
                session_filter=session_filter,
                expanded_history=expanded_history,
                event_filter=event_filter,
                paused=paused,
                new_event_count=(new_event_counts or {}).get(os.fspath(state.root), 0),
                newest_first=newest_first,
                search=search,
                busiest=busiest,
            )
        )
    for row in range(column_height):
        output.append(
            "".join(
                pad_visible(
                    block[row] if index == 0 else shared_column_edge(block[row]),
                    column_width,
                )
                for index, (block, column_width) in enumerate(
                    zip(blocks, widths, strict=True)
                )
            )
        )
    pause_action = "resume" if paused else "pause"
    detail_action = "compact" if expanded_history else "expand"
    order_action = "oldest" if newest_first else "newest"
    footer = crop(
        f" a all · Tab folder · 1-{min(len(states), 9)} jump · r {order_action} · e {detail_action} · f {event_filter} · p {pause_action} · / find · C web · R reload · ? help · q quit ",
        width,
    )
    output.append(f"{ANSI['dim']}{footer}{ANSI['reset']}" if color else footer)
    return "\n".join(output[:height])


def root_is_missing(root: Path) -> bool:
    try:
        return not root.is_dir()
    except OSError:
        return True


def canonical_watch_roots(
    projects: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
) -> list[Path]:
    values = [projects] if isinstance(projects, (str, os.PathLike)) else list(projects)
    if not values:
        values = ["."]
    roots: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        root = canonical_root(value)
        # A folder that is gone still has its recorded activity, so watching it
        # reads that back. A path Side Dog has never seen is a typo.
        if root_is_missing(root) and not events_path(root).exists():
            raise SystemExit(f"no folder and no saved activity at: {root}")
        if root in seen:
            raise SystemExit(f"that folder is listed twice: {root}")
        roots.append(root)
        seen.add(root)
    return roots


def initial_watch_roots(
    projects: str | os.PathLike[str] | Iterable[str | os.PathLike[str]],
    *,
    follow_herdr: bool,
    require_herdr: bool = False,
) -> tuple[list[Path], set[Path], str | None]:
    """Resolve pinned folders plus live folders from the inherited Herdr session."""
    values = [projects] if isinstance(projects, (str, os.PathLike)) else list(projects)
    explicit = canonical_watch_roots(values) if values else []
    requested = set(explicit)
    roots = list(explicit)
    error: str | None = None
    if follow_herdr:
        live, error = herdr_session_roots()
        if error and require_herdr:
            raise SystemExit(f"could not follow the Herdr session: {error}")
        for root in live:
            if root not in roots and len(roots) < WATCH_ROOT_LIMIT:
                roots.append(root)
    if not roots:
        roots = canonical_watch_roots(["."])
    return roots, requested, error


def reconcile_herdr_roots(
    watched: Iterable[Path],
    live: Iterable[Path],
    requested: set[Path],
    limit: int = WATCH_ROOT_LIMIT,
) -> tuple[list[Path], list[Path]]:
    """Make room for newly live Herdr folders without evicting explicit folders."""
    current = list(watched)
    live_order = list(dict.fromkeys(live))
    pinned = [root for root in current if root in requested]
    dynamic_limit = max(0, limit - len(pinned))
    preferred_live = [
        root for root in live_order if root not in requested
    ][:dynamic_limit]
    missing = [root for root in preferred_live if root not in current]
    preferred = set(preferred_live)
    live_set = set(live_order)
    candidates = [
        root for root in current if root not in requested and root not in preferred
    ]
    candidates.sort(
        key=lambda root: (
            0 if root not in live_set else 1,
            0 if folder_is_finished(root) else 1,
            last_event_epoch(root),
            os.fspath(root),
        )
    )
    # A session with no agents starts on the shell's current folder so the UI
    # has something useful to render. Replace that temporary root as soon as a
    # real session root appears; explicit current-folder arguments stay pinned.
    current_folder = canonical_root(".")
    retired = (
        [current_folder]
        if live_order
        and not requested
        and current == [current_folder]
        and current_folder in candidates
        else []
    )
    needed = max(0, len(current) - len(retired) + len(missing) - limit)
    retired.extend(
        [root for root in candidates if root not in retired][:needed]
    )
    room = max(0, limit - (len(current) - len(retired)))
    additions = missing[:room]
    return retired, additions


def initialize_watch_root(root: Path, github_poll: float) -> WatchRootState:
    path = events_path(root)
    records: deque[dict[str, Any]] = deque(latest_events(path), maxlen=500)
    github_status: dict[str, Any] | None = None
    last_github_fingerprint: str | None = None
    for record in reversed(records):
        if record.get("kind") != "github" or not isinstance(record.get("github"), dict):
            continue
        github_status = dict(record["github"])
        last_github_fingerprint = str(
            record.get("github_fingerprint") or github_fingerprint(github_status)
        )
        break
    return WatchRootState(
        root=root,
        path=path,
        records=records,
        position=path.stat().st_size if path.exists() else 0,
        # Asking git what differs is quick enough to do here - the walk this
        # replaced was not - so a write made while Side Dog was starting is
        # reported rather than quietly adopted by the first sweep.
        known_files=snapshot(root),
        baselined=not root_is_missing(root),
        present=not root_is_missing(root),
        git_status=load_git_state(root),
        last_hook_writes={},
        identities={},
        github_status=github_status,
        last_github_fingerprint=last_github_fingerprint,
        last_scan=0.0,
        last_git_refresh=-10.0,
        last_herdr_refresh=-10.0,
        last_github_refresh=-max(1.0, github_poll),
    )


def last_event_epoch(root: Path) -> int:
    """When Side Dog last recorded anything for a folder, or 0."""
    try:
        with events_path(root).open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 8192))
            tail = handle.read().splitlines()
    except OSError:
        return 0
    for raw_line in reversed(tail):
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict) and (epoch := event_epoch(record)):
            return int(epoch)
    return 0


def folder_is_finished(root: Path) -> bool:
    """Whether this folder's pull request is already merged or closed.

    A worktree whose branch has landed is done, however recently it was busy.
    The answer comes from the activity Side Dog already recorded, so no folder
    needs a fresh GitHub call to be judged finished.
    """
    try:
        with events_path(root).open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 65536))
            tail = handle.read().splitlines()
    except OSError:
        return False
    for raw_line in reversed(tail):
        if b'"github"' not in raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        status = record.get("github") if isinstance(record, dict) else None
        if not isinstance(status, dict) or not status.get("state"):
            continue
        # A worktree reused for a new branch is not finished because the branch
        # it used to hold was merged.
        branch = str(status.get("branch") or "")
        git_state = load_git_state(root)
        current = (git_state or {}).get("branch", "")
        if branch and current and branch != current:
            return False
        return str(status["state"]).upper() in {"MERGED", "CLOSED"}
    return False


def head_commit_epoch(root: Path) -> int:
    """When the folder's checked-out branch last got a commit, or 0."""
    try:
        completed = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if completed.returncode != 0:
        return 0
    try:
        return int(completed.stdout.strip()) * 1000
    except ValueError:
        return 0


CODEX_SUBAGENT_WINDOW_SECONDS = 300
CODEX_SESSION_HEADERS: dict[str, dict[str, Any]] = {}


def codex_session_header(path: Path) -> dict[str, Any]:
    """The first record of a Codex session file, read once and remembered.

    An unreadable first line is not remembered: a session creates its file a
    moment before it writes the header, and caching that empty read would hide
    the agent for as long as the watcher runs.
    """
    key = os.fspath(path)
    cached = CODEX_SESSION_HEADERS.get(key)
    if cached is not None:
        return cached
    try:
        with path.open("rb") as handle:
            record = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    payload = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(payload, dict) or not payload:
        return {}
    CODEX_SESSION_HEADERS[key] = payload
    return payload


def codex_sessions_root() -> Path:
    """Where Codex keeps its session files, honouring CODEX_HOME."""
    configured = os.environ.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return home / "sessions"


CODEX_LISTING_TTL_SECONDS = 2.0
CODEX_LISTING_CACHE: dict[str, tuple[float, list[tuple[Path, float]]]] = {}


def codex_session_listing() -> list[tuple[Path, float]]:
    """Every session file with its modification time, walked at most once a tick.

    Identities and worker names both need this, and every watched folder asks
    for both on its own refresh. Walking a year of Codex once per question meant
    sixteen recursive passes over thousands of files every two seconds; the
    answer barely changes in that time, so it is shared.
    """
    root = os.fspath(codex_sessions_root())
    cached = CODEX_LISTING_CACHE.get(root)
    now = time.monotonic()
    if cached is not None and now - cached[0] < CODEX_LISTING_TTL_SECONDS:
        return cached[1]
    listing: list[tuple[Path, float]] = []
    try:
        candidates = list(codex_sessions_root().rglob("*.jsonl"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            listing.append((path, path.stat().st_mtime))
        except OSError:
            continue
    CODEX_LISTING_CACHE[root] = (now, listing)
    return listing


def codex_recent_sessions(deadline: float) -> list[tuple[Path, float]]:
    """Session files written since deadline, oldest first, with their mtimes.

    A year of Codex holds thousands of files and gigabytes of transcript, so the
    modification time decides who is worth opening before anything is opened.
    """
    recent = [item for item in codex_session_listing() if item[1] >= deadline]
    recent.sort(key=lambda item: item[1])
    return recent


def codex_workers(root: Path, now: float | None = None) -> list[str]:
    """Names of the worker subagents a Codex session has running in this repo.

    One Codex session can spawn several workers, each writing in a different
    worktree. Herdr reports the session, not its workers, so a pane can say
    "1 agent" while four names are busy. Their session files say who they are.
    """
    deadline = (now if now is not None else time.time()) - CODEX_SUBAGENT_WINDOW_SECONDS
    common = git_common_dir(os.fspath(root))
    names: list[str] = []
    for path, _ in codex_recent_sessions(deadline):
        header = codex_session_header(path)
        if header.get("thread_source") != "subagent":
            continue
        cwd = header.get("cwd")
        if not isinstance(cwd, str):
            continue
        try:
            if not common or git_common_dir(cwd) != common:
                continue
        except OSError:
            continue
        name = header.get("agent_nickname") or header.get("agent_role") or "worker"
        names.append(str(name))
    return sorted(set(names))


# A Codex agent with no pane still writes a session file every turn, so the last
# write is the only evidence of it. Within a minute it is working; past that it
# is waiting for a person, the same two words Herdr reports for a pane.
CODEX_SESSION_WORKING_SECONDS = 60
# How far back to go looking for agents nobody's pane vouches for. A conversation
# that has written nothing for a quarter of an hour is finished rather than idle,
# and without a cutoff every session file ever written arrives as an agent.
CODEX_SESSION_IDENTITY_WINDOW_SECONDS = 900
# Threads Codex spawns for itself. codex_workers() already counts these by name,
# so an identity for one would show the same agent twice.
CODEX_HELPER_THREAD_SOURCES = {"subagent", "guardian_review"}


def load_codex_session_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Codex agents working in this folder that no terminal pane knows about.

    Codex Desktop writes the same session files the CLI does, so the desktop app
    is visible here even though Herdr has no pane to report it. The identities
    are shaped like Herdr's and keyed by session id, so the two merge.
    """
    moment = now if now is not None else time.time()
    watched_common = git_common_dir(os.fspath(root))
    identities: dict[str, dict[str, str]] = {}
    for path, changed in codex_recent_sessions(
        moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = codex_session_header(path)
        if header.get("thread_source") in CODEX_HELPER_THREAD_SOURCES:
            continue
        cwd = header.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        # A helper thread's header carries its parent's session_id, so the
        # thread's own id is the one that names this file and that Herdr reports.
        session_id = header.get("id") or header.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            session_root = canonical_root(cwd)
            session_common, session_worktree = git_repository_location(
                os.fspath(session_root)
            )
            associated = (
                canonical_root(session_worktree) if session_worktree else session_root
            )
        except OSError:
            continue
        # By repository, not by path: Codex Desktop keeps its own worktrees under
        # ~/.codex/worktrees, nowhere near the folder being watched.
        same_repository = bool(watched_common) and session_common == watched_common
        if associated != root and not same_repository:
            continue
        originator = str(header.get("originator") or "").strip()
        # There is no terminal title to borrow, so say where the agent came from
        # and where it is working rather than inventing a task name. The folder
        # above comes along unless the session is in the watched folder itself:
        # every Codex Desktop worktree of one repository ends in the same folder
        # name, and three agents sharing a label would show as one.
        parts = [part for part in session_root.parts[-2:] if part != os.sep]
        where = session_root.name if session_root == root else "/".join(parts)
        label = " · ".join(part for part in (originator, where) if part)
        identities[session_id] = {
            "agent": "codex",
            "root": os.fspath(associated),
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "working_root": os.fspath(session_root),
            "status": (
                "working"
                if changed >= moment - CODEX_SESSION_WORKING_SECONDS
                else "idle"
            ),
            "label": label or agent_label("codex"),
            "session_id": session_id,
            **load_codex_metadata(session_id),
        }
    return identities


PI_SESSION_HEADERS: dict[str, dict[str, Any]] = {}
PI_LISTING_TTL_SECONDS = 2.0
PI_LISTING_CACHE: dict[str, tuple[float, list[tuple[Path, float]]]] = {}


def pi_sessions_root() -> Path:
    """Where Pi keeps its session files, honouring PI_HOME."""
    configured = os.environ.get("PI_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".pi"
    return home / "agent" / "sessions"


def pi_session_header(path: Path) -> dict[str, Any]:
    """The first record of a Pi session file, read once and remembered.

    Pi opens a session with a `session` record naming its id and cwd, so that
    one line says who the agent is and where it is working. An unreadable first
    line is not cached: the file lands a moment before its header, and caching
    the empty read would hide the agent for the life of the watcher.
    """
    key = os.fspath(path)
    cached = PI_SESSION_HEADERS.get(key)
    if cached is not None:
        return cached
    try:
        with path.open("rb") as handle:
            record = json.loads(handle.readline())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(record, dict) or record.get("type") != "session":
        return {}
    PI_SESSION_HEADERS[key] = record
    return record


def pi_session_listing() -> list[tuple[Path, float]]:
    """Every Pi session file with its modification time, walked once a tick."""
    root = os.fspath(pi_sessions_root())
    cached = PI_LISTING_CACHE.get(root)
    now = time.monotonic()
    if cached is not None and now - cached[0] < PI_LISTING_TTL_SECONDS:
        return cached[1]
    listing: list[tuple[Path, float]] = []
    try:
        candidates = list(pi_sessions_root().rglob("*.jsonl"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            listing.append((path, path.stat().st_mtime))
        except OSError:
            continue
    PI_LISTING_CACHE[root] = (now, listing)
    return listing


def pi_recent_sessions(deadline: float) -> list[tuple[Path, float]]:
    """Pi session files written since deadline, oldest first, with their mtimes."""
    recent = [item for item in pi_session_listing() if item[1] >= deadline]
    recent.sort(key=lambda item: item[1])
    return recent


def load_pi_session_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Pi agents working in this folder that no terminal pane knows about.

    Pi writes one session file per run under ~/.pi/agent/sessions, so a Pi
    session in a terminal, an editor or a desktop surface is visible here even
    when Herdr has no pane to report it. The identities are shaped like Herdr's
    and keyed by session id, so the two merge.
    """
    moment = now if now is not None else time.time()
    watched_common = git_common_dir(os.fspath(root))
    identities: dict[str, dict[str, str]] = {}
    for path, changed in pi_recent_sessions(
        moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = pi_session_header(path)
        cwd = header.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            continue
        session_id = header.get("id") or header.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            session_root = canonical_root(cwd)
            session_common, session_worktree = git_repository_location(
                os.fspath(session_root)
            )
            associated = (
                canonical_root(session_worktree) if session_worktree else session_root
            )
        except OSError:
            continue
        same_repository = bool(watched_common) and session_common == watched_common
        if associated != root and not same_repository:
            continue
        parts = [part for part in session_root.parts[-2:] if part != os.sep]
        where = session_root.name if session_root == root else "/".join(parts)
        label = " · ".join(part for part in ("Pi", where) if part)
        identities[session_id] = {
            "agent": "pi",
            "root": os.fspath(associated),
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "working_root": os.fspath(session_root),
            "status": (
                "working"
                if changed >= moment - CODEX_SESSION_WORKING_SECONDS
                else "idle"
            ),
            "label": label or agent_label("pi"),
            "session_id": session_id,
            **load_pi_metadata(session_id),
        }
    return identities


def load_agent_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Everyone working in this folder, from every source that knows.

    Herdr sees terminal panes. Claude registers every live session whatever
    surface launched it, desktop app included. Codex and Pi each leave a session
    file per run. Herdr wins where two sources describe one session: it alone knows the
    pane, tab and window, and a session file does not. Keying on the session id
    keeps one agent to a row.
    """
    identities = load_herdr_identities(root)
    known = {
        identity["session_id"]
        for identity in identities.values()
        if identity.get("session_id")
    }
    for source in (
        claude_identities(root),
        load_codex_session_identities(root, now),
        load_pi_session_identities(root, now),
    ):
        for session_id, identity in source.items():
            if session_id in known:
                continue
            known.add(session_id)
            identities[session_id] = identity
    return identities


def worktree_root_for(path: str) -> Path | None:
    """The worktree an agent's working folder belongs to, or the folder itself."""
    try:
        folder = canonical_root(path)
        reported = git_worktree_root(os.fspath(folder))
        return canonical_root(reported) if reported else folder
    except OSError:
        return None


def agent_working_folders(now: float | None = None) -> dict[Path, bool]:
    """Every folder a coding agent is working in right now, anywhere on this
    machine, mapped to whether that agent is working this minute.

    load_agent_identities() answers a different question - who is working in
    one folder Side Dog is already watching - so it filters everything by that
    folder's repository and cannot be asked what to watch in the first place.
    This puts the same three sources the same questions and keeps the folder
    rather than the identity.
    """
    moment = now if now is not None else time.time()
    folders: dict[Path, bool] = {}

    def remember(raw: Any, working: bool) -> None:
        if not isinstance(raw, str) or not raw:
            return
        folder = worktree_root_for(raw)
        if folder is None:
            return
        # Any source saying the agent is working wins: two agents can share a
        # worktree, and one of them typing makes the folder worth a column.
        folders[folder] = folders.get(folder, False) or working

    for agent in herdr_agents(herdr_snapshot()):
        remember(
            agent.get("foreground_cwd") or agent.get("cwd"),
            agent.get("agent_status") == "working",
        )
    for record in claude_session_registry():
        session_id = str(record.get("sessionId") or "")
        remember(
            record.get("cwd"),
            claude_session_status(session_id, moment) == "working",
        )
    for path, changed in codex_recent_sessions(
        moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = codex_session_header(path)
        if header.get("thread_source") in CODEX_HELPER_THREAD_SOURCES:
            continue
        remember(
            header.get("cwd"),
            changed >= moment - CODEX_SESSION_WORKING_SECONDS,
        )
    for path, changed in pi_recent_sessions(
        moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = pi_session_header(path)
        remember(
            header.get("cwd"),
            changed >= moment - CODEX_SESSION_WORKING_SECONDS,
        )
    return folders


def discovered_watch_roots(
    configuration: dict[str, Any] | None = None,
    limit: int | None = None,
    now: float | None = None,
) -> list[Path]:
    """The folders to watch when nobody named any: wherever agents are working.

    Ten repositories and dozens of worktrees is more than anyone wants to type,
    and all three identity sources already know the folder their agent sits in.

    Pinned folders lead, because a pin is meant to survive the cap. Folders with
    an agent working this minute come next, so when there are more folders than
    the pane holds it is the quiet ones that miss out; alphabetical order inside
    each group keeps two runs a second apart from shuffling the colors.

    A pinned folder is kept even if an ignore pattern covers it: naming one
    folder is a more specific instruction than a glob over many.
    """
    configuration = load_config() if configuration is None else configuration
    limit = config_limit(configuration, WATCH_ROOT_LIMIT) if limit is None else limit
    ignore = config_ignores(configuration)
    roots = pinned_folders(configuration)
    seen = set(roots)
    found = agent_working_folders(now)
    for folder in sorted(found, key=lambda item: (not found[item], os.fspath(item))):
        if folder in seen or path_is_ignored(folder, ignore):
            continue
        if root_is_missing(folder):
            continue
        seen.add(folder)
        roots.append(folder)
    return roots[:limit]


def rediscovered_roots(
    states: list["WatchRootState"],
    configuration: dict[str, Any],
    limit: int,
    requested: set[Path],
) -> tuple[list[Path], list[Path]]:
    """What discovery would retire and add now, ranked and fitted to the cap.

    A bare `side-dog watch` answers "wherever agents are working", and that
    answer changes. An agent starting in a repository Side Dog has never seen
    would otherwise stay invisible until the next restart, because the
    worktree scan only looks inside repositories already on screen. The
    Herdr reconciliation already knows how to seat newcomers - retiring the
    quietest adopted folder when every seat is taken - so discovery hands it
    its own ranking rather than repeating the arithmetic.
    """
    return reconcile_herdr_roots(
        (state.root for state in states),
        discovered_watch_roots(configuration, limit),
        requested,
        limit,
    )


def herdr_workspace_folders(
    workspace: dict[str, Any], snapshot: dict[str, Any]
) -> list[Path]:
    """The folders the agents in one Herdr workspace are working in."""
    workspace_id = str(workspace.get("workspace_id") or "")
    folders: list[Path] = []
    seen: set[Path] = set()
    for agent in herdr_agents(snapshot):
        if str(agent.get("workspace_id") or "") != workspace_id:
            continue
        raw = agent.get("foreground_cwd") or agent.get("cwd")
        if not isinstance(raw, str) or not raw:
            continue
        folder = worktree_root_for(raw)
        if folder is None or folder in seen:
            continue
        seen.add(folder)
        folders.append(folder)
    return folders


def space_names(workspaces: list[dict[str, Any]]) -> list[str]:
    """Every name @something could mean, saved sets and Herdr spaces together."""
    names = {str(workspace["label"]) for workspace in workspaces}
    names.update(load_spaces())
    return sorted(names)


def unknown_space_message(
    name: str, matches: int, workspaces: list[dict[str, Any]]
) -> str:
    known = space_names(workspaces)
    listed = ", ".join(known) if known else "there are none"
    if matches > 1:
        return (
            f"{matches} spaces are called {name!r}, so Side Dog cannot tell "
            f"which one you mean. Name their folders instead, or save the set "
            f"you want with --save. Names in use: {listed}"
        )
    return f"no space called {name!r}. Names that do exist: {listed}"


def space_folders(name: str) -> list[Path]:
    """The folders behind @name: a folder set you saved, or a Herdr space.

    Herdr already labels each workspace after the repository in it, so the
    names are the ones a person would use anyway. A name saved with --save wins
    over a Herdr label spelled the same, because saving something and then
    finding it ignored would be the more surprising of the two.
    """
    saved = find_space(name, load_spaces())
    if saved is not None:
        folders: list[Path] = []
        for value in saved:
            try:
                folders.append(canonical_root(value))
            except OSError:
                continue
        return folders
    snapshot = herdr_snapshot()
    workspaces = herdr_workspaces(snapshot)
    folded = name.casefold()
    matches = [
        workspace
        for workspace in workspaces
        if str(workspace["label"]).casefold() == folded
    ]
    if len(matches) != 1:
        raise SystemExit(unknown_space_message(name, len(matches), workspaces))
    folders = herdr_workspace_folders(matches[0], snapshot)
    if not folders:
        raise SystemExit(f"no agent is working in {name!r} right now")
    return folders


def resolve_watch_arguments(values: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Turn any @name argument into the folders it stands for.

    Folders a space contributes are deduplicated here, because two agents often
    share one worktree and two spaces often share one repository. Folders typed
    out by hand are left alone, so naming the same one twice is still the typo
    it always was.
    """
    resolved: list[str] = []
    from_spaces: set[Path] = set()
    for value in values:
        text = os.fspath(value)
        if not text.startswith("@") or len(text) == 1:
            resolved.append(text)
            continue
        for folder in space_folders(text[1:]):
            if folder in from_spaces:
                continue
            from_spaces.add(folder)
            resolved.append(os.fspath(folder))
    return resolved


def save_named_space(name: str, roots: list[Path]) -> str:
    """Write the folders being watched under a name, and say what happened.

    Worktrees Side Dog adopted for itself are left out: they come and go with
    the work, and preserving today's accident is not what --save is for.
    """
    written = save_space(name.lstrip("@"), [os.fspath(root) for root in roots])
    if written is None:
        return f"Could not save @{name}; check {display_root(spaces_path())}"
    folders = "folder" if len(roots) == 1 else "folders"
    return f"Saved {len(roots)} {folders} as @{name} in {display_root(written)}"


def agent_folders(root: Path) -> set[Path]:
    """Folders of this repository a coding agent is sitting in right now.

    Herdr only, deliberately. This answers "should Side Dog start watching that
    worktree", and the file-derived sources would answer yes for every desktop
    worktree of the repository, each of which then costs a scan and a stream.
    A folder those agents work in still joins as soon as it has activity.
    """
    folders: set[Path] = set()
    for identity in load_herdr_identities(root).values():
        for key in ("working_root", "root"):
            value = identity.get(key)
            if not value:
                continue
            try:
                folders.add(canonical_root(value))
            except OSError:
                continue
    return folders


def pinned_folders(
    document: dict[str, Any] | None = None,
    existing: Iterable[Path] = (),
) -> list[Path]:
    """Folders the configuration file wants watched however quiet they are.

    A pin that points nowhere is dropped rather than fatal. A configuration
    file is meant to travel between machines, and a folder that only exists on
    one of them should not stop the pane starting on the others. A pin whose
    folder is gone but whose recorded activity Side Dog still holds is kept,
    the same as a folder named on the command line.
    """
    configuration = load_config() if document is None else document
    pinned: list[Path] = []
    seen = set(existing)
    for value in config_pins(configuration):
        try:
            root = canonical_root(value)
        except OSError:
            continue
        if root in seen:
            continue
        if root_is_missing(root) and not events_path(root).exists():
            continue
        seen.add(root)
        pinned.append(root)
    return pinned


def busy_worktrees(
    watched: list[Path],
    now_ms: int,
    limit: int,
    live: set[Path] | None = None,
    ignore: Iterable[str] | None = None,
) -> list[Path]:
    """Worktrees worth a column: an agent is in one, or it moved recently.

    A repository collects worktrees faster than a pane collects columns, so a
    quiet one stays out until something happens in it. Busiest first, because
    the cap decides who misses out.
    """
    watched_set = set(watched)
    # One listing per watched folder gives every checkout and its branch, and
    # one more question gives every branch's commit time. Asking per worktree
    # cost 37 ms each, which is seconds on a repository with 154 of them.
    branches: dict[Path, str] = {}
    heads: dict[Path, str] = {}
    candidates: set[Path] = set()
    # Commit times belong to the repository they came from. Two unrelated
    # repositories both have a refs/heads/main, and one map for all of them
    # would rank a worktree here by a commit made over there.
    committed_at: dict[Path, dict[str, int]] = {}
    for folder in watched:
        detached: list[str] = []
        listed: list[Path] = []
        for path, branch, head in git_worktree_entries(folder):
            candidates.add(path)
            listed.append(path)
            if branch:
                branches[path] = branch
            elif head:
                heads[path] = head
                detached.append(head)
        times = branch_commit_times(folder)
        times.update(commit_times(folder, detached))
        for path in listed:
            committed_at[path] = times
    candidates -= watched_set
    # A worktree arriving on its own is subject to the file's ignores, the
    # same as everywhere else it could arrive from.
    patterns = config_ignores(load_config()) if ignore is None else list(ignore)
    if patterns:
        candidates = {
            path for path in candidates if not path_is_ignored(path, patterns)
        }
    if not candidates:
        return []
    if live is None:
        # Every watched repository, not the first: an agent sitting in an old
        # worktree of the third repository is just as alive.
        live = set()
        for folder in watched:
            live |= agent_folders(folder)
    ranked: list[tuple[int, str]] = []
    for path in candidates:
        if path in live:
            ranked.append((now_ms, os.fspath(path)))
            continue
        if folder_is_finished(path):
            continue
        reference = branches.get(path) or heads.get(path, "")
        times = committed_at.get(path, {})
        recent = max(last_event_epoch(path), times.get(reference, 0))
        if now_ms - recent <= FOLDER_ACTIVE_WINDOW_MS:
            ranked.append((recent, os.fspath(path)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    room = max(0, limit - len(watched))
    return [Path(path) for _, path in ranked[:room]]


def discovered_worktrees(
    roots: Iterable[Path], ignore: Iterable[str] | None = None
) -> set[Path]:
    """Every worktree of these folders, minus the ones the file ignores.

    Ignoring here rather than at each caller covers every way a worktree can
    arrive on its own: the start-up scan, the busiest-first cap, and a worktree
    created while Side Dog is running. A folder named on the command line
    arrives by a different road and is never ignored.
    """
    patterns = config_ignores(load_config()) if ignore is None else list(ignore)
    found: set[Path] = set()
    for root in roots:
        found.update(git_worktree_paths(root))
    if not patterns:
        return found
    return {path for path in found if not path_is_ignored(path, patterns)}


def keep_one_root(retired: list[Path], watched_count: int) -> list[Path]:
    """Never retire the last folder: an empty pane shows nothing and has
    nowhere to grow back from, so the quietest folder keeps its seat."""
    if len(retired) >= watched_count:
        return retired[: max(0, watched_count - 1)]
    return retired


def retired_worktrees(
    states: list[WatchRootState],
    requested: set[Path],
    live: set[Path],
    pinned: set[Path] | None = None,
) -> list[Path]:
    """Folders Side Dog adopted that are finished, so the pane can have the room.

    A folder named on the command line is never retired, however quiet it gets,
    and neither is one the configuration file pins: pinning a folder is exactly
    the statement that it should stay on screen when nothing is happening in it.
    """
    kept = set(pinned_folders()) if pinned is None else pinned
    return [
        state.root
        for state in states
        if state.root not in requested
        and state.root not in live
        and state.root not in kept
        and folder_is_finished(state.root)
    ]


def follow_new_worktrees(
    states: list[WatchRootState],
    known: set[Path],
    now_ms: int,
    limit: int | None = None,
    live: set[Path] | None = None,
    ignore: Iterable[str] | None = None,
) -> tuple[list[Path], set[Path]]:
    """Report worktrees to start watching, with the refreshed baseline.

    Two ways in. A worktree created since Side Dog started joins straight
    away, because an agent branching into one is about to work there. A
    worktree that was already sitting there joins once something happens in
    it, so a repository full of finished branches does not eat the pane.
    """
    configuration = load_config()
    limit = config_limit(configuration, WATCH_ROOT_LIMIT) if limit is None else limit
    patterns = config_ignores(configuration) if ignore is None else list(ignore)
    watched = [state.root for state in states]
    watched_set = set(watched)
    current = discovered_worktrees(watched_set, patterns)
    created = sorted(current - known - watched_set)
    woken = [
        path
        for path in busy_worktrees(watched, now_ms, limit, live, patterns)
        if path not in created
    ]
    room = max(0, limit - len(states))
    return (created + woken)[:room], current | known | watched_set


def watch_repository_context(states: list["WatchRootState"]) -> str:
    """Where the watched folders live, for the header.

    "several folders" said how many; it never said where. Worktrees of one
    repository name that repository, and a mix names the first plus how many
    more, so `FOCUS: ALL` always says what it is all *of*.
    """
    homes: list[str] = []
    for state in states:
        status = state.git_status
        home = state.root
        if status and status.get("common_dir"):
            common = Path(status["common_dir"])
            if common.name == ".git":
                home = common.parent
            elif status.get("worktree_root"):
                home = Path(status["worktree_root"])
        label = display_root(home)
        if label not in homes:
            homes.append(label)
    if not homes:
        return "several folders"
    if len(homes) == 1:
        return homes[0]
    return f"{homes[0]} +{len(homes) - 1}"


def watch_root_labels(states: list[WatchRootState]) -> list[str]:
    candidates: list[str] = []
    for state in states:
        number = (
            state.github_status.get("number")
            if isinstance(state.github_status, dict)
            else None
        )
        if isinstance(number, int):
            candidates.append(f"PR #{number}")
        elif state.git_status and state.git_status.get("branch"):
            candidates.append(state.git_status["branch"])
        else:
            candidates.append(state.root.name)
    counts = Counter(candidates)
    labels: list[str] = []
    used: set[str] = set()
    for state, candidate in zip(states, candidates, strict=True):
        base = candidate if counts[candidate] == 1 else state.root.name
        label = base
        suffix = 2
        while label in used:
            label = f"{base}:{suffix}"
            suffix += 1
        used.add(label)
        labels.append(label)
    return labels


def watch_root_summary(
    state: WatchRootState,
    label: str,
    now_ms: int | None = None,
    records: Iterable[dict[str, Any]] | None = None,
    busiest: int = 0,
) -> str:
    summary = label
    if state.git_status and state.git_status.get("short_oid"):
        summary += f" @ {state.git_status['short_oid']}"
    if isinstance(state.github_status, dict):
        number = state.github_status.get("number")
        if isinstance(number, int) and not label.startswith("PR #"):
            summary += f" · PR #{number}"
        lifecycle = str(state.github_status.get("state") or "").upper()
        merge_state = display_merge_state(state.github_status)
        if lifecycle:
            summary += f" {lifecycle}"
        if merge_state and merge_state != lifecycle:
            summary += f" {merge_state}"
    if not state.present:
        summary += " · gone"
    shown = state.records if records is None else records
    if now_ms is not None:
        count = activity_count(shown, now_ms)
        summary += f" {activity_meter(count, busiest if busiest else count)}"
    return summary


def selected_watch_indexes(count: int, focused_index: int | None) -> list[int]:
    if focused_index is None:
        return list(range(count))
    return [focused_index] if 0 <= focused_index < count else list(range(count))


def aggregate_watch_records(
    states: list[WatchRootState],
    labels: list[str],
    paused_records: dict[str, list[dict[str, Any]]] | None,
    focused_index: int | None,
) -> list[dict[str, Any]]:
    tagged: list[tuple[int, int, int, dict[str, Any]]] = []
    show_source = len(states) > 1
    for root_index in selected_watch_indexes(len(states), focused_index):
        state = states[root_index]
        source_key = os.fspath(state.root)
        source_records = (
            paused_records.get(source_key, [])
            if paused_records is not None
            else list(state.records)
        )
        for append_index, original in enumerate(source_records):
            record = dict(original)
            record[SOURCE_KEY] = source_key
            if show_source:
                record[SOURCE_LABEL] = labels[root_index]
                record[SOURCE_COLOR_INDEX] = root_color_index(root_index)
            tagged.append((event_epoch(record), root_index, append_index, record))
    tagged.sort(key=lambda item: item[:3])
    return [record for _, _, _, record in tagged]


def aggregate_watch_identities(
    states: list[WatchRootState],
    focused_index: int | None,
    labels: list[str] | None = None,
) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    column_identities = watch_root_column_identities(states)
    for root_index in selected_watch_indexes(len(states), focused_index):
        state = states[root_index]
        source_key = os.fspath(state.root)
        source_label = labels[root_index] if labels else state.root.name
        for key, identity in column_identities[root_index].items():
            tagged = {
                **identity,
                SOURCE_LABEL: source_label,
                SOURCE_COLOR_INDEX: str(root_color_index(root_index)),
            }
            identities.setdefault(key, tagged)
            identities[f"{source_key}:{key}"] = tagged
    return identities


def root_focus_for_key(key: bytes, current: int | None, root_count: int) -> int | None:
    if root_count <= 1:
        return current
    if key == b"a":
        return None
    if key == b"\t":
        return 0 if current is None else (current + 1) % root_count
    if len(key) == 1 and key.isdigit() and key != b"0":
        index = int(key) - 1
        return index if index < min(root_count, 9) else current
    return current


@dataclass(frozen=True)
class WatchRootExternalRefresh:
    identities: dict[str, dict[str, str]] | None
    github_result: tuple[dict[str, Any] | None, str | None] | None
    github_branch: str | None = None
    workers: list[str] | None = None


def load_watch_root_external_refresh(
    root: Path,
    refresh_herdr: bool,
    refresh_github: bool,
    github_branch: str | None = None,
) -> WatchRootExternalRefresh:
    return WatchRootExternalRefresh(
        identities=load_agent_identities(root) if refresh_herdr else None,
        github_result=load_github_pr(root) if refresh_github else None,
        github_branch=github_branch,
        workers=codex_workers(root) if refresh_herdr else None,
    )


def apply_watch_root_external_refresh(
    state: WatchRootState, refresh: WatchRootExternalRefresh
) -> None:
    if refresh.identities is not None:
        state.identities = refresh.identities
    if refresh.workers is not None:
        state.workers = refresh.workers
    if refresh.github_result is None:
        return
    current_branch = state.git_status.get("branch") if state.git_status else None
    if refresh.github_branch != current_branch:
        return
    verified, github_error = refresh.github_result
    if verified is not None:
        verified = carry_forward_merge_state(verified, state.github_status)
        fingerprint = github_fingerprint(verified)
        if fingerprint != state.last_github_fingerprint:
            append_event(
                state.root,
                github_event(
                    verified,
                    state.github_status,
                    latest_delivery_context(state.records),
                ),
            )
            state.last_github_fingerprint = fingerprint
        state.github_status = verified
    elif is_definitive_no_pr(github_error):
        state.github_status = None
        state.last_github_fingerprint = None
    elif state.github_status is not None:
        state.github_status = {
            **state.github_status,
            "coverage": "PARTIAL",
            "error": github_error,
        }
    elif any(record.get("kind") in {"pr", "merge"} for record in state.records):
        state.github_status = {
            "state": "UNKNOWN",
            "ci": "CI ?",
            "coverage": "PARTIAL",
            "error": github_error,
        }


def schedule_watch_root_refreshes(
    states: list[WatchRootState],
    now: float,
    github_poll: float,
    executor: ThreadPoolExecutor,
    pending: dict[str, Future[WatchRootExternalRefresh]],
) -> None:
    for state in states:
        key = os.fspath(state.root)
        if key in pending:
            continue
        refresh_herdr = now - state.last_herdr_refresh >= 2.0
        refresh_github = (
            github_poll > 0 and now - state.last_github_refresh >= github_poll
        )
        if not refresh_herdr and not refresh_github:
            continue
        if refresh_herdr:
            state.last_herdr_refresh = now
        if refresh_github:
            state.last_github_refresh = now
        pending[key] = executor.submit(
            load_watch_root_external_refresh,
            state.root,
            refresh_herdr,
            refresh_github,
            state.git_status.get("branch") if state.git_status else None,
        )


def apply_completed_watch_root_refreshes(
    states: list[WatchRootState],
    pending: dict[str, Future[WatchRootExternalRefresh]],
) -> None:
    states_by_root = {os.fspath(state.root): state for state in states}
    for key, future in list(pending.items()):
        if not future.done():
            continue
        del pending[key]
        try:
            refresh = future.result()
        except Exception:
            continue
        state = states_by_root.get(key)
        if state is not None:
            apply_watch_root_external_refresh(state, refresh)


def wait_for_watch_root_refreshes(
    states: list[WatchRootState],
    pending: dict[str, Future[WatchRootExternalRefresh]],
) -> None:
    if pending:
        wait(tuple(pending.values()))
    apply_completed_watch_root_refreshes(states, pending)


FOLDER_SCAN_COST_MULTIPLE = 10
FOLDER_SCAN_MAX_SECONDS = 30.0


def folder_scan_interval(state: "WatchRootState", poll: float) -> float:
    """How long to leave a folder alone between filesystem sweeps.

    Walking ten thousand files takes most of a second, and doing that for eight
    folders every tick leaves no time to draw. A folder is revisited in
    proportion to what it costs, so small folders stay near-live and a big one
    backs off. This is the fallback path; an agent's own stream reports its
    writes immediately either way.
    """
    return min(
        FOLDER_SCAN_MAX_SECONDS,
        max(0.5, poll, state.scan_seconds * FOLDER_SCAN_COST_MULTIPLE),
    )


def folder_due_for_scan(
    states: list["WatchRootState"], now: float, poll: float
) -> "WatchRootState | None":
    """The one folder that sweeps the filesystem this pass, if any is due.

    One sweep per pass, or eight big folders spend seconds walking between
    frames. The folder picked is the one whose next sweep fell due first, and
    only if it is due at all. Picking the folder scanned longest ago instead
    hands the turn to a big folder that is not ready to sweep, and the small
    folder behind it waits out the big one's interval - up to thirty seconds
    of edits nobody mentions.
    """
    ready = [
        state
        for state in states
        if now - state.last_scan >= folder_scan_interval(state, poll)
    ]
    return min(
        ready,
        key=lambda state: state.last_scan + folder_scan_interval(state, poll),
        default=None,
    )


def poll_watch_root(
    state: WatchRootState,
    now: float,
    poll: float,
    github_poll: float,
    *,
    poll_external: bool = True,
    scan_files: bool = True,
) -> int:
    poll_native_agent_events(state.root, state.identities, state.native_streams)
    new_records, state.position = read_new_events(state.path, state.position)
    for record in new_records:
        state.records.append(record)
        if record.get("kind") in {"file", "config"}:
            state.last_hook_writes[str(record.get("detail", ""))] = now
        if record.get("kind") in {"pr", "merge"}:
            state.last_github_refresh = -max(1.0, github_poll)
    if scan_files and now - state.last_scan >= folder_scan_interval(state, poll):
        started = time.monotonic()
        present = not root_is_missing(state.root)
        current = snapshot(state.root)
        state.scan_seconds = time.monotonic() - started
        if not state.baselined or (present and not state.present):
            # A folder that was not there at start-up, and a folder that comes
            # back, are both adopted as they are found: neither is new work.
            state.known_files = current
            state.baselined = True
        state.present = present
        # A file put back exactly the way the last commit had it drops off
        # git's list without being deleted, and so does a file that was just
        # committed. Measure the ones that left the list: still there and
        # different means somebody wrote it, still there and unchanged means it
        # was committed, and not there at all means it is gone.
        changed_now = {
            path: value
            for path, value in current.items()
            if value != DELETED_FILE and state.known_files.get(path) != value
        }
        vanished: set[str] = set()
        for path in set(state.known_files) - set(current):
            # A deletion that has since been committed drops off git's list.
            # It was announced when the hole appeared; do not say it twice.
            if state.known_files[path] == DELETED_FILE:
                continue
            try:
                stat = (state.root / path).lstat()
            except OSError:
                vanished.add(path)
                continue
            value = (stat.st_mtime_ns, stat.st_size)
            if value != state.known_files[path]:
                changed_now[path] = value
        vanished.update(
            path
            for path, value in current.items()
            if value == DELETED_FILE and state.known_files.get(path) != DELETED_FILE
        )
        for changed in sorted(changed_now):
            if now - state.last_hook_writes.get(changed, -100.0) < 2.0:
                continue
            counts = git_line_changes(state.root, changed)
            append_event(
                state.root,
                {
                    "agent": "filesystem",
                    "kind": "config" if is_config(changed) else "file",
                    "status": "success",
                    "title": "Config changed" if is_config(changed) else "File changed",
                    "detail": changed,
                    **(
                        {"lines_added": counts[0], "lines_removed": counts[1]}
                        if counts
                        else {}
                    ),
                },
            )
        for removed in sorted(vanished):
            # A file Side Dog cannot read is not a file it can announce.
            if (state.root / removed).exists():
                continue
            append_event(
                state.root,
                {
                    "agent": "filesystem",
                    "kind": "config" if is_config(removed) else "file",
                    "status": "success",
                    "title": "Config removed" if is_config(removed) else "File removed",
                    "detail": removed,
                },
            )
        state.known_files = current
        state.last_scan = now
    if now - state.last_git_refresh >= 1.0:
        current_git_status = load_git_state(state.root)
        if current_git_status is not None and state.git_status is not None:
            branch_changed = current_git_status["branch"] != state.git_status["branch"]
            oid_changed = current_git_status["oid"] != state.git_status["oid"]
            if branch_changed:
                state.github_status = None
                state.last_github_fingerprint = None
                state.last_github_refresh = -max(1.0, github_poll)
                append_event(
                    state.root,
                    {
                        "agent": "git",
                        "kind": "branch",
                        "status": "success",
                        "title": "Branch switched",
                        "detail": current_git_status["branch"],
                        "git_oid": current_git_status["oid"],
                    },
                )
            elif oid_changed and not any(
                record.get("kind") == "commit"
                and record.get("git_oid") == current_git_status["oid"]
                for record in state.records
            ):
                append_event(
                    state.root,
                    {
                        "agent": "git",
                        "kind": "commit",
                        "status": "success",
                        "title": "Commit created",
                        "detail": git_commit_detail(state.root, current_git_status),
                        "git_oid": current_git_status["oid"],
                    },
                )
        if current_git_status is not None:
            state.git_status = current_git_status
        state.last_git_refresh = now
    refresh_herdr = poll_external and now - state.last_herdr_refresh >= 2.0
    refresh_github = (
        poll_external
        and github_poll > 0
        and now - state.last_github_refresh >= github_poll
    )
    if refresh_herdr or refresh_github:
        if refresh_herdr:
            state.last_herdr_refresh = now
        if refresh_github:
            state.last_github_refresh = now
        apply_watch_root_external_refresh(
            state,
            load_watch_root_external_refresh(
                state.root,
                refresh_herdr,
                refresh_github,
                state.git_status.get("branch") if state.git_status else None,
            ),
        )
    return len(new_records)


def watch(
    projects: str | Iterable[str],
    *,
    width: int,
    poll: float,
    no_color: bool,
    layout: str = "auto",
    session_filter: str | None = None,
    github_poll: float = 15.0,
    once: bool = False,
    follow_worktrees: bool = True,
    save_space_as: str | None = None,
    follow_herdr: bool = False,
    require_herdr: bool = False,
) -> int:
    configuration = load_config()
    limit = config_limit(configuration, WATCH_ROOT_LIMIT)
    ignore = config_ignores(configuration)
    named = resolve_watch_arguments(
        [projects] if isinstance(projects, (str, os.PathLike)) else list(projects)
    )
    discovering = False
    if named or follow_herdr:
        # Inside a Herdr session, the session says where to look, which is a
        # more specific answer than every agent on the machine.
        roots, requested, herdr_error = initial_watch_roots(
            named, follow_herdr=follow_herdr, require_herdr=require_herdr
        )
        if follow_herdr and herdr_error:
            print(
                f"side-dog: {herdr_error}; watching available folders and retrying",
                file=sys.stderr,
            )
    else:
        # Nobody said where to look, so ask every agent on the machine where it
        # is working. A discovered folder is not "requested": when its pull
        # request lands it should leave again, the way an adopted worktree does.
        discovering = True
        roots = discovered_watch_roots(configuration, limit)
        requested = set()
        if not roots:
            # Never useless: with no agent anywhere, watch where you are stood.
            # Not "requested", though - the seat is borrowed, and rediscovery
            # hands it to the first real agent folder that appears.
            roots = canonical_watch_roots(["."])
    # Pinned folders join whatever was asked for, so a folder you always want
    # on screen is written down once instead of typed out every run.
    configured_pins = pinned_folders(configuration)
    pinned = set(configured_pins)
    # Against what is already watched, not against what was named: discovery
    # names nothing, and it has already put the pinned folders on the list.
    already = set(roots)
    roots = roots + [root for root in configured_pins if root not in already]
    # Saved before the worktrees Side Dog adopts for itself join in, so the
    # name means the folders you chose rather than what was busy at the time.
    space_notice = save_named_space(save_space_as, roots) if save_space_as else ""
    if follow_worktrees:
        roots = roots + busy_worktrees(
            roots, int(time.time() * 1000), limit, ignore=ignore
        )
    states = [initialize_watch_root(root, github_poll) for root in roots]
    known_worktrees = (
        discovered_worktrees(roots, ignore) | set(roots) if follow_worktrees else set()
    )
    last_worktree_scan = 0.0
    running = True
    show_help = False
    saved = load_display_settings()
    migrate_display_settings(saved)
    # The file is where preferences start; the e, f and r keys still write to
    # display.json, so what was pressed last wins over what was written down.
    remembered = {**config_display(configuration), **saved}
    expanded_history = bool(remembered.get("expanded_history", False))
    newest_first = bool(remembered.get("newest_first", True))
    remembered_filter = str(remembered.get("event_filter", FILTER_ORDER[0]))
    event_filter_index = (
        FILTER_ORDER.index(remembered_filter)
        if remembered_filter in FILTER_ORDER
        else 0
    )
    search = ""
    searching = False
    pending_search = b""
    reloading = False
    focused_root_index: int | None = None
    paused_records: dict[str, list[dict[str, Any]]] | None = None
    paused_new_count = 0
    paused_new_counts: dict[str, int] = {}
    display_notice = DisplayNotice()
    if space_notice:
        display_notice.show(space_notice, time.monotonic())
    web_panel = WebPanel()
    input_descriptor: int | None = None
    terminal_state: list[Any] | None = None
    refresh_executor = (
        ThreadPoolExecutor(max_workers=min(32, len(states)))
        if len(states) > 1
        else None
    )
    pending_refreshes: dict[str, Future[WatchRootExternalRefresh]] = {}

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    color = not no_color and sys.stdout.isatty()
    interactive = color and not once
    if interactive:
        if sys.stdin.isatty():
            try:
                input_descriptor = sys.stdin.fileno()
                terminal_state = termios.tcgetattr(input_descriptor)
                tty.setcbreak(input_descriptor)
            except (OSError, termios.error):
                input_descriptor = None
                terminal_state = None
        sys.stdout.write("\x1b[?25l\x1b[?1049h")
        sys.stdout.flush()
    try:
        while running:
            if input_descriptor is not None:
                while select.select([input_descriptor], [], [], 0)[0]:
                    key = os.read(input_descriptor, 1)
                    if searching:
                        if key in {b"\r", b"\n"}:
                            searching = False
                            display_notice.show(search_notice(search), time.monotonic())
                        elif key == b"\x1b":
                            searching = False
                            search = ""
                            display_notice.show(search_notice(search), time.monotonic())
                        elif key in {b"\x7f", b"\b"}:
                            search, pending_search = search[:-1], b""
                        else:
                            search, pending_search = append_search_byte(
                                search, pending_search, key
                            )
                        continue
                    if key == b"/" and not show_help:
                        searching = True
                        search = ""
                    elif key == b"?":
                        show_help = not show_help
                    elif key == b"\x1b" and show_help:
                        show_help = False
                    elif key == b"e" and not show_help:
                        expanded_history = not expanded_history
                        save_display_settings(
                            newest_first=newest_first,
                            expanded_history=expanded_history,
                            event_filter=FILTER_ORDER[event_filter_index],
                        )
                        display_notice.show(
                            expanded_history_notice(expanded_history),
                            time.monotonic(),
                        )
                    elif key == b"f" and not show_help:
                        event_filter_index = (event_filter_index + 1) % len(
                            FILTER_ORDER
                        )
                        save_display_settings(
                            newest_first=newest_first,
                            expanded_history=expanded_history,
                            event_filter=FILTER_ORDER[event_filter_index],
                        )
                        display_notice.show(
                            event_filter_notice(FILTER_ORDER[event_filter_index]),
                            time.monotonic(),
                        )
                    elif key == b"q":
                        running = False
                    elif key == b"R":
                        # Start again from the same command line, so new code
                        # and a changed config take effect without retyping it.
                        reloading = True
                        running = False
                    elif key == b"\x1b" and search and not show_help:
                        search = ""
                        display_notice.show(search_notice(search), time.monotonic())
                    elif key == b"r" and not show_help:
                        newest_first = not newest_first
                        save_display_settings(
                            newest_first=newest_first,
                            expanded_history=expanded_history,
                            event_filter=FILTER_ORDER[event_filter_index],
                        )
                        display_notice.show(
                            ordering_notice(newest_first), time.monotonic()
                        )
                    elif not show_help and (
                        key in {b"a", b"\t"} or (len(key) == 1 and key.isdigit())
                    ):
                        previous_focus = focused_root_index
                        focused_root_index = root_focus_for_key(
                            key, focused_root_index, len(states)
                        )
                        if focused_root_index != previous_focus:
                            display_notice.show(
                                root_focus_notice(
                                    focused_root_index,
                                    watch_root_labels(states),
                                    layout,
                                ),
                                time.monotonic(),
                            )
                    elif key == b"C" and not show_help:
                        if not web_panel.alive():
                            web_panel = launch_web_panel(
                                [state.root for state in states],
                                follow_herdr=follow_herdr,
                                requested_roots=requested,
                            )
                            message = (
                                "Opening the web panel in a browser…"
                                if web_panel.alive()
                                else "Could not start the web panel."
                            )
                        elif web_panel.url:
                            open_browser(web_panel.url)
                            message = web_panel_notice(web_panel.url)
                        else:
                            message = "The web panel is still starting…"
                        display_notice.show(message, time.monotonic())
                    elif key == b"p" and not show_help:
                        if paused_records is None:
                            paused_records = {
                                os.fspath(state.root): list(state.records)
                                for state in states
                            }
                            paused_new_count = 0
                            paused_new_counts = {
                                os.fspath(state.root): 0 for state in states
                            }
                        else:
                            paused_records = None
                            paused_new_count = 0
                            paused_new_counts = {}
                        display_notice.show(
                            pause_notice(paused_records is not None),
                            time.monotonic(),
                        )
            now = time.monotonic()
            # One folder sweeps the filesystem per pass. Eight big folders on
            # every pass meant seconds of walking between frames.
            due = folder_due_for_scan(states, now, poll)
            new_counts = [
                poll_watch_root(
                    state,
                    now,
                    poll,
                    github_poll,
                    poll_external=refresh_executor is None,
                    scan_files=state is due,
                )
                for state in states
            ]
            new_count = sum(new_counts)
            if refresh_executor is not None:
                apply_completed_watch_root_refreshes(states, pending_refreshes)
                schedule_watch_root_refreshes(
                    states,
                    now,
                    github_poll,
                    refresh_executor,
                    pending_refreshes,
                )
                if not interactive:
                    wait_for_watch_root_refreshes(states, pending_refreshes)
            if paused_records is not None:
                paused_new_count += new_count
                for state, root_new_count in zip(states, new_counts, strict=True):
                    source = os.fspath(state.root)
                    paused_new_counts[source] = (
                        paused_new_counts.get(source, 0) + root_new_count
                    )
            if (follow_worktrees or follow_herdr or discovering) and (
                now - last_worktree_scan >= WORKTREE_SCAN_SECONDS
            ):
                last_worktree_scan = now
                session_additions: list[Path] = []
                session_retired: list[Path] = []
                if follow_herdr:
                    live_order, current_herdr_error = herdr_session_roots()
                    if current_herdr_error and current_herdr_error != herdr_error:
                        print(
                            f"side-dog: {current_herdr_error}; keeping current folders and retrying",
                            file=sys.stderr,
                        )
                    herdr_error = current_herdr_error
                    live_folders = set(live_order)
                    session_retired, session_additions = reconcile_herdr_roots(
                        (state.root for state in states),
                        live_order,
                        requested,
                        limit,
                    )
                else:
                    # Adoption asks Herdr alone, on purpose: see agent_folders().
                    # Every watched repository contributes, not just the first.
                    live_folders = set()
                    for state in states:
                        live_folders |= agent_folders(state.root)
                    if discovering:
                        session_retired, session_additions = rediscovered_roots(
                            states, configuration, limit, requested | pinned
                        )
                worktree_additions: list[Path] = []
                if follow_worktrees:
                    worktree_additions, known_worktrees = follow_new_worktrees(
                        states,
                        known_worktrees,
                        int(time.time() * 1000),
                        limit,
                        live=live_folders,
                        ignore=ignore,
                    )
                # Retirement asks the wider question - is anybody sitting
                # here at all - because agent_folders() has looked at one
                # repository, and watching wherever agents are working means
                # several. Otherwise a landed folder is retired out from under
                # the agent still working in it.
                retired = retired_worktrees(
                    states,
                    requested,
                    live_folders | set(agent_working_folders()),
                    pinned,
                )
                retired = keep_one_root(
                    list(dict.fromkeys([*session_retired, *retired])), len(states)
                )
                if retired:
                    states = [
                        state for state in states if state.root not in retired
                    ]
                    focused_root_index = None
                    display_notice.show(
                        worktree_retire_notice(retired), time.monotonic()
                    )
                additions = list(
                    dict.fromkeys([*session_additions, *worktree_additions])
                )[: max(0, limit - len(states))]
                for addition in additions:
                    if addition in {state.root for state in states}:
                        continue
                    states.append(initialize_watch_root(addition, github_poll))
                    known_worktrees.update(
                        discovered_worktrees([addition]) | {addition}
                    )
                    if paused_records is not None:
                        source = os.fspath(addition)
                        paused_records[source] = list(states[-1].records)
                        paused_new_counts[source] = 0
                if additions:
                    if refresh_executor is None and len(states) > 1:
                        refresh_executor = ThreadPoolExecutor(
                            max_workers=min(32, len(states))
                        )
                    display_notice.show(
                        worktree_follow_notice(additions), time.monotonic()
                    )
            labels = watch_root_labels(states)
            records = aggregate_watch_records(
                states, labels, paused_records, focused_root_index
            )
            identities = aggregate_watch_identities(
                states, focused_root_index, labels
            )
            selected_indexes = selected_watch_indexes(len(states), focused_root_index)
            primary_index = selected_indexes[0]
            primary = states[primary_index]
            multi_root = len(states) > 1
            summary_now = int(time.time() * 1000)
            busiest_folder = max(
                (
                    activity_count(
                        state.records
                        if paused_records is None
                        else paused_records.get(os.fspath(state.root), []),
                        summary_now,
                    )
                    for state in states
                ),
                default=0,
            )
            summaries = (
                tuple(
                    watch_root_summary(
                        states[index],
                        labels[index],
                        summary_now,
                        None
                        if paused_records is None
                        else paused_records.get(os.fspath(states[index].root), []),
                        busiest_folder,
                    )
                    for index in selected_indexes
                )
                if multi_root
                else ()
            )
            root_activity_states = (
                tuple(
                    watch_root_activity_state(states[index])
                    for index in selected_indexes
                )
                if multi_root
                else ()
            )
            fallback_width = width if width > 0 else 80
            terminal = shutil.get_terminal_size((fallback_width, 30))
            actual_width = (
                terminal.columns if width <= 0 else min(width, terminal.columns)
            )
            current_display_notice = display_notice.current(time.monotonic())
            if should_render_root_columns(
                layout,
                actual_width,
                len(folders_worth_a_column(states)),
                focused_root_index,
                show_help,
            ):
                screen = render_root_columns(
                    states,
                    labels,
                    paused_records,
                    actual_width,
                    terminal.lines,
                    color,
                    session_filter=session_filter,
                    expanded_history=expanded_history,
                    event_filter=FILTER_ORDER[event_filter_index],
                    paused=paused_records is not None,
                    new_event_counts=paused_new_counts,
                    newest_first=newest_first,
                    display_notice=current_display_notice,
                    search=search,
                    discovered=discovering,
                )
            else:
                screen = render(
                    records,
                    primary.root,
                    actual_width,
                    terminal.lines,
                    color,
                    identities=identities,
                    session_filter=session_filter,
                    github_status=primary.github_status if not multi_root else None,
                    git_status=primary.git_status if not multi_root else None,
                    show_help=show_help,
                    expanded_history=expanded_history,
                    event_filter=FILTER_ORDER[event_filter_index],
                    paused=paused_records is not None,
                    new_event_count=paused_new_count,
                    newest_first=newest_first,
                    root_count=len(states),
                    focused_root_label=(
                        labels[focused_root_index]
                        if focused_root_index is not None
                        else None
                    ),
                    worker_count=len({name for s in states for name in s.workers}),
                    root_summaries=summaries,
                    root_activity_states=root_activity_states,
                    root_summary_color_indexes=(
                        tuple(root_color_index(index) for index in selected_indexes)
                        if multi_root
                        else ()
                    ),
                    display_notice=current_display_notice,
                    search=search,
                    repository_context=watch_repository_context(
                        [states[focused_root_index]]
                        if focused_root_index is not None
                        and focused_root_index < len(states)
                        else states
                    ),
                    discovered=discovering,
                )
            if interactive:
                sys.stdout.write("\x1b[H\x1b[2J" + screen)
                sys.stdout.flush()
            else:
                sys.stdout.write(screen + "\n")
                sys.stdout.flush()
                return 0
            time.sleep(0.15)
    finally:
        web_panel.stop()
        if refresh_executor is not None:
            refresh_executor.shutdown(wait=False, cancel_futures=True)
        if input_descriptor is not None and terminal_state is not None:
            termios.tcsetattr(input_descriptor, termios.TCSADRAIN, terminal_state)
        if interactive:
            sys.stdout.write("\x1b[?1049l\x1b[?25h")
            sys.stdout.flush()
    if reloading:
        restart_side_dog()
    return 0


def restart_side_dog() -> None:
    """Replace this process with a fresh one, same arguments.

    The terminal has already been handed back by the time this runs, so the new
    pane takes over cleanly. Folders and flags come from the command line and
    the display toggles from the settings file, so nothing is lost.
    """
    command = [*side_dog_command(), *sys.argv[1:]]
    try:
        os.execvp(command[0], command)
    except OSError:
        # Nothing to fall back to: the caller returns and Side Dog exits.
        return


def side_dog_command() -> list[str]:
    """How to run this Side Dog again: installed if it is, else from source."""
    executable = shutil.which("side-dog")
    if executable:
        return [executable]
    return [sys.executable, os.fspath(Path(__file__).resolve())]


def panel_url_from_output(line: str) -> str:
    """Pull the address out of the line the panel prints when it starts."""
    _, marker, url = line.partition(PANEL_URL_PREFIX)
    return url.strip() if marker else ""


def open_browser(url: str) -> bool:
    try:
        from side_dog.panel import launch_panel
    except ImportError:
        return False
    return bool(launch_panel(url))


@dataclass
class WebPanel:
    """The browser panel this pane started, and how to get back to it.

    The panel picks its own port and puts a one-off secret in the path, so the
    address has to be read from the panel rather than guessed. It arrives a
    moment after launch, which is why a reader thread fills it in.
    """

    process: "subprocess.Popen[bytes] | None" = None
    url: str = ""

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        if self.alive() and self.process is not None:
            self.process.terminate()


def launch_web_panel(
    roots: list[Path],
    *,
    follow_herdr: bool = False,
    requested_roots: set[Path] | None = None,
) -> WebPanel:
    """Serve the browser panel for the watched folders and open a window."""
    launch_roots = roots
    if follow_herdr and requested_roots is not None:
        launch_roots = [root for root in roots if root in requested_roots]
    command = [
        *side_dog_command(),
        "panel",
        *(os.fspath(root) for root in launch_roots),
        *(["--herdr"] if follow_herdr else []),
    ]
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return WebPanel()
    panel = WebPanel(process=process)

    def remember_url() -> None:
        stream = process.stdout
        if stream is None:
            return
        for raw_line in stream:
            url = panel_url_from_output(raw_line.decode("utf-8", "replace"))
            if url:
                panel.url = url
                return

    threading.Thread(target=remember_url, daemon=True).start()
    return panel


def tmux_pane(project: str, *, width: int) -> int:
    if "TMUX" not in os.environ:
        raise SystemExit(
            "not inside tmux; run `side-dog watch .` in a manual right split"
        )
    root = canonical_root(project)
    executable = shutil.which("side-dog")
    if executable:
        command = f"{shlex.quote(executable)} watch {shlex.quote(os.fspath(root))} --width {width}"
    else:
        command = f"{shlex.quote(sys.executable)} {shlex.quote(os.fspath(Path(__file__).resolve()))} watch {shlex.quote(os.fspath(root))} --width {width}"
    completed = subprocess.run(
        ["tmux", "split-window", "-h", "-l", str(width), command], check=False
    )
    return completed.returncode


def emit_demo(project: str) -> int:
    root = canonical_root(project)
    writer = {
        "session_id": "demo-writer",
        "turn_id": "demo-delivery-turn",
        "herdr_workspace_id": "wD",
        "herdr_tab_id": "wD:t1",
        "herdr_pane_id": "wD:p1",
    }
    reviewer = {
        "session_id": "demo-reviewer",
        "turn_id": "demo-review-turn",
        "herdr_workspace_id": "wD",
        "herdr_tab_id": "wD:t1",
        "herdr_pane_id": "wD:p2",
    }
    verified = {
        "number": 12,
        "state": "OPEN",
        "ci": "CI 3/4",
        "review": "REVIEW_REQUIRED",
        "merge_state": "BLOCKED",
        "coverage": "OK",
    }
    samples: tuple[dict[str, Any], ...] = (
        {
            **writer,
            "agent": "filesystem",
            "kind": "file",
            "status": "success",
            "title": "Wrote file",
            "detail": "side_dog/cli.py",
        },
        {
            **writer,
            "agent": "filesystem",
            "kind": "file",
            "status": "success",
            "title": "Wrote file",
            "detail": "side_dog/cli.py",
        },
        {
            **writer,
            "agent": "filesystem",
            "kind": "file",
            "status": "success",
            "title": "Wrote file",
            "detail": "side_dog/cli.py",
        },
        {
            **writer,
            "operation_id": "demo-test",
            "group_id": "demo-test",
            "kind": "test",
            "status": "running",
            "title": "Running tests",
            "detail": "pytest",
        },
        {
            **writer,
            "operation_id": "demo-test",
            "group_id": "demo-test",
            "kind": "test",
            "status": "success",
            "title": "Tests passed",
            "detail": "pytest",
        },
        {
            **writer,
            "kind": "commit",
            "status": "success",
            "title": "Commit created",
            "detail": "a1b2c3d",
        },
        {
            **writer,
            "kind": "push",
            "status": "success",
            "title": "Branch pushed",
            "detail": "origin",
        },
        {
            **writer,
            "kind": "pr",
            "status": "success",
            "title": "PR create command succeeded",
            "detail": "gh pr create",
        },
        github_event(verified, None, writer),
        {
            **reviewer,
            "kind": "config",
            "status": "success",
            "title": "Wrote config",
            "detail": "pyproject.toml",
        },
        {
            **reviewer,
            "kind": "issue",
            "status": "success",
            "title": "Closed issue",
            "detail": "#9",
        },
    )
    for event in samples:
        append_event(root, event)
        time.sleep(0.08)
    print(f"Wrote demo activity to {events_path(root)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="side-dog",
        description="Watch coding agents work in a narrow terminal pane.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="install machine-local Claude Code hooks"
    )
    init_parser.add_argument("project", nargs="?", default=".")
    init_parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="print merged settings without writing",
    )

    hook_parser = subparsers.add_parser("hook", help="receive a Claude Code hook event")
    hook_parser.add_argument("--root", help="the folder Side Dog was set up in")

    watch_parser = subparsers.add_parser(
        "watch", help="render the live narrow activity feed"
    )
    watch_parser.add_argument(
        "projects",
        nargs="*",
        default=WATCH_DEFAULT_PROJECTS,
        metavar="FOLDER",
        help=(
            "folders to watch; with none, the Herdr session you are in,"
            " else wherever agents are working"
        ),
    )
    watch_parser.add_argument(
        "--width",
        type=int,
        default=0,
        help="render width; 0 uses the full terminal pane",
    )
    watch_parser.add_argument(
        "--poll", type=float, default=0.75, help="filesystem scan interval"
    )
    watch_parser.add_argument(
        "--session",
        dest="session_filter",
        help="filter by agent pane, title, or session-id prefix",
    )
    watch_parser.add_argument(
        "--github-poll",
        type=float,
        default=15.0,
        help="seconds between verified GitHub PR readbacks; 0 disables",
    )
    watch_parser.add_argument(
        "--layout",
        choices=("auto", "timeline", "columns"),
        default="auto",
        help="how to show several folders; columns fall back when too narrow",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="print one frame and exit instead of watching",
    )
    watch_parser.add_argument(
        "--save",
        dest="save_space_as",
        metavar="NAME",
        help="save the folders being watched as NAME, for `watch @NAME`",
    )
    watch_parser.add_argument(
        "--no-follow-worktrees",
        action="store_true",
        help="watch only the folders named, never their worktrees",
    )
    watch_parser.add_argument(
        "--herdr",
        action="store_true",
        help="follow coding-agent folders in the current Herdr session",
    )
    watch_parser.add_argument("--no-color", action="store_true")

    panel_parser = subparsers.add_parser(
        "panel", help="stream the activity feed to a local browser panel"
    )
    panel_parser.add_argument(
        "projects",
        nargs="*",
        default=[],
        metavar="FOLDER",
        help="folders to show; defaults to the Herdr session or current folder",
    )
    panel_parser.add_argument(
        "--port", type=int, default=0, help="local port; 0 selects a free port"
    )
    panel_parser.add_argument(
        "--poll", type=float, default=0.75, help="JSONL polling interval"
    )
    panel_parser.add_argument(
        "--no-open", action="store_true", help="print the URL without opening it"
    )
    panel_parser.add_argument(
        "--herdr",
        action="store_true",
        help="follow coding-agent folders in the current Herdr session",
    )

    pane_parser = subparsers.add_parser(
        "tmux", help="open the feed in a right-side tmux pane"
    )
    pane_parser.add_argument("project", nargs="?", default=".")
    pane_parser.add_argument("--width", type=int, default=42)

    demo_parser = subparsers.add_parser(
        "demo", help="append representative sample activity"
    )
    demo_parser.add_argument("project", nargs="?", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if arguments[:1] == ["help"]:
        if len(arguments) == 1:
            arguments = ["--help"]
        elif len(arguments) == 2 and arguments[1] in COMMANDS:
            arguments = [arguments[1], "--help"]
        elif len(arguments) == 2:
            return command_error(f"unknown command {arguments[1]!r}")
        else:
            return command_error("help accepts at most one command")
    elif not arguments:
        return command_error("a command is required")
    elif arguments[0] not in (*COMMANDS, "-h", "--help"):
        return command_error(f"unknown command {arguments[0]!r}")

    args = parser.parse_args(arguments)
    if args.command == "hook":
        return hook(args.root)
    if args.command == "init":
        return init_claude(args.project, print_only=args.print_only)
    if args.command == "watch":
        terminal_cell_width("")
        named = [] if args.projects is WATCH_DEFAULT_PROJECTS else args.projects
        automatic_herdr = not named and invoked_within_herdr()
        return watch(
            named,
            width=args.width,
            poll=args.poll,
            no_color=args.no_color,
            layout=args.layout,
            session_filter=args.session_filter,
            github_poll=args.github_poll,
            once=args.once,
            follow_worktrees=not args.no_follow_worktrees,
            save_space_as=args.save_space_as,
            follow_herdr=args.herdr or automatic_herdr,
            require_herdr=args.herdr,
        )
    if args.command == "panel":
        from side_dog.panel import panel

        automatic_herdr = not args.projects and invoked_within_herdr()
        return panel(
            args.projects,
            port=args.port,
            poll_seconds=args.poll,
            open_window=not args.no_open,
            follow_herdr=args.herdr or automatic_herdr,
            require_herdr=args.herdr,
        )
    if args.command == "tmux":
        return tmux_pane(args.project, width=args.width)
    if args.command == "demo":
        return emit_demo(args.project)
    return 2


def command_error(message: str) -> int:
    available = ", ".join(COMMANDS)
    print(f"side-dog: {message}", file=sys.stderr)
    print(f"Available commands: {available}", file=sys.stderr)
    print("Try 'side-dog help' or 'side-dog help <command>'.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
