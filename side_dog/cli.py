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
import tempfile
import textwrap
import tty
import unicodedata
from collections import Counter, OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import IO, Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit

import zstandard

from side_dog import __version__
from side_dog.config import (
    CONFIG_HOME_ENV,
    config_display,
    config_ignores,
    config_limit,
    config_notify_enabled,
    config_pins,
    find_space,
    load_config,
    load_spaces,
    migrate_display_settings,
    path_is_ignored,
    save_space,
    spaces_path,
)
from side_dog.crush import (
    CRUSH_OVERLAP_MS,
    CrushProject,
    CrushSession,
    CrushToolLifecycle,
    crush_projects_path,
    read_crush_activity,
    read_crush_project_snapshot,
    read_crush_session_snapshot,
)
from side_dog.integrations import (
    ACTIVITY_SCHEMA,
    AgentIdentity,
    CODING_AGENT_PROVIDERS,
    HERDR_CONTEXT,
    INTEGRATIONS,
    INTEGRATION_ALIASES,
    SAFE_EVENT_FIELDS,
    SafeEvent,
    SessionKey,
    SetupRequirement,
    StreamCheckpoint,
    integration_for,
)
from side_dog.model import (
    MILESTONE_KINDS,
    SOURCE_KEY,
    SOURCE_LABEL,
    activity_unit_local_date,
    actor_label,
    agent_label,
    agent_session_key,
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
    task_status_key,
)
from side_dog.notify import notify_for_event
from side_dog.privacy import (
    EventObservation,
    PrivacyRejection,
    rejection_diagnostic,
    safe_event,
    safe_events,
)
from side_dog.polling import (
    CheckpointStore,
    PollBatch,
    PollCoordinator,
    PollErrorCode,
    PollStats,
    PollTarget,
)
from side_dog.t3code import (
    T3CODE_ACTIVITY_SOURCE,
    T3CODE_TURN_SOURCE,
    T3CodePollRequest,
    T3CodePollRow,
    T3CodeSession,
    read_t3code_poll_rows,
    read_t3code_sessions,
    t3code_database_path,
)
from side_dog.usage import (
    LiveUsageSnapshot,
    UsageBlock,
    UsageMonitor,
    UsageReport,
    load_ccusage,
    load_ccusage_block,
    live_usage_lines,
    render_usage_table,
    samples_for_sessions,
    usage_summary,
    usage_summary_wire,
)


SCHEMA = ACTIVITY_SCHEMA
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
    "GEMINI.md",
    "Makefile",
    "compose.yml",
    "compose.yaml",
    "opencode.json",
    "opencode.jsonc",
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
    ".jsonc",
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
    # Use the terminal's own theme colors rather than fixed RGB values. This is
    # what keeps semantic accents readable in both light and dark themes.
    "blue": "\x1b[34m",
    "cyan": "\x1b[36m",
    "green": "\x1b[32m",
    "magenta": "\x1b[35m",
    "red": "\x1b[31m",
    "yellow": "\x1b[33m",
}

# Semantic colors have one job everywhere Side Dog renders them. Terminal
# themes choose the final colors for these ANSI accents, so the same roles
# remain legible on light and dark backgrounds. Glyphs and words carry the
# meaning when color is disabled or unavailable.
SEMANTIC_ANSI = {
    "navigation": ANSI["blue"],
    "selection": ANSI["blue"],
    "identity": ANSI["magenta"],
    "success": ANSI["green"],
    "running": ANSI["yellow"],
    "warning": ANSI["yellow"],
    "failure": ANSI["red"],
    "idle": ANSI["dim"],
    "unknown": ANSI["dim"],
}

STATUS_GLYPHS = {
    "success": "✓",
    "running": "…",
    "warning": "!",
    "failed": "×",
    "idle": "○",
    "unknown": "?",
}

# Root colors are deliberately attached to root names and source badges instead
# of detached swatches or full-row fills. This keeps ownership explicit without
# making the accent look like progress or status, and leaves semantic status
# foregrounds readable on both dark and light terminal themes. Assignment is by
# canonical root order, not the mutable branch/PR label; roots beyond the
# palette cycle predictably.
# One color per watched root, shared by the block at the start of its lines,
# its source badge, and its column title.
ROOT_PALETTE = (39, 40, 203, 170, 184, 44, 141, 208, 75, 78, 167, 111)
# Near-black, so a root name reads on any of those bright colors.
ROOT_NAME_INK = "\x1b[38;5;16m"

GITHUB_PR_FIELDS = (
    "number,url,title,state,isDraft,headRefName,reviewDecision,mergeStateStatus,"
    "mergeable,statusCheckRollup,createdAt,updatedAt,closedAt,mergedAt"
)
DEFAULT_GITHUB_POLL_SECONDS = 60.0
GITHUB_NO_PR_POLL_SECONDS = 300.0
GITHUB_PARTIAL_POLL_SECONDS = 300.0
GITHUB_TERMINAL_POLL_SECONDS = 900.0
FILTER_ORDER = ("all", "milestones", "files")
COMMANDS = (
    "setup",
    "init",
    "doctor",
    "hook",
    "watch",
    "panel",
    "usage",
    "tmux",
    "demo",
)
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
DEEPSEEK_METADATA_CACHE: dict[str, tuple[int, dict[str, str]]] = {}
ANTIGRAVITY_METADATA_CACHE: dict[str, tuple[int, dict[str, str]]] = {}
# Compatibility names for callers that need the supported inventory. The
# registry is authoritative; Herdr aliases are normalized through
# ``integration_for`` before they reach display code.
HERDR_CODING_AGENTS = frozenset(INTEGRATION_ALIASES)
DISPLAY_CODING_AGENTS = CODING_AGENT_PROVIDERS
OPENCODE_LISTING_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
OPENCODE_LISTING_TTL_SECONDS = 2.0
OPENCODE_SESSION_WORKING_SECONDS = 60
OPENCODE_SESSION_IDENTITY_WINDOW_SECONDS = 900
OPENCODE_CHECKPOINT_SOURCE = "opencode:parts-v1"
OPENCODE_ROOT_CHECKPOINT_SOURCE = "opencode:root-baseline-v1"
OPENCODE_ROOT_CHECKPOINT_SESSION = "watch-baseline"
CRUSH_LISTING_TTL_SECONDS = 2.0
CRUSH_SESSION_WORKING_SECONDS = 60
CRUSH_SESSION_IDENTITY_WINDOW_SECONDS = 900
CRUSH_CHECKPOINT_SOURCE = "crush:messages-v1"
CRUSH_ROOT_CHECKPOINT_SOURCE = "crush:root-baseline-v1"
CRUSH_ROOT_CHECKPOINT_SESSION = "watch-baseline"
CRUSH_LISTING_CACHE: (
    tuple[
        str,
        float,
        tuple[tuple[CrushProject, tuple[CrushSession, ...]], ...],
        tuple[PollErrorCode, ...],
        int,
        tuple[CrushProject, ...],
        tuple[CrushProject, ...],
    ]
    | None
) = None
CRUSH_LISTING_LOCK = threading.Lock()
T3CODE_LISTING_TTL_SECONDS = 2.0
T3CODE_SESSION_WORKING_SECONDS = 60
T3CODE_SESSION_IDENTITY_WINDOW_SECONDS = 900
T3CODE_LISTING_CACHE: tuple[str, float, tuple[T3CodeSession, ...]] | None = None
T3CODE_LISTING_LOCK = threading.Lock()
CLINE_LISTING_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
CLINE_LISTING_TTL_SECONDS = 2.0
CLINE_SESSION_WORKING_SECONDS = 60
CLINE_SESSION_IDENTITY_WINDOW_SECONDS = 900
CLINE_NON_TERMINAL_STATUSES = {"idle", "pending", "running"}
ANTIGRAVITY_LISTING_CACHE: dict[str, tuple[float, list[tuple[Path, float]]]] = {}
ANTIGRAVITY_HISTORY_CACHE: dict[
    str, tuple[int, int, dict[str, dict[str, Any]]]
] = {}
ANTIGRAVITY_WORKER_CACHE: dict[str, tuple[int, set[str]]] = {}
ANTIGRAVITY_LISTING_TTL_SECONDS = 2.0
ANTIGRAVITY_SUBAGENT_WINDOW_SECONDS = 300
ANTIGRAVITY_SESSION_WORKING_SECONDS = 60
ANTIGRAVITY_SESSION_IDENTITY_WINDOW_SECONDS = 900
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


@dataclass(frozen=True)
class DiscoveryMode:
    key: str
    label: str
    compact: str

    def wire(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label, "compact": self.compact}


DISCOVERY_MODES = {
    mode.key: mode
    for mode in (
        DiscoveryMode("explicit-plus-herdr", "explicit folders + Herdr", "explicit + Herdr"),
        DiscoveryMode("explicit", "explicit folder selection", "explicit"),
        DiscoveryMode("required-herdr", "explicit --herdr discovery", "--herdr discovery"),
        DiscoveryMode("herdr-session", "inherited Herdr session", "Herdr session"),
        DiscoveryMode("automatic", "automatic machine-wide agent discovery", "auto agents"),
        DiscoveryMode("current-folder", "current folder fallback", "current folder"),
    )
}


def discovery_mode_from_key(key: str) -> DiscoveryMode:
    return DISCOVERY_MODES[key]


def folder_discovery_mode(
    *,
    explicit_roots: bool,
    follow_herdr: bool,
    require_herdr: bool,
    automatic: bool = True,
) -> DiscoveryMode:
    """Name why roots were selected, independently of roots joining later."""
    if explicit_roots and follow_herdr:
        return DISCOVERY_MODES["explicit-plus-herdr"]
    if explicit_roots:
        return DISCOVERY_MODES["explicit"]
    if follow_herdr and require_herdr:
        return DISCOVERY_MODES["required-herdr"]
    if follow_herdr:
        return DISCOVERY_MODES["herdr-session"]
    return DISCOVERY_MODES["automatic" if automatic else "current-folder"]


def render_discovery_mode(mode: DiscoveryMode, width: int, color: bool) -> str:
    label = mode.compact if width < 48 else mode.label
    line = crop(f" Mode: {label}", width)
    return f"{ANSI['dim']}{line}{ANSI['reset']}" if color else line


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


def expanded_header_notice(expanded: bool) -> str:
    if expanded:
        return "Expanded header — Watching and Mode details are visible."
    return "Compact header — Watching and Mode details are hidden."


def expanded_header_for_key(key: bytes, expanded: bool) -> bool:
    """Toggle header details only for uppercase E; lowercase e is history."""
    return not expanded if key == b"E" else expanded


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
    *,
    newest_first: bool,
    expanded_history: bool,
    expanded_header: bool,
    event_filter: str,
) -> None:
    path = display_settings_path()
    payload = {
        "newest_first": bool(newest_first),
        "expanded_history": bool(expanded_history),
        "expanded_header": bool(expanded_header),
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


def _diagnostic_provider(event: dict[str, Any] | SafeEvent) -> str:
    provider = normalize_agent(
        event.agent if isinstance(event, SafeEvent) else event.get("agent")
    )
    if provider in CODING_AGENT_PROVIDERS or provider in {
        "filesystem",
        "git",
        "github",
    }:
        return provider
    return "unknown"


def _validated_event(
    root: Path, event: dict[str, Any] | SafeEvent, *, now: datetime | None = None
) -> SafeEvent:
    return _validated_event_with_dedupe(root, event, now=now)[0]


def _rejected_event_dedupe_key(
    event: dict[str, Any] | SafeEvent, rejection: PrivacyRejection
) -> str:
    """Return an opaque, bounded identity for one rejected source event.

    Rejected input is never copied into the native-event index.  Hashing only
    stable identity fields lets concurrent views agree on the same diagnostic
    while bounding the amount of untrusted material inspected at this sink.
    """
    digest = hashlib.sha256(b"side-dog-rejected-event-v1\0")
    digest.update(rejection.reason.value.encode())
    for name in (
        "agent",
        "source_event_id",
        "session_id",
        "operation_id",
        "group_id",
        "turn_id",
        "kind",
        "status",
        "timestamp",
        "epoch_ms",
    ):
        value = (
            getattr(event, name) if isinstance(event, SafeEvent) else event.get(name)
        )
        if isinstance(value, str):
            encoded = value[:1_024].encode()
        elif isinstance(value, bool):
            encoded = b"true" if value else b"false"
        elif isinstance(value, int):
            # Avoid decimal conversion of an attacker-controlled huge integer.
            encoded = (
                f"bits={value.bit_length()};low={value & ((1 << 256) - 1):x}"
            ).encode()
        elif isinstance(value, float):
            encoded = value.hex().encode()
        elif value is None:
            encoded = b"null"
        else:
            encoded = b"unsupported"
        digest.update(b"\0" + name.encode() + b"\0")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return f"rejected:{digest.hexdigest()}"


def _validated_event_with_dedupe(
    root: Path, event: dict[str, Any] | SafeEvent, *, now: datetime | None = None
) -> tuple[SafeEvent, str]:
    try:
        validated = _policy_event(root, event, now=now)
        return validated, validated.source_event_id
    except PrivacyRejection as error:
        return (
            rejection_diagnostic(
                root, _diagnostic_provider(event), error.reason, now=now
            ),
            _rejected_event_dedupe_key(event, error),
        )


def _policy_event(
    root: Path, event: dict[str, Any] | SafeEvent, *, now: datetime | None = None
) -> SafeEvent:
    if isinstance(event, SafeEvent) or event.get("kind") not in {"file", "config"}:
        return safe_event(root, event, now=now)
    raw_path = event.get("detail")
    validation_wire = dict(event)
    validation_wire["detail"] = (
        "unknown config" if event.get("kind") == "config" else "unknown file"
    )
    validated = safe_event(root, validation_wire, now=now)
    observation_wire = validated.to_wire()
    for key in ("schema", "project", "detail"):
        observation_wire.pop(key, None)
    normalized = safe_events(
        root,
        EventObservation(
            **observation_wire,
            path=validated.detail if raw_path in (None, "") else raw_path,
            cwd=os.fspath(root),
        ),
        now=now,
    )
    if not normalized:
        raise PrivacyRejection("invalid_value")
    return normalized[0]


def _append_safe_event(root: Path, event: SafeEvent) -> None:
    destination = events_path(root)
    ensure_private_dir(destination.parent)
    record = event.to_wire()
    payload = (
        json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def append_event(root: Path, event: dict[str, Any] | SafeEvent) -> None:
    """Validate one event at the only durable JSONL boundary."""
    _append_safe_event(root, _validated_event(root, event))


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


def load_native_stream_position(
    root: Path, session_id: str, path: Path, agent: str = "unknown"
) -> int:
    checkpoint = CheckpointStore(native_index_path).load(
        root, SessionKey(agent, session_id), path
    )
    return checkpoint.position if checkpoint is not None else 0


def save_native_stream_position(
    root: Path,
    session_id: str,
    path: Path,
    position: int,
    agent: str = "unknown",
) -> None:
    checkpoint = StreamCheckpoint(
        session=SessionKey(agent, session_id),
        source=os.fspath(path),
        position=position,
    )
    CheckpointStore(native_index_path).save(root, checkpoint)


def native_event_count(root: Path, session_id: str, agent: str = "codex") -> int:
    prefix = f"{normalize_agent(agent)}:{session_id}:"
    escaped = prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    connection = native_index_connection(root)
    try:
        row = connection.execute(
            "SELECT count(*) FROM native_events "
            "WHERE source_event_id LIKE ? ESCAPE '!'",
            (f"{escaped}%",),
        ).fetchone()
    finally:
        connection.close()
    return int(row[0]) if row is not None else 0


_POLL_EVENT_BUFFER = threading.local()


def append_event_once(root: Path, event: dict[str, Any] | SafeEvent) -> bool:
    """Append a native agent event once, even with multiple Side Dog views open."""
    validated, source_event_id = _validated_event_with_dedupe(root, event)
    buffered: list[tuple[Path, SafeEvent]] | None = getattr(
        _POLL_EVENT_BUFFER, "events", None
    )
    if buffered is not None:
        buffered.append((canonical_root(root), validated))
        return True
    if not source_event_id:
        _append_safe_event(root, validated)
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
            _append_safe_event(root, validated)
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
    for field_name, environment_name in (
        ("herdr_pane_id", "HERDR_PANE_ID"),
        ("herdr_tab_id", "HERDR_TAB_ID"),
        ("herdr_workspace_id", "HERDR_WORKSPACE_ID"),
    ):
        value = os.environ.get(environment_name)
        if value:
            context[field_name] = value
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


def edit_path(tool_input: Any, root: Path, cwd: Any = "") -> str:
    if not isinstance(tool_input, dict):
        return "unknown file"
    for key in ("file_path", "notebook_path", "path"):
        raw_path = tool_input.get(key)
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            base = Path(cwd).expanduser() if isinstance(cwd, str) and cwd else root
            if not base.is_absolute():
                base = root / base
            path = base / path
        return os.fspath(path)
    return "unknown file"


def is_config(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.name in CONFIG_NAMES
        or candidate.suffix.lower() in CONFIG_SUFFIXES
        or any(
            part in {".claude", ".codex", ".github", ".agents", ".gemini"}
            for part in candidate.parts
        )
    )


def _safe_arg(command: str, pattern: str, fallback: str) -> str:
    match = re.search(pattern, command, flags=re.IGNORECASE)
    if not match:
        return fallback
    value = match.group(1).strip("'\"")
    if not value or value.startswith("-") or len(value) > 80:
        return fallback
    return value


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
            test_match.group(0),
            (
                r"\b(pytest|unittest|vitest|jest|cargo test|go test|rspec|mix test|"
                r"npm|pnpm|yarn|bun|make)\b"
            ),
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
    for index, token in enumerate(tokens):
        if not token or "=" in token:
            continue
        name = token.rsplit("/", 1)[-1].strip("\"'")
        if not name:
            continue
        if name in SHELL_WRAPPERS:
            following = tokens[index + 1 :]
            next_non_assignment = next(
                (candidate for candidate in following if "=" not in candidate),
                "",
            )
            if next_non_assignment.startswith("-"):
                return "command"
            continue
        if token.startswith("-"):
            return "command"
        return name[:40]
    return "command"


def operation_id(payload: dict[str, Any]) -> str:
    raw = payload.get("tool_use_id")
    if isinstance(raw, str) and raw:
        return raw
    session = str(payload.get("session_id", "unknown"))
    material = json.dumps(payload.get("tool_input", {}), sort_keys=True, default=str)
    return hashlib.sha256(f"{session}:{material}".encode()).hexdigest()[:16]


def command_stage_id(command: str, cwd: str, kind: str) -> str:
    """Identify one command stage without retaining its arguments."""

    digest = hashlib.sha256(f"{cwd}\0{kind}\0{command}".encode()).hexdigest()[:16]
    return f"{kind}:{digest}"


def normalized_tool_events(
    payload: dict[str, Any], root: Path, *, status: str
) -> list[dict[str, Any]]:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    context = hook_context(payload)
    identifier = operation_id(payload)

    if tool_name in EDIT_TOOLS:
        path = edit_path(tool_input, root, payload.get("cwd"))
        config = is_config(path)
        in_root = _native_path_matches_root(root, path)
        counts = (
            git_line_changes(root, relative_display(path, root))
            if status == "success" and in_root
            else None
        )
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
                "task_stage_id": command_stage_id(
                    command,
                    str(payload.get("cwd") or root),
                    kind,
                ),
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


def prepare_claude_settings(project: str) -> tuple[Path, str]:
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
    return settings, rendered


def write_claude_settings(settings: Path, rendered: str) -> None:
    settings.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings.with_name(f".{settings.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, rendered.encode())
    finally:
        os.close(descriptor)
    os.replace(temporary, settings)


def init_claude(project: str, *, print_only: bool = False) -> int:
    settings, rendered = prepare_claude_settings(project)
    if print_only:
        print(rendered, end="")
        return 0
    write_claude_settings(settings, rendered)
    print(f"Installed Claude Code hooks in {settings}")
    print("Restart Claude Code, then run `side-dog watch .` in a narrow right pane.")
    return 0


def _setup_confirmation(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def setup(
    project: str,
    *,
    claude: bool | None = None,
    herdr: bool | None = None,
) -> int:
    root = canonical_root(project)
    if not root.is_dir():
        raise SystemExit(f"no such folder: {root}")

    claude_detected = shutil.which("claude") is not None or (
        Path.home() / ".claude" / "sessions"
    ).is_dir()
    herdr_name = HERDR_CONTEXT.product_name
    herdr_detected = shutil.which("herdr") is not None
    if claude is None:
        claude = claude_detected and _setup_confirmation(
            "Claude Code was found. Install project-local Side Dog hooks?"
        )
    if herdr is None:
        herdr = herdr_detected and _setup_confirmation(
            f"{herdr_name} was found. Include its session discovery in launch commands?"
        )

    print(f"Side Dog setup for {root}")
    print("\nRequired")
    print("  Side Dog needs no application-wide or project configuration.")
    print("\nAgent-specific")
    for integration in INTEGRATIONS:
        if integration.setup is SetupRequirement.NONE:
            activity = integration.activity_source_summary
            if activity.startswith("Yes, "):
                activity = activity[5:]
            print(
                f"  {integration.product_name}: supported; no hooks required. "
                f"When the agent runs, Side Dog collects activity {activity}."
            )

    changed = False
    claude_integration = integration_for("claude-code")
    claude_name = (
        claude_integration.product_name
        if claude_integration is not None
        else "Claude Code"
    )
    if claude:
        settings, rendered = prepare_claude_settings(os.fspath(root))
        print(f"  {claude_name}: preview of {settings} before writing:")
        print(rendered, end="")
        write_claude_settings(settings, rendered)
        print(f"  {claude_name}: installed project-local hooks in {settings}.")
        print(f"  {claude_name}: restart Claude Code so it loads these hooks.")
        changed = True
    elif claude_detected:
        print(
            f"  {claude_name}: detected; optional project hooks were skipped "
            "for this project. Sessions can still be named."
        )
    else:
        print(
            f"  {claude_name}: supported but not detected; no hooks were "
            "written."
        )

    print("\nOptional")
    herdr_ready = False
    if herdr:
        _snapshot, herdr_error = read_herdr_snapshot()
        if herdr_error:
            print(
                f"  {herdr_name}: selected, but its health check failed: "
                f"{herdr_error}"
            )
            print(
                f"  Launch commands will use the selected project without {herdr_name}."
            )
        else:
            herdr_ready = True
            print(
                f"  {herdr_name}: selected and ready; "
                f"{HERDR_CONTEXT.session_discovery_summary.lower()}."
            )
    elif herdr_detected:
        print(
            f"  {herdr_name}: detected but not selected; "
            f"{HERDR_CONTEXT.session_discovery_summary.lower()}."
        )
    else:
        print(
            f"  {herdr_name}: not detected and not required; it would add "
            "pane, tab, workspace, and terminal-title context."
        )

    project_argument = shlex.quote(os.fspath(root))
    herdr_argument = " --herdr" if herdr_ready else ""
    print("\nStart Side Dog")
    print(f"  side-dog watch {project_argument}{herdr_argument}")
    print(f"  side-dog panel {project_argument}{herdr_argument}")
    print("\nVerify")
    print(f"  side-dog doctor {project_argument}")
    if not changed:
        print("\nSetup complete; no project files were changed.")
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


def read_new_events(
    path: Path, position: int, root: Path | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Read v1 history through the current privacy policy.

    Production callers provide ``root`` so the on-disk project field is never
    trusted. The fallback keeps this low-level reader useful for old callers
    and standalone history tests.
    """
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
                except (json.JSONDecodeError, RecursionError):
                    continue
                if isinstance(value, dict) and value.get("schema") == SCHEMA:
                    wire = {
                        key: value[key] for key in SAFE_EVENT_FIELDS if key in value
                    }
                    event_root = root
                    if event_root is None:
                        project = value.get("project")
                        event_root = (
                            Path(project)
                            if isinstance(project, str) and project
                            else path.parent
                        )
                    else:
                        wire["project"] = os.fspath(event_root)
                    try:
                        records.append(_policy_event(event_root, wire).to_wire())
                    except (PrivacyRejection, RecursionError, TypeError, ValueError):
                        continue
            return records, handle.tell()
    except OSError:
        return [], position


def latest_events(
    path: Path, limit: int = 200, *, root: Path | None = None
) -> list[dict[str, Any]]:
    records, _ = read_new_events(path, 0, root)
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


def crop_left(text: str, width: int) -> str:
    """Fit text in terminal cells while keeping its most useful tail."""
    if width <= 0:
        return ""
    if terminal_cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    budget = width - 1
    cropped: list[str] = []
    used = 0
    for cluster in reversed(list(display_clusters(text))):
        cluster_width = terminal_cell_width(cluster)
        if used + cluster_width > budget:
            break
        cropped.append(cluster)
        used += cluster_width
    return "…" + "".join(reversed(cropped))


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
    status = str(event.get("status") or "unknown").casefold()
    kind = str(event.get("kind") or "")
    if status == "failed":
        return STATUS_GLYPHS["failed"], SEMANTIC_ANSI["failure"]
    if status in {"running", "pending"}:
        return STATUS_GLYPHS["running"], SEMANTIC_ANSI["running"]
    if status in {"warning", "partial"}:
        return STATUS_GLYPHS["warning"], SEMANTIC_ANSI["warning"]
    if status == "unknown":
        return STATUS_GLYPHS["unknown"], SEMANTIC_ANSI["unknown"]
    if kind == "github":
        github_state = event.get("github_state")
        if github_state == "MERGED":
            return "⇉", SEMANTIC_ANSI["success"]
        if github_state == "CLOSED":
            return "×", SEMANTIC_ANSI["unknown"]
        return "↗", (
            SEMANTIC_ANSI["success"]
            if status == "success"
            else SEMANTIC_ANSI["navigation"]
        )
    glyphs = {
        "file": "✎",
        "config": "⚙",
        "test": "✓",
        "branch": "⑂",
        "worktree": "⌘",
        "commit": "◆",
        "push": "↑",
        "pr": "↗",
        "merge": "⇉",
        "issue": "◈",
        "session": "◇",
        "command": "×",
        "search": "⌕",
        "todo": "☐",
    }
    glyph = glyphs.get(kind, "·")
    if status in {"success", "completed", "done", "finished"}:
        return glyph, SEMANTIC_ANSI["success"]
    return STATUS_GLYPHS["unknown"], SEMANTIC_ANSI["unknown"]


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


def _dsh_chunks(
    path: Path,
    position: int,
    *,
    limit: int | None = None,
    end: int | None = None,
    health: _PollHealth | None = None,
) -> tuple[list[bytes], int]:
    """Read complete DeepSeek Harness records or Zstandard frames.

    Harness writes its default compressed log as independent frames: one for
    the header and one per durable append batch.  Saving only complete frame
    boundaries gives Side Dog the same restart behavior it has for plain
    JSONL, including while Harness is still appending the next frame.
    """
    try:
        size = path.stat().st_size
    except OSError:
        if health is not None:
            health.record(PollErrorCode.IO)
        return [], position
    if position > size:
        position = 0
    read_end = min(size, end) if end is not None else size
    if path.suffix != ".zstd":
        chunks: list[bytes] = []
        try:
            with path.open("rb") as handle:
                handle.seek(position)
                while handle.tell() < read_end:
                    line_start = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line or not raw_line.endswith(b"\n"):
                        handle.seek(line_start)
                        break
                    if handle.tell() > read_end:
                        handle.seek(line_start)
                        break
                    chunks.append(raw_line)
                    position = handle.tell()
                    if limit is not None and len(chunks) >= limit:
                        break
        except OSError:
            if health is not None:
                health.record(PollErrorCode.IO)
            return [], position
        return chunks, position

    chunks = []
    next_position = position
    try:
        with path.open("rb") as handle:
            while next_position < read_end:
                handle.seek(next_position)
                decoder = zstandard.ZstdDecompressor().decompressobj(
                    read_across_frames=False
                )
                decoded_parts: list[bytes] = []
                while handle.tell() < read_end:
                    compressed = handle.read(min(64 * 1024, read_end - handle.tell()))
                    if not compressed:
                        return chunks, next_position
                    try:
                        decoded_parts.append(decoder.decompress(compressed))
                    except zstandard.ZstdError:
                        if health is not None:
                            health.record(PollErrorCode.PARSE)
                        return chunks, next_position
                    if not decoder.eof:
                        continue
                    frame_end = handle.tell() - len(decoder.unused_data)
                    if frame_end <= next_position:
                        return chunks, next_position
                    chunks.append(b"".join(decoded_parts))
                    next_position = frame_end
                    break
                else:
                    return chunks, next_position
                if limit is not None and len(chunks) >= limit:
                    break
    except OSError:
        if health is not None:
            health.record(PollErrorCode.IO)
        return [], position
    return chunks, next_position


def _dsh_records(
    path: Path,
    position: int,
    *,
    limit_chunks: int | None = None,
    end: int | None = None,
    health: _PollHealth | None = None,
) -> tuple[list[dict[str, Any]], int]:
    chunks, next_position = _dsh_chunks(
        path, position, limit=limit_chunks, end=end, health=health
    )
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        for raw_line in chunk.splitlines():
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if health is not None:
                    health.record(PollErrorCode.PARSE)
                continue
            if isinstance(record, dict):
                records.append(record)
            elif health is not None:
                health.record(PollErrorCode.PARSE)
    return records, next_position


def deepseek_session_path(session_id: str) -> Path | None:
    if not session_id:
        return None
    return resolve_session_path(
        f"deepseek:{session_id}",
        lambda: next(
            (
                path
                for path, _ in deepseek_session_listing()
                if dsh_session_header(path).get("id") == session_id
            ),
            None,
        ),
    )


def _deepseek_metadata_record(
    metadata: dict[str, str], record: dict[str, Any]
) -> None:
    data = record.get("data")
    if not isinstance(data, dict):
        return
    model: Any = None
    effort: Any = None
    if record.get("type") == "request/context":
        model = data.get("model")
    elif record.get("type") == "request/header":
        header = data.get("header")
        config = header.get("config") if isinstance(header, dict) else None
        if isinstance(config, dict):
            model = config.get("model")
            effort = config.get("reasoningEffort")
    elif record.get("type") == "model/selection":
        selection = data.get("selection")
        source = selection if isinstance(selection, dict) else data
        model = source.get("model")
        effort = source.get("reasoningEffort")
    if isinstance(model, str) and model:
        metadata["model"] = model
    if isinstance(effort, str) and effort:
        metadata["effort"] = effort


def load_deepseek_metadata(session_id: str) -> dict[str, str]:
    """Model and reasoning effort from a Harness session's public envelopes."""
    path = deepseek_session_path(session_id)
    if path is None:
        return {}
    cache_key = os.fspath(path)
    position, metadata = DEEPSEEK_METADATA_CACHE.get(cache_key, (0, {}))
    prior_position = position
    records, position = _dsh_records(path, position)
    if position < prior_position:
        metadata = {}
    for record in records:
        _deepseek_metadata_record(metadata, record)
    DEEPSEEK_METADATA_CACHE[cache_key] = (position, metadata)
    return dict(metadata)


def antigravity_session_path(session_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", session_id):
        return None
    return resolve_session_path(
        f"antigravity:{session_id}",
        lambda: _locate_antigravity_session(session_id),
    )


def _locate_antigravity_session(session_id: str) -> Path | None:
    for base in antigravity_sessions_roots():
        brain = base / "brain" / session_id
        if not brain.is_dir():
            continue
        transcript = brain / ".system_generated" / "logs" / "transcript.jsonl"
        if transcript.is_file():
            return transcript
        transcript_flat = brain / "transcript.jsonl"
        if transcript_flat.is_file():
            return transcript_flat
    return None


def load_antigravity_metadata(session_id: str) -> dict[str, str]:
    """Model and reasoning effort for an Antigravity session."""
    path = antigravity_session_path(session_id)
    if path is None:
        return {}
    cache_key = os.fspath(path)
    position, metadata = ANTIGRAVITY_METADATA_CACHE.get(cache_key, (0, {}))
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            if position > size:
                position, metadata = 0, {}
            handle.seek(position)
            for raw_line in transcript_lines(handle):
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                model = (
                    record.get("model")
                    or record.get("model_name")
                    or record.get("modelName")
                )
                if isinstance(model, str) and model:
                    metadata["model"] = model
                effort = (
                    record.get("effort")
                    or record.get("thinking_level")
                    or record.get("thinkingLevel")
                )
                if isinstance(effort, str) and effort:
                    metadata["effort"] = effort
            position = handle.tell()
    except OSError:
        return dict(metadata)
    ANTIGRAVITY_METADATA_CACHE[cache_key] = (position, metadata)
    return dict(metadata)


@dataclass
class NativeAgentStream:
    session_id: str
    path: Path
    position: int
    agent: str = "codex"
    agent_root: str = ""
    model: str = ""
    effort: str = ""
    turn_id: str = ""
    session_cwd: str = ""
    pending_commands: deque[tuple[str, str, str]] = field(
        default_factory=lambda: deque(maxlen=256)
    )
    completed_commands: deque[tuple[str, str, str, str]] = field(
        default_factory=lambda: deque(maxlen=256)
    )
    # Pi splits a tool call from its result; the arguments live only in the
    # call, so they wait here, keyed by call id, until the result lands.
    pending_calls: "OrderedDict[str, dict[str, Any]]" = field(
        default_factory=OrderedDict
    )
    pending_tools: dict[str, tuple[str, dict[str, Any], str]] = field(
        default_factory=dict
    )
    # Antigravity identifies results by the following transcript step rather
    # than a call id, and background task results can arrive later.
    antigravity_pending_calls: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict
    )
    record_position: int = 0


@dataclass
class OpenCodeStream:
    """One opencode session being tailed from its shared SQLite store.

    Opencode writes every session into a single database, so a stream carries
    the database path rather than a per-session transcript, and the cursor is
    the highest ``part.time_updated`` already read. There is no backfill: a new
    stream starts at the watcher's baseline, so only activity from now on is
    ingested. A subagent's stream is keyed by its own session id but attributes
    its events to ``context_session_id``, the parent.
    """

    session_id: str
    db_path: Path
    position: int
    agent_root: str = ""
    model: str = ""
    effort: str = ""
    context_session_id: str = ""
    cursor_signature: str = ""
    processed: set[tuple[str, str]] = field(default_factory=set)
    processed_order: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=4096), repr=False
    )


@dataclass
class ClineStream:
    """One Cline session whose privacy-filtered message metadata is watched."""

    session_id: str
    path: Path
    agent_root: str = ""
    model: str = ""
    effort: str = ""
    context_session_id: str = ""
    turn_id: str = ""
    fingerprint: tuple[int, int] | None = None
    message_count: int = 0
    message_prefix_signature: str = ""
    processed: set[tuple[str, str]] = field(default_factory=set)
    processed_order: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=4096), repr=False
    )


@dataclass
class _PollHealth:
    parse_errors: int = 0
    last_error: PollErrorCode | None = None

    def record(self, code: PollErrorCode) -> None:
        if code == PollErrorCode.PARSE:
            self.parse_errors += 1
        self.last_error = code


def _remember_processed(
    stream: OpenCodeStream | ClineStream, key: tuple[str, str]
) -> None:
    """Remember a replay key without allowing a long session to grow forever."""
    if key in stream.processed:
        return
    if len(stream.processed_order) == stream.processed_order.maxlen:
        stream.processed.discard(stream.processed_order[0])
    stream.processed_order.append(key)
    stream.processed.add(key)


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
            if not fallback_root:
                return False
            base = canonical_root(fallback_root)
            return base == root or base.is_relative_to(root)
        except (OSError, ValueError):
            return False
    try:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute() and fallback_root:
            candidate = Path(fallback_root).expanduser() / candidate
        path = candidate.resolve(strict=False)
        probe = path if path.is_dir() else path.parent
        reported_root = git_worktree_root(os.fspath(probe))
        if reported_root and canonical_root(reported_root) == root:
            return True
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
    raw = record.get("timestamp") or record.get("created_at")
    if isinstance(raw, str):
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return {}
        return {"timestamp": raw, "epoch_ms": int(instant.timestamp() * 1000)}
    return {}


def _stream_context(stream: NativeAgentStream) -> dict[str, str]:
    context = {"agent": stream.agent, "session_id": stream.session_id}
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
                "cwd": workdir,
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
            "cwd": workdir,
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
        raw_reference = item.get("agent_thread_id")
        if not isinstance(raw_reference, str) or not raw_reference:
            raw_reference = (
                raw_agent_path
                if isinstance(raw_agent_path, str) and raw_agent_path
                else item_id
            )
        opaque_reference = hashlib.sha256(
            raw_reference[:1_024].encode(errors="replace")
        ).hexdigest()[:16]
        identifier = f"subagent:{opaque_reference}"
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
                    "detail": "subagent",
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
            "cwd": workdir,
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


DEEPSEEK_MUTATING_EDITOR_COMMANDS = {
    "create",
    "insert",
    "str_replace",
    "undo_edit",
}


def _deepseek_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _deepseek_absolute_path(raw: str, session_cwd: str) -> str:
    path = Path(raw).expanduser()
    if not path.is_absolute() and session_cwd:
        path = Path(session_cwd).expanduser() / path
    return os.fspath(path)


def _deepseek_call_summary(
    stream: NativeAgentStream, data: dict[str, Any]
) -> tuple[str, dict[str, Any], str] | None:
    """Keep only the tool fields Side Dog's event normalizer needs."""
    name = str(data.get("name") or "").casefold()
    arguments = _deepseek_arguments(data.get("arguments"))
    session_cwd = stream.session_cwd or stream.agent_root
    if name in {"bash", "pwsh", "terminal"}:
        command = arguments.get("command")
        if not isinstance(command, str) or not command:
            return None
        raw_workdir = arguments.get("workdir")
        workdir = raw_workdir if isinstance(raw_workdir, str) else session_cwd
        workdir = _deepseek_absolute_path(workdir, session_cwd)
        return "Bash", {"command": command}, workdir
    if name in {"write", "edit", "str_replace_editor"}:
        if name == "str_replace_editor":
            command = str(arguments.get("command") or "").casefold()
            if command not in DEEPSEEK_MUTATING_EDITOR_COMMANDS:
                return None
        raw_path = next(
            (
                arguments[key]
                for key in ("path", "filePath", "file_path")
                if isinstance(arguments.get(key), str) and arguments[key]
            ),
            None,
        )
        if raw_path is None:
            return None
        absolute_path = _deepseek_absolute_path(raw_path, session_cwd)
        tool_name = "Write" if name == "write" else "Edit"
        return tool_name, {"path": absolute_path}, session_cwd
    if name in {"subagent", "subagent_fork"}:
        return "Subagent", {}, session_cwd
    return None


def _remember_deepseek_call(
    stream: NativeAgentStream, data: dict[str, Any]
) -> tuple[str, dict[str, Any], str] | None:
    call_id = data.get("callId")
    if not isinstance(call_id, str) or not call_id:
        return None
    summary = _deepseek_call_summary(stream, data)
    if summary is None:
        return None
    if len(stream.pending_tools) >= 256 and call_id not in stream.pending_tools:
        del stream.pending_tools[next(iter(stream.pending_tools))]
    stream.pending_tools[call_id] = summary
    return summary


def _deepseek_result(
    data: dict[str, Any], call_id: str, tool_name: str
) -> str | None:
    message = data.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "tool-result":
            continue
        if block.get("toolCallId") != call_id:
            continue
        if block.get("isError") is True:
            return "failed"
        if tool_name != "Bash":
            return "success"
        content = block.get("content")
        if isinstance(content, list):
            for item in content:
                text = item.get("text") if isinstance(item, dict) else None
                if not isinstance(text, str):
                    continue
                match = re.search(r"\[exit code:\s*(-?\d+)\]", text)
                if match is not None:
                    return "success" if int(match.group(1)) == 0 else "failed"
        return "success"
    return None


def _deepseek_subagent_event(
    root: Path,
    stream: NativeAgentStream,
    call_id: str,
    status: str,
    timing: dict[str, Any],
) -> int:
    if not _native_path_matches_root(
        root, "", stream.session_cwd or stream.agent_root
    ):
        return 0
    title = {
        "running": "Subagent started",
        "success": "Subagent completed",
        "failed": "Subagent failed",
    }[status]
    phase = "running" if status == "running" else "complete"
    return int(
        append_event_once(
            root,
            {
                **hook_context(_stream_context(stream)),
                **timing,
                "operation_id": f"deepseek:{call_id}:subagent",
                "group_id": f"deepseek:{call_id}",
                "kind": "session",
                "status": status,
                "title": title,
                "detail": "subagent",
                "source_event_id": (
                    f"deepseek:{stream.session_id}:call:{call_id}:{phase}:subagent"
                ),
            },
        )
    )


def _update_deepseek_stream_state(
    stream: NativeAgentStream, record: dict[str, Any]
) -> None:
    metadata = {"model": stream.model, "effort": stream.effort}
    _deepseek_metadata_record(metadata, record)
    stream.model = metadata.get("model", "")
    stream.effort = metadata.get("effort", "")
    if record.get("type") == "session":
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            stream.session_cwd = cwd
        return
    data = record.get("data")
    if not isinstance(data, dict):
        return
    if record.get("type") == "turn/start":
        turn = data.get("turn")
        if isinstance(turn, int) and not isinstance(turn, bool):
            stream.turn_id = f"turn-{turn}"
    elif record.get("type") == "tool/call":
        _remember_deepseek_call(stream, data)
    elif record.get("type") == "tool/result":
        message = data.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if isinstance(blocks, list):
            for block in blocks:
                call_id = block.get("toolCallId") if isinstance(block, dict) else None
                if isinstance(call_id, str):
                    stream.pending_tools.pop(call_id, None)


def _rehydrate_deepseek_stream(stream: NativeAgentStream) -> None:
    if stream.position <= 0:
        return
    records, _ = _dsh_records(stream.path, 0, end=stream.position)
    for record in records:
        _update_deepseek_stream_state(stream, record)


def _poll_deepseek_record(
    root: Path, stream: NativeAgentStream, record: dict[str, Any]
) -> int:
    record_type = record.get("type")
    data = record.get("data")
    timing = _record_time(record, "time")
    if record_type == "session":
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            stream.session_cwd = cwd
        return 0
    if record_type in {"request/context", "request/header", "model/selection"}:
        metadata = {"model": stream.model, "effort": stream.effort}
        _deepseek_metadata_record(metadata, record)
        stream.model = metadata.get("model", "")
        stream.effort = metadata.get("effort", "")
        return 0
    if not isinstance(data, dict):
        return 0
    if record_type == "turn/start":
        turn = data.get("turn")
        if isinstance(turn, int) and not isinstance(turn, bool):
            stream.turn_id = f"turn-{turn}"
        return 0
    if record_type == "turn/end":
        turn = data.get("turn")
        turn_id = f"turn-{turn}" if isinstance(turn, int) else stream.turn_id
        if turn_id:
            stream.turn_id = turn_id
        reason = data.get("reason")
        reason_kind = (
            str(reason.get("kind") or "").casefold()
            if isinstance(reason, dict)
            else ""
        )
        status = (
            "success"
            if reason_kind == "completed"
            else "failed" if reason_kind == "error" else "unknown"
        )
        identifier = f"deepseek:{stream.session_id}:{turn_id or 'turn'}:end"
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
                    "title": "DeepSeek turn finished",
                    "detail": "",
                    "source_event_id": identifier,
                },
            )
        )
    if record_type == "tool/call":
        call_id = data.get("callId")
        if not isinstance(call_id, str) or not call_id:
            return 0
        summary = _remember_deepseek_call(stream, data)
        if summary is None:
            return 0
        tool_name, tool_input, workdir = summary
        if tool_name == "Subagent":
            return _deepseek_subagent_event(
                root, stream, call_id, "running", timing
            )
        raw_path = tool_input.get("path") if tool_name in EDIT_TOOLS else workdir
        if not _native_path_matches_root(
            root, str(raw_path or ""), stream.session_cwd or stream.agent_root
        ):
            return 0
        return _append_native_tool_events(
            root,
            {
                **_stream_context(stream),
                "cwd": workdir,
                "tool_use_id": call_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            "running",
            f"deepseek:{stream.session_id}:call:{call_id}:running",
            timing,
        )
    if record_type != "tool/result":
        return 0
    message = data.get("message")
    blocks = message.get("content") if isinstance(message, dict) else None
    if not isinstance(blocks, list):
        return 0
    count = 0
    for block in blocks:
        call_id = block.get("toolCallId") if isinstance(block, dict) else None
        if not isinstance(call_id, str) or not call_id:
            continue
        summary = stream.pending_tools.pop(call_id, None)
        if summary is None:
            continue
        tool_name, tool_input, workdir = summary
        status = _deepseek_result(data, call_id, tool_name)
        if status is None:
            continue
        if tool_name == "Subagent":
            count += _deepseek_subagent_event(root, stream, call_id, status, timing)
            continue
        raw_path = tool_input.get("path") if tool_name in EDIT_TOOLS else workdir
        if not _native_path_matches_root(
            root, str(raw_path or ""), stream.session_cwd or stream.agent_root
        ):
            continue
        count += _append_native_tool_events(
            root,
            {
                **_stream_context(stream),
                "cwd": workdir,
                "tool_use_id": call_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
            },
            status,
            f"deepseek:{stream.session_id}:call:{call_id}:complete",
            timing,
        )
    return count


# A Pi call whose result never lands must not grow the map without bound.
PI_PENDING_CALLS_LIMIT = 256


def _pi_abs_path(raw: str, session_cwd: str) -> str:
    """Resolve a Pi tool path against the session's launch directory.

    Pi may be started in a subdirectory, so a relative path like `foo.py`
    belongs under that directory, not the repository root the shared pipeline
    would otherwise assume. An absolute path is returned unchanged.
    """
    path = Path(raw).expanduser()
    if not path.is_absolute() and session_cwd:
        path = Path(session_cwd) / path
    return os.fspath(path)


def _pi_call_payload(
    root: Path, stream: NativeAgentStream, item: dict[str, Any]
) -> tuple[str, dict[str, Any], bool] | None:
    """Turn a Pi `toolCall` into a normalized payload and whether it is in root.

    Returns None for tools with no activity to report (notably `read`). The
    third element says whether the call belongs to the watched repository:
    Bash is scoped by the session's launch directory - Side Dog never parses a
    command for target paths - while writes are scoped by their resolved path.
    """
    call_id = item.get("id")
    if not isinstance(call_id, str) or not call_id:
        return None
    name = str(item.get("name") or "").casefold()
    arguments = item.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "bash":
        command = arguments.get("command")
        if not isinstance(command, str) or not command:
            return None
        payload = {
            **_stream_context(stream),
            "cwd": stream.session_cwd or stream.agent_root,
            "tool_use_id": call_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        in_root = _native_path_matches_root(root, "", stream.session_cwd)
        return call_id, payload, in_root
    if name in {"write", "edit"}:
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return None
        absolute = _pi_abs_path(raw_path, stream.session_cwd)
        payload = {
            **_stream_context(stream),
            "tool_use_id": call_id,
            "tool_name": "Write" if name == "write" else "Edit",
            "tool_input": {"path": absolute},
        }
        in_root = _native_path_matches_root(root, absolute, stream.session_cwd)
        return call_id, payload, in_root
    return None


def _pi_remember_call(
    root: Path, stream: NativeAgentStream, item: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """Register an in-root Pi call so its later result can complete it."""
    built = _pi_call_payload(root, stream, item)
    if built is None:
        return None
    call_id, payload, in_root = built
    if not in_root:
        return None
    stream.pending_calls[call_id] = {"payload": payload}
    while len(stream.pending_calls) > PI_PENDING_CALLS_LIMIT:
        stream.pending_calls.popitem(last=False)
    return call_id, payload


def _pi_scope_context(root: Path, stream: NativeAgentStream) -> dict[str, Any] | None:
    """Session-framing context, only when the session is inside this root."""
    if not _native_path_matches_root(root, "", stream.session_cwd):
        return None
    return hook_context(_stream_context(stream))


def _emit_pi_session_start(
    root: Path, stream: NativeAgentStream, record: dict[str, Any]
) -> int:
    context = _pi_scope_context(root, stream)
    if context is None:
        return 0
    identifier = f"pi:{stream.session_id}:session:start"
    return int(
        append_event_once(
            root,
            {
                **context,
                **_record_time(record),
                "operation_id": identifier,
                "group_id": identifier,
                "kind": "session",
                "status": "success",
                "title": "Pi session active",
                "source_event_id": identifier,
            },
        )
    )


def _emit_pi_tool_calls(
    root: Path,
    stream: NativeAgentStream,
    record: dict[str, Any],
    message: dict[str, Any],
) -> int:
    content = message.get("content")
    if not isinstance(content, list):
        return 0
    timing = _record_time(record)
    count = 0
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "toolCall":
            continue
        remembered = _pi_remember_call(root, stream, item)
        if remembered is None:
            continue
        call_id, payload = remembered
        count += _append_native_tool_events(
            root,
            payload,
            "running",
            f"pi:{stream.session_id}:call:{call_id}:running",
            timing,
        )
    return count


def _emit_pi_tool_result(
    root: Path,
    stream: NativeAgentStream,
    record: dict[str, Any],
    message: dict[str, Any],
) -> int:
    call_id = message.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return 0
    pending = stream.pending_calls.pop(call_id, None)
    if pending is None:
        return 0
    status = "failed" if message.get("isError") else "success"
    timing = _record_time(message, "timestamp") or _record_time(record)
    return _append_native_tool_events(
        root,
        pending["payload"],
        status,
        f"pi:{stream.session_id}:call:{call_id}:output",
        timing,
    )


def _pi_message_is_text_only(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    saw_text = False
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "toolCall":
            return False
        if kind == "text":
            saw_text = True
    return saw_text


def _emit_pi_turn_finished(
    root: Path, stream: NativeAgentStream, record: dict[str, Any]
) -> int:
    context = _pi_scope_context(root, stream)
    if context is None or not stream.turn_id:
        return 0
    identifier = f"pi:{stream.session_id}:turn:{stream.turn_id}:end"
    emitted = int(
        append_event_once(
            root,
            {
                **context,
                **_record_time(record),
                "operation_id": identifier,
                "group_id": identifier,
                "kind": "session",
                "status": "success",
                "title": "Pi turn finished",
                "source_event_id": identifier,
            },
        )
    )
    stream.turn_id = ""
    return emitted


def _replay_pi_pending(
    root: Path, stream: NativeAgentStream, record: dict[str, Any]
) -> None:
    """Rebuild session state and open calls without emitting any events."""
    record_type = record.get("type")
    if record_type == "session":
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            stream.session_cwd = cwd
        return
    if record_type == "model_change":
        model = record.get("modelId")
        if isinstance(model, str) and model:
            stream.model = model
        return
    if record_type == "thinking_level_change":
        effort = record.get("thinkingLevel")
        if isinstance(effort, str) and effort:
            stream.effort = effort
        return
    if record_type != "message":
        return
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    role = message.get("role")
    if role == "user":
        record_id = record.get("id")
        if isinstance(record_id, str) and record_id:
            stream.turn_id = record_id
    elif role == "assistant":
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "toolCall":
                    _pi_remember_call(root, stream, item)
        if _pi_message_is_text_only(message):
            stream.turn_id = ""
    elif role == "toolResult":
        call_id = message.get("toolCallId")
        if isinstance(call_id, str):
            stream.pending_calls.pop(call_id, None)


def _reconstruct_pi_stream(root: Path, stream: NativeAgentStream) -> None:
    """Replay a Pi transcript up to the saved cursor, emitting nothing.

    Restores `session_cwd`, model, effort, the open turn, and the calls still
    awaiting a result, so a restart between a call and its result still lets the
    result complete its operation.
    """
    if stream.position <= 0:
        return
    try:
        with stream.path.open("rb") as handle:
            consumed = 0
            while consumed < stream.position:
                raw_line = handle.readline()
                if not raw_line:
                    break
                consumed += len(raw_line)
                if consumed > stream.position or not raw_line.endswith(b"\n"):
                    break
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(record, dict):
                    _replay_pi_pending(root, stream, record)
    except OSError:
        return


def _poll_pi_record(
    root: Path, stream: NativeAgentStream, record: dict[str, Any]
) -> int:
    record_type = record.get("type")
    if record_type == "session":
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            stream.session_cwd = cwd
        return _emit_pi_session_start(root, stream, record)
    if record_type == "model_change":
        model = record.get("modelId")
        if isinstance(model, str) and model:
            stream.model = model
        return 0
    if record_type == "thinking_level_change":
        effort = record.get("thinkingLevel")
        if isinstance(effort, str) and effort:
            stream.effort = effort
        return 0
    if record_type != "message":
        return 0
    message = record.get("message")
    message = message if isinstance(message, dict) else {}
    role = message.get("role")
    if role == "user":
        record_id = record.get("id")
        if isinstance(record_id, str) and record_id:
            stream.turn_id = record_id
        return 0
    if role == "assistant":
        count = _emit_pi_tool_calls(root, stream, record, message)
        if _pi_message_is_text_only(message):
            count += _emit_pi_turn_finished(root, stream, record)
        return count
    if role == "toolResult":
        return _emit_pi_tool_result(root, stream, record, message)
    return 0


def _antigravity_call_args(call: dict[str, Any]) -> dict[str, Any]:
    args = call.get("args") or call.get("toolArgs") or call.get("parameters") or {}
    return args if isinstance(args, dict) else {}


def _antigravity_subagent_roles(args: dict[str, Any]) -> list[str]:
    raw_subagents = args.get("Subagents") or args.get("subagents")
    if not isinstance(raw_subagents, list):
        raw_subagents = [args]
    roles: list[str] = []
    for subagent in raw_subagents:
        if not isinstance(subagent, dict):
            continue
        role = (
            subagent.get("Role")
            or subagent.get("role")
            or subagent.get("TypeName")
            or subagent.get("typeName")
        )
        if isinstance(role, str) and role:
            roles.append(" ".join(role.split())[:80])
    return roles


def _antigravity_subagent_detail(args: dict[str, Any]) -> str:
    roles = _antigravity_subagent_roles(args)
    return ", ".join(roles) if roles else "subagent"


def _append_antigravity_call_events(
    root: Path,
    stream: NativeAgentStream,
    call: dict[str, Any],
    status: str,
    timing: dict[str, Any],
    phase: str,
) -> int:
    tool_name = str(call.get("tool_name") or "")
    args = call.get("args")
    if not isinstance(args, dict):
        return 0
    step_idx = call.get("step_idx", 0)
    call_idx = call.get("call_idx", 0)
    call_id = f"{step_idx}:{call_idx}"
    source_prefix = (
        f"antigravity:{stream.session_id}:step:{step_idx}:{call_idx}:{phase}"
    )
    if tool_name == "run_command":
        command = str(args.get("CommandLine") or args.get("command") or "")
        workdir = str(args.get("Cwd") or args.get("cwd") or stream.agent_root)
        if not command or not _native_path_matches_root(
            root, workdir, stream.agent_root
        ):
            return 0
        payload = {
            **_stream_context(stream),
            "cwd": workdir,
            "tool_use_id": f"antigravity:{stream.session_id}:{call_id}",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        return _append_native_tool_events(root, payload, status, source_prefix, timing)
    if tool_name in {"write_to_file", "replace_file_content"}:
        raw_path = str(
            args.get("TargetFile")
            or args.get("target_file")
            or args.get("path")
            or ""
        )
        if not raw_path or not _native_path_matches_root(
            root, raw_path, stream.agent_root
        ):
            return 0
        payload = {
            **_stream_context(stream),
            "tool_use_id": f"antigravity:{stream.session_id}:{call_id}",
            "tool_name": "Write" if tool_name == "write_to_file" else "Edit",
            "tool_input": {"path": raw_path},
        }
        return _append_native_tool_events(root, payload, status, source_prefix, timing)
    if tool_name != "invoke_subagent":
        return 0
    if not _native_path_matches_root(root, "", stream.agent_root):
        return 0
    identifier = f"subagent:{stream.session_id}:{call_id}"
    title = {
        "running": "Subagent started",
        "success": "Subagent completed",
        "failed": "Subagent failed",
    }.get(status, "Subagent status unknown")
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
                "detail": _antigravity_subagent_detail(args),
                "source_event_id": f"{source_prefix}:subagent",
            },
        )
    )


ANTIGRAVITY_EXIT_CODE = re.compile(
    r"(?i)\b(?:exited with (?:code|status)|exit(?:ed)? (?:code|status)|"
    r"return code)\s*:?\s*(-?\d+)"
)
ANTIGRAVITY_TASK_ID = re.compile(r"(?im)\btask id\s*:\s*[\"']?([a-z0-9][a-z0-9._/-]*)")
ANTIGRAVITY_TASK_STATUS = re.compile(
    r"(?im)^status\s*:\s*(done|completed|success|running|pending|"
    r"error|failed|cancelled|canceled)\s*$"
)


def _antigravity_command_results(record: dict[str, Any]) -> list[tuple[str, str]]:
    """Ordered command outcomes as ``(status, background task ID)`` pairs."""
    content = record.get("content")
    if not isinstance(content, str):
        return []
    results: list[tuple[int, str, str]] = []
    for match in ANTIGRAVITY_EXIT_CODE.finditer(content):
        status = "success" if int(match.group(1)) == 0 else "failed"
        results.append((match.start(), status, ""))
    for match in ANTIGRAVITY_TASK_ID.finditer(content):
        results.append((match.start(), "running", match.group(1)))
    results.sort(key=lambda item: item[0])
    return [(status, task_id) for _, status, task_id in results]


def _antigravity_result_status(
    call: dict[str, Any], record: dict[str, Any]
) -> str:
    raw_status = str(record.get("status") or "").casefold()
    if raw_status in {"running", "pending"}:
        return "running"
    if raw_status in {"error", "failed", "cancelled", "canceled"}:
        return "failed"
    if call.get("tool_name") == "run_command":
        content = record.get("content")
        if isinstance(content, str):
            match = ANTIGRAVITY_EXIT_CODE.search(content)
            if match is not None:
                return "success" if int(match.group(1)) == 0 else "failed"
            task_status = ANTIGRAVITY_TASK_STATUS.search(content)
            if task_status is not None:
                normalized = task_status.group(1).casefold()
                if normalized in {"running", "pending"}:
                    return "running"
                if normalized in {"error", "failed", "cancelled", "canceled"}:
                    return "failed"
                return "success"
        return "unknown"
    return "success" if raw_status in {"done", "completed", "success"} else "unknown"


def _remember_antigravity_call(
    stream: NativeAgentStream, key: str, call: dict[str, Any]
) -> None:
    """Bound incomplete Antigravity calls while keeping recent results joinable."""
    pending = stream.antigravity_pending_calls
    if key not in pending and len(pending) >= 256:
        pending.pop(next(iter(pending)))
    calls = pending.setdefault(key, [])
    if len(calls) >= 64:
        del calls[0]
    calls.append(call)


def _poll_antigravity_record(
    root: Path, stream: NativeAgentStream, record: dict[str, Any]
) -> int:
    record_type = record.get("type")
    timing = _record_time(record)
    step_idx = record.get("step_index")
    if not isinstance(step_idx, int):
        step_idx = record.get("stepIdx")
    if not isinstance(step_idx, int):
        return 0

    if record_type == "USER_INPUT":
        if not _native_path_matches_root(root, "", stream.agent_root):
            return 0
        stream.turn_id = str(step_idx)
        source_id = f"antigravity:{stream.session_id}:step:{step_idx}:user_input"
        return int(
            append_event_once(
                root,
                {
                    **hook_context(_stream_context(stream)),
                    **timing,
                    "operation_id": source_id,
                    "group_id": source_id,
                    "kind": "session",
                    "status": "success",
                    "title": "Antigravity turn started",
                    "detail": "",
                    "source_event_id": source_id,
                },
            )
        )

    if record_type == "PLANNER_RESPONSE":
        tool_calls = record.get("tool_calls") or record.get("toolCalls")
        if not isinstance(tool_calls, list):
            return 0
        count = 0
        result_key = str(step_idx + 1)
        for call_idx, raw_call in enumerate(tool_calls):
            if not isinstance(raw_call, dict):
                continue
            tool_name = raw_call.get("name") or raw_call.get("toolName")
            if not isinstance(tool_name, str):
                continue
            args = _antigravity_call_args(raw_call)
            if tool_name == "manage_task":
                action = str(args.get("Action") or args.get("action") or "")
                task_id = str(args.get("TaskId") or args.get("taskId") or "").strip(
                    "\"'"
                )
                if action.strip('"').casefold() == "status" and task_id:
                    _remember_antigravity_call(
                        stream,
                        result_key,
                        {
                            "tool_name": "task_status",
                            "task_id": task_id,
                            "step_idx": step_idx,
                            "call_idx": call_idx,
                            "offset": stream.record_position,
                        },
                    )
                continue
            if tool_name not in {
                "run_command",
                "write_to_file",
                "replace_file_content",
                "invoke_subagent",
            }:
                continue
            call = {
                "tool_name": tool_name,
                "args": args,
                "step_idx": step_idx,
                "call_idx": call_idx,
                "offset": stream.record_position,
            }
            _remember_antigravity_call(stream, result_key, call)
            count += _append_antigravity_call_events(
                root, stream, call, "running", timing, "running"
            )
        return count

    if record_type != "GENERIC":
        return 0
    pending = stream.antigravity_pending_calls.pop(str(step_idx), [])
    if not pending:
        return 0
    count = 0
    result_index = 0
    command_results = _antigravity_command_results(record)
    for call in pending:
        if call.get("tool_name") == "task_status":
            task_result = (
                command_results[result_index]
                if result_index < len(command_results)
                else None
            )
            if task_result is not None:
                result_index += 1
            task_key = f"task:{call.get('task_id', '')}"
            task_calls = stream.antigravity_pending_calls.get(task_key, [])
            if not task_calls:
                continue
            result_status = (
                task_result[0]
                if task_result is not None
                else _antigravity_result_status(task_calls[0], record)
            )
            if result_status == "running":
                continue
            stream.antigravity_pending_calls.pop(task_key, None)
            for task_call in task_calls:
                count += _append_antigravity_call_events(
                    root, stream, task_call, result_status, timing, "complete"
                )
            continue
        command_result = (
            command_results[result_index]
            if call.get("tool_name") == "run_command"
            and result_index < len(command_results)
            else None
        )
        if call.get("tool_name") == "run_command" and command_result is not None:
            result_index += 1
        if command_result is not None:
            status, task_id = command_result
        elif call.get("tool_name") == "run_command" and command_results:
            status, task_id = "unknown", ""
        else:
            status, task_id = _antigravity_result_status(call, record), ""
        if status == "running":
            task_id = task_id or str(step_idx)
            _remember_antigravity_call(stream, f"task:{task_id}", call)
            continue
        count += _append_antigravity_call_events(
            root, stream, call, status, timing, "complete"
        )
    return count


def _native_session_path(agent: str, session_id: str) -> Path | None:
    if agent == "codex":
        return codex_session_path(session_id)
    if agent == "pi":
        return pi_session_path(session_id)
    if agent == "deepseek":
        return deepseek_session_path(session_id)
    if agent == "antigravity":
        return antigravity_session_path(session_id)
    return None


def sync_native_streams(
    root: Path,
    identities: dict[str, dict[str, str]],
    streams: dict[str, NativeAgentStream],
) -> dict[str, int]:
    """Attach a cursor to every supported native session in this root."""
    attached: dict[str, int] = {}
    desired: set[str] = set()
    for identity in identities.values():
        agent = normalize_agent(identity.get("agent"))
        if agent not in {"codex", "pi", "deepseek", "antigravity"}:
            continue
        session_id = identity.get("session_id")
        if not session_id:
            continue
        stream_key = agent_session_key(agent, session_id)
        desired.add(stream_key)
        legacy_stream = streams.get(session_id)
        if (
            stream_key not in streams
            and legacy_stream is not None
            and normalize_agent(legacy_stream.agent) == agent
        ):
            # Migrate in-memory cursors created before stream keys were scoped.
            del streams[session_id]
            streams[stream_key] = legacy_stream
        if stream_key in streams:
            streams[stream_key].agent_root = identity.get(
                "root", streams[stream_key].agent_root
            )
            streams[stream_key].model = identity.get(
                "model", streams[stream_key].model
            )
            streams[stream_key].effort = identity.get(
                "effort", streams[stream_key].effort
            )
            streams[stream_key].session_cwd = identity.get(
                "working_root", streams[stream_key].session_cwd
            )
            continue
        path = _native_session_path(agent, session_id)
        if path is None:
            continue
        position = load_native_stream_position(root, session_id, path, agent)
        stream = NativeAgentStream(
            session_id=session_id,
            path=path,
            position=position,
            agent=agent,
            agent_root=identity.get("root", ""),
            session_cwd=identity.get("working_root", ""),
            model=identity.get("model", ""),
            effort=identity.get("effort", ""),
        )
        # A Pi result carries only its call id and error flag; the arguments
        # live in the earlier call, which may sit before the saved cursor. Read
        # the transcript up to that cursor and rebuild the calls still awaiting
        # a result, so a restart between a call and its result still completes.
        if agent == "pi":
            _reconstruct_pi_stream(root, stream)
        elif agent == "deepseek":
            _rehydrate_deepseek_stream(stream)
        streams[stream_key] = stream
        attached[stream_key] = position
    if identities:
        for stream_key in set(streams) - desired:
            del streams[stream_key]
    return attached


# Backwards-compatible alias: the old name only knew Codex.
sync_codex_streams = sync_native_streams


def announce_native_history(
    root: Path, stream: NativeAgentStream, initial_position: int
) -> None:
    indexed = native_event_count(root, stream.session_id, stream.agent)
    buffered: list[tuple[Path, SafeEvent]] = getattr(
        _POLL_EVENT_BUFFER, "events", []
    )
    indexed += sum(
        event.agent == stream.agent and event.session_id == stream.session_id
        for event_root, event in buffered
        if event_root == canonical_root(root)
    )
    if indexed == 0:
        return
    backfilled = initial_position == 0
    event_word = "event" if indexed == 1 else "events"
    milestone_id = (
        f"{stream.agent}:{stream.session_id}:history-backfill-complete-v3"
    )
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
    *,
    save_checkpoints: bool = True,
    health: _PollHealth | None = None,
) -> int:
    """Ingest privacy-filtered Codex, Pi, DeepSeek, and Antigravity events."""
    attached = sync_native_streams(root, identities, streams)
    count = 0
    for stream in streams.values():
        stream_key = agent_session_key(stream.agent, stream.session_id)
        if stream.agent == "deepseek":
            records, stream.position = _dsh_records(
                stream.path, stream.position, health=health
            )
            for record in records:
                count += _poll_deepseek_record(root, stream, record)
            if save_checkpoints:
                save_native_stream_position(
                    root,
                    stream.session_id,
                    stream.path,
                    stream.position,
                    stream.agent,
                )
            if stream_key in attached:
                announce_native_history(root, stream, attached[stream_key])
            continue
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
                        if health is not None:
                            health.record(PollErrorCode.PARSE)
                        stream.position = handle.tell()
                        continue
                    if isinstance(record, dict):
                        stream.record_position = line_start
                        if stream.agent == "pi":
                            count += _poll_pi_record(root, stream, record)
                        elif stream.agent == "antigravity":
                            count += _poll_antigravity_record(root, stream, record)
                        else:
                            count += _poll_codex_record(root, stream, record)
                    stream.position = handle.tell()
        except OSError:
            if health is not None:
                health.record(PollErrorCode.IO)
            continue
        if save_checkpoints:
            save_native_stream_position(
                root,
                stream.session_id,
                stream.path,
                _native_checkpoint_position(stream),
                stream.agent,
            )
        if stream_key in attached:
            announce_native_history(root, stream, attached[stream_key])
    return count


def _native_checkpoint_position(stream: NativeAgentStream) -> int:
    replay_positions = [
        call.get("offset")
        for calls in stream.antigravity_pending_calls.values()
        for call in calls
        if isinstance(call.get("offset"), int)
    ]
    return (
        min(stream.position, *replay_positions)
        if replay_positions
        else stream.position
    )


def opencode_db_path() -> Path | None:
    """Where opencode keeps its single SQLite store, if it exists.

    Opencode writes every session into one database under the platform data
    directory. Honouring XDG_DATA_HOME lets a relocated install still be found;
    the default is ~/.local/share/opencode/opencode.db.
    """
    data_home = os.environ.get("XDG_DATA_HOME")
    if data_home and not Path(data_home).expanduser().is_absolute():
        return None
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    candidate = base / "opencode" / "opencode.db"
    return candidate if candidate.exists() else None


def _open_opencode_source(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2.0)


def _opencode_model_info(raw_model: Any, raw_agent: Any) -> tuple[str, str]:
    """Model id and reasoning variant out of opencode's JSON model column."""
    model_id = ""
    variant = ""
    if isinstance(raw_model, str) and raw_model:
        try:
            parsed = json.loads(raw_model)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            model_id = str(parsed.get("id") or parsed.get("modelID") or "")
            variant = str(parsed.get("variant") or "")
    if not model_id and isinstance(raw_agent, str):
        model_id = raw_agent
    return model_id, variant


def _read_opencode_sessions(
    db: Path,
    connection: sqlite3.Connection | None = None,
    *,
    health: _PollHealth | None = None,
) -> list[dict[str, Any]]:
    return _read_opencode_sessions_result(
        db, connection, health=health
    )[0]


def _read_opencode_sessions_result(
    db: Path,
    connection: sqlite3.Connection | None = None,
    *,
    health: _PollHealth | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    owns_connection = connection is None
    if connection is None:
        try:
            connection = _open_opencode_source(db)
        except (sqlite3.Error, ValueError):
            if health is not None:
                health.record(PollErrorCode.SQLITE)
            return [], False
    try:
        rows = connection.execute(
            "SELECT id, directory, title, model, agent, parent_id, "
            "time_updated FROM session"
        ).fetchall()
    except sqlite3.Error:
        if health is not None:
            health.record(PollErrorCode.SQLITE)
        return [], False
    finally:
        if owns_connection:
            connection.close()
    sessions: list[dict[str, Any]] = []
    for session_id, directory, title, model, agent, parent_id, time_updated in rows:
        if not isinstance(session_id, str) or not isinstance(directory, str):
            continue
        model_id, variant = _opencode_model_info(model, agent)
        sessions.append(
            {
                "id": session_id,
                "directory": directory,
                "title": str(title or "").strip(),
                "model": model_id,
                "effort": variant,
                "parent_id": parent_id,
                "time_updated": int(time_updated or 0),
            }
        )
    return sessions, True


def opencode_session_listing() -> list[dict[str, Any]]:
    """Every opencode session, walked at most once a tick.

    Opencode keeps its sessions in one SQLite database, so a listing is one
    query rather than a recursive walk. The result barely changes between
    polls, so it is shared the way Codex's file listing is.
    """
    db = opencode_db_path()
    if db is None:
        return []
    return _cached_opencode_session_listing(db)


def _cached_opencode_session_listing(
    db: Path,
    connection: sqlite3.Connection | None = None,
    *,
    health: _PollHealth | None = None,
) -> list[dict[str, Any]]:
    return _opencode_session_listing_for_poll(
        db, connection, health=health
    )[0]


def _opencode_session_listing_for_poll(
    db: Path,
    connection: sqlite3.Connection | None = None,
    *,
    health: _PollHealth | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    key = os.fspath(db)
    now = time.monotonic()
    cached = OPENCODE_LISTING_CACHE.get(key)
    if cached is not None and now - cached[0] < OPENCODE_LISTING_TTL_SECONDS:
        return cached[1], False
    listing, successful = _read_opencode_sessions_result(
        db, connection, health=health
    )
    if successful:
        OPENCODE_LISTING_CACHE[key] = (now, listing)
    return listing, successful


def _opencode_effective_updated(listing: list[dict[str, Any]]) -> dict[str, int]:
    """The newest write in each session's whole descendant tree, keyed by id.

    A subagent writes to its own session row rather than its parent's, so a
    long-running subagent would otherwise leave the parent looking idle and
    eventually finished. Folding each descendant's time into its ancestors keeps
    the parent working for as long as any subagent is.
    """
    records = {record["id"]: record for record in listing}
    children: dict[str, list[str]] = {}
    for record in listing:
        parent = record.get("parent_id")
        if parent:
            children.setdefault(parent, []).append(record["id"])

    memo: dict[str, int] = {}

    def newest(session_id: str) -> int:
        cached = memo.get(session_id)
        if cached is not None:
            return cached
        value = int(records[session_id]["time_updated"])
        for child in children.get(session_id, []):
            value = max(value, newest(child))
        memo[session_id] = value
        return value

    return {session_id: newest(session_id) for session_id in records}


def opencode_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Opencode agents working in this folder, read from its session store.

    Opencode has no separate registry: a session row is a session, and its
    newest descendant write says whether it is still working. Subagent sessions
    carry a parent id, so the parent alone names the agent.
    """
    moment = now if now is not None else time.time()
    watched_common = git_common_dir(os.fspath(root))
    listing = opencode_session_listing()
    effective = _opencode_effective_updated(listing)
    identities: dict[str, dict[str, str]] = {}
    for record in listing:
        if record.get("parent_id"):
            continue
        session_id = record["id"]
        try:
            session_root = canonical_root(record["directory"])
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
        age = moment - effective[session_id] / 1000
        if age > OPENCODE_SESSION_IDENTITY_WINDOW_SECONDS:
            continue
        identities[session_id] = {
            "agent": "opencode",
            "root": os.fspath(associated),
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "working_root": os.fspath(session_root),
            "status": (
                "working"
                if age <= OPENCODE_SESSION_WORKING_SECONDS
                else "idle"
            ),
            "label": record["title"] or agent_label("opencode"),
            "session_id": session_id,
            "model": record["model"],
            "effort": record["effort"],
        }
    return identities


def load_opencode_metadata(session_id: str) -> dict[str, str]:
    """Model and reasoning variant for an opencode session, from its store."""
    for record in opencode_session_listing():
        if record["id"] == session_id:
            return {
                "model": record["model"],
                "effort": record["effort"],
            }
    return {}


def clear_crush_listing_cache() -> None:
    global CRUSH_LISTING_CACHE
    with CRUSH_LISTING_LOCK:
        CRUSH_LISTING_CACHE = None


def _crush_session_snapshot(
    *, health: _PollHealth | None = None
) -> tuple[
    tuple[tuple[CrushProject, tuple[CrushSession, ...]], ...],
    int,
    tuple[CrushProject, ...],
    tuple[CrushProject, ...],
]:
    """One bounded machine-wide read shared by discovery and polling."""
    global CRUSH_LISTING_CACHE
    index = crush_projects_path()
    key = os.fspath(index) if index is not None else ""
    now = time.monotonic()
    with CRUSH_LISTING_LOCK:
        cached = CRUSH_LISTING_CACHE
        if (
            cached is not None
            and cached[0] == key
            and now - cached[1] < CRUSH_LISTING_TTL_SECONDS
        ):
            if health is not None:
                for error in cached[3]:
                    health.record(error)
            return cached[2], cached[4], cached[5], cached[6]
        # Advance a watch baseline only to the instant before this snapshot
        # begins. A session committed while the read is in flight, or while
        # this cache remains live, must still fall after that watermark.
        snapshot_epoch_ms = int(time.time()) * 1000
        listing: list[tuple[CrushProject, tuple[CrushSession, ...]]] = []
        errors: list[PollErrorCode] = []
        failed_projects: list[CrushProject] = []
        incomplete_projects: list[CrushProject] = []
        seen_databases: set[Path] = set()
        omitted_projects: tuple[CrushProject, ...] = ()
        if index is None or not index.exists():
            projects = ()
        elif not index.is_file():
            errors.append(PollErrorCode.IO)
            projects = ()
        else:
            try:
                projects, omitted_projects = read_crush_project_snapshot(
                    index, strict=True
                )
            except OSError:
                errors.append(PollErrorCode.IO)
                projects = ()
            except ValueError:
                errors.append(PollErrorCode.PARSE)
                projects = ()
        incomplete_projects.extend(omitted_projects)
        for project in projects:
            database = project.database
            if database in seen_databases:
                continue
            seen_databases.add(database)
            if not database.is_file():
                failed_projects.append(project)
                continue
            try:
                sessions, complete = read_crush_session_snapshot(database)
            except sqlite3.Error:
                errors.append(PollErrorCode.SQLITE)
                failed_projects.append(project)
                continue
            except OSError:
                errors.append(PollErrorCode.IO)
                failed_projects.append(project)
                continue
            except ValueError:
                errors.append(PollErrorCode.PARSE)
                failed_projects.append(project)
                continue
            if not complete:
                incomplete_projects.append(project)
            listing.append((project, sessions))
        result = tuple(listing)
        recorded_errors = tuple(errors)
        CRUSH_LISTING_CACHE = (
            key,
            now,
            result,
            recorded_errors,
            snapshot_epoch_ms,
            tuple(failed_projects),
            tuple(incomplete_projects),
        )
        if health is not None:
            for error in recorded_errors:
                health.record(error)
        return (
            result,
            snapshot_epoch_ms,
            tuple(failed_projects),
            tuple(incomplete_projects),
        )


def crush_session_listing(
    *, health: _PollHealth | None = None
) -> tuple[tuple[CrushProject, tuple[CrushSession, ...]], ...]:
    return _crush_session_snapshot(health=health)[0]


def _crush_top_session_id(session_id: str, records: Mapping[str, CrushSession]) -> str:
    current = session_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        record = records.get(current)
        if record is None or not record.parent_session_id:
            return current
        current = record.parent_session_id
    return session_id


def _crush_session_trees(
    sessions: tuple[CrushSession, ...],
) -> tuple[dict[str, CrushSession], dict[str, list[CrushSession]]]:
    records = {record.session_id: record for record in sessions}
    trees: dict[str, list[CrushSession]] = {}
    for record in sessions:
        top = _crush_top_session_id(record.session_id, records)
        trees.setdefault(top, []).append(record)
    return records, trees


def _crush_watched_root(
    project: CrushProject, roots: Iterable[Path]
) -> Path | None:
    matches = [
        root
        for root in roots
        if project.path == root or root in project.path.parents
    ]
    return max(matches, key=lambda root: len(root.parts), default=None)


def crush_identities(root: Path, now: float | None = None) -> dict[str, dict[str, Any]]:
    """Recent top-level Crush sessions belonging to one watched worktree."""
    moment = time.time() if now is None else now
    identities: dict[str, dict[str, Any]] = {}
    for project, sessions in crush_session_listing():
        if _crush_watched_root(project, (root,)) is None:
            continue
        associated = root
        _records, trees = _crush_session_trees(sessions)
        for top_id, members in trees.items():
            top = next(
                (record for record in members if record.session_id == top_id), None
            )
            if top is None:
                continue
            newest = max((record.updated_epoch_ms for record in members), default=0)
            age = moment - newest / 1000 if newest else float("inf")
            unfinished = any(not record.finished for record in members)
            if age > CRUSH_SESSION_IDENTITY_WINDOW_SECONDS:
                continue
            if unfinished:
                status = "working" if age <= CRUSH_SESSION_WORKING_SECONDS else "idle"
            else:
                status = "done"
            identities[top_id] = {
                "agent": "crush",
                "session_id": top_id,
                "root": os.fspath(associated),
                "working_root": os.fspath(project.path),
                "pane_id": "",
                "workspace_id": "",
                "tab_id": "",
                "status": status,
                "label": top.title or agent_label("crush"),
                "model": top.model,
                "effort": "",
                "inference_provider": top.provider,
            }
    return identities


def load_crush_metadata(session_id: str) -> dict[str, str]:
    for _project, sessions in crush_session_listing():
        for record in sessions:
            if record.session_id == session_id:
                return {
                    "model": record.model,
                    "inference_provider": record.provider,
                }
    return {}


def clear_t3code_listing_cache() -> None:
    global T3CODE_LISTING_CACHE
    with T3CODE_LISTING_LOCK:
        T3CODE_LISTING_CACHE = None


def t3code_session_listing() -> tuple[T3CodeSession, ...]:
    """One bounded machine-wide T3 Code projection read shared by all roots."""
    global T3CODE_LISTING_CACHE
    database = t3code_database_path()
    key = os.fspath(database)
    now = time.monotonic()
    with T3CODE_LISTING_LOCK:
        cached = T3CODE_LISTING_CACHE
        if (
            cached is not None
            and cached[0] == key
            and now - cached[1] < T3CODE_LISTING_TTL_SECONDS
        ):
            return cached[2]
        try:
            listing = tuple(read_t3code_sessions(database)) if database.is_file() else ()
        except (OSError, sqlite3.Error, ValueError):
            listing = ()
        T3CODE_LISTING_CACHE = (key, now, listing)
        return listing


def _t3code_recent_session(record: T3CodeSession, moment: float) -> bool:
    if record.updated_epoch_ms <= 0:
        return False
    return (
        moment - record.updated_epoch_ms / 1000
        <= T3CODE_SESSION_IDENTITY_WINDOW_SECONDS
    )


def _t3code_session_status(record: T3CodeSession, moment: float) -> str:
    if (
        record.status == "working"
        and moment - record.updated_epoch_ms / 1000
        > T3CODE_SESSION_WORKING_SECONDS
    ):
        return "idle"
    return record.status


def _t3code_record_root(record: T3CodeSession) -> Path | None:
    return worktree_root_for(record.working_root)


def _t3code_root_matches(record: T3CodeSession, root: Path) -> bool:
    associated = _t3code_record_root(record)
    return associated == root


def t3code_identities(
    root: Path, provider: str, now: float | None = None
) -> dict[str, dict[str, str]]:
    moment = time.time() if now is None else now
    identities: dict[str, dict[str, str]] = {}
    for record in t3code_session_listing():
        if record.provider != provider or not _t3code_recent_session(record, moment):
            continue
        if not _t3code_root_matches(record, root):
            continue
        associated = _t3code_record_root(record)
        if associated is None:
            continue
        session_id = record.native_session_id
        if not session_id:
            continue
        # The listing is newest-first. A provider may resume one native
        # session in several T3 threads, so the first matching thread wins.
        if session_id in identities:
            continue
        identities[session_id] = {
            "agent": provider,
            "session_id": session_id,
            "root": os.fspath(associated),
            "working_root": record.working_root,
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "status": _t3code_session_status(record, moment),
            "label": record.title or agent_label(provider),
            "model": record.model,
            "effort": record.effort,
            "surface": "T3 Code",
            "t3code_thread_id": record.thread_id,
        }
    return identities


def cursor_identities(root: Path, now: float | None = None) -> dict[str, dict[str, str]]:
    return t3code_identities(root, "cursor", now)


def grok_identities(root: Path, now: float | None = None) -> dict[str, dict[str, str]]:
    return t3code_identities(root, "grok", now)


def load_cursor_metadata(_session_id: str) -> dict[str, str]:
    """T3 metadata needs root and freshness checks done during enrichment."""
    return {}


def load_grok_metadata(_session_id: str) -> dict[str, str]:
    """T3 metadata needs root and freshness checks done during enrichment."""
    return {}


def _t3code_enrich_identity(
    identity: dict[str, Any], *, keep_label: bool = False, now: float | None = None
) -> dict[str, Any]:
    provider = normalize_agent(identity.get("agent"))
    session_id = str(identity.get("session_id") or "")
    if not session_id:
        return identity
    identity_root = worktree_root_for(
        str(identity.get("working_root") or identity.get("root") or "")
    )
    if identity_root is None:
        return identity
    moment = time.time() if now is None else now
    for record in t3code_session_listing():
        if record.provider != provider or record.native_session_id != session_id:
            continue
        associated = _t3code_record_root(record)
        if (
            associated is None
            or associated != identity_root
            or not _t3code_recent_session(record, moment)
        ):
            continue
        enriched = {
            **identity,
            "root": os.fspath(associated),
            "working_root": record.working_root or identity.get("working_root", ""),
            "label": record.title or identity.get("label", ""),
            "model": identity.get("model") or record.model,
            "effort": identity.get("effort") or record.effort,
            "surface": "T3 Code",
            "t3code_thread_id": record.thread_id,
        }
        if keep_label:
            enriched["label"] = identity.get("label", "")
        return enriched
    return identity


def _opencode_context(stream: OpenCodeStream) -> dict[str, str]:
    context = {
        "agent": "opencode",
        "session_id": stream.context_session_id or stream.session_id,
    }
    if stream.model:
        context["model"] = stream.model
    if stream.effort:
        context["effort"] = stream.effort
    return context


def _opencode_tool_status(state: dict[str, Any], tool: str) -> str | None:
    status = str(state.get("status") or "").casefold()
    if status == "running":
        return "running"
    if status == "error":
        return "failed"
    if status != "completed":
        return None
    if tool != "bash":
        return "success"
    exit_code = (
        state.get("metadata", {}).get("exit")
        if isinstance(state.get("metadata"), dict)
        else None
    )
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "success" if exit_code == 0 else "failed"
    return "unknown"


def _opencode_part_timing(data: dict[str, Any], time_updated: int) -> dict[str, Any]:
    instant = datetime.fromtimestamp(time_updated / 1000, timezone.utc)
    timing = {
        "timestamp": instant.isoformat(timespec="milliseconds"),
        "epoch_ms": time_updated,
    }
    state = data.get("state")
    started = (
        state.get("time", {}).get("start")
        if isinstance(state, dict) and isinstance(state.get("time"), dict)
        else None
    )
    if isinstance(started, int):
        timing["started_epoch_ms"] = started
    return timing


def _append_opencode_marker(
    root: Path,
    stream: OpenCodeStream,
    part_id: str,
    phase: str,
    timing: dict[str, Any],
    context: dict[str, str],
    *,
    kind: str,
    title: str,
    detail: str,
) -> int:
    """One lightweight, privacy-filtered event for a context-gathering tool."""
    return int(
        append_event_once(
            root,
            {
                **hook_context(context),
                **timing,
                "operation_id": f"opencode:{part_id}:{kind}",
                "group_id": f"opencode:{part_id}",
                "kind": kind,
                "status": "success",
                "title": title,
                "detail": detail,
                "source_event_id": (
                    f"opencode:{stream.session_id}:part:{part_id}:{phase}:{kind}"
                ),
            },
        )
    )


def _poll_opencode_part(
    root: Path,
    stream: OpenCodeStream,
    part_id: str,
    data: dict[str, Any],
    time_updated: int,
) -> int:
    context = _opencode_context(stream)
    timing = _opencode_part_timing(data, time_updated)

    if data.get("type") == "step-finish":
        # A step-finish with reason "stop" closes the turn; "tool-calls" just
        # means the agent is continuing to the next step.
        if str(data.get("reason") or "").casefold() == "stop":
            return int(
                append_event_once(
                    root,
                    {
                        **hook_context(context),
                        **timing,
                        "operation_id": f"opencode:{part_id}:turn",
                        "group_id": f"opencode:{part_id}",
                        "kind": "session",
                        "status": "success",
                        "title": "Opencode turn finished",
                        "detail": "",
                        "source_event_id": (
                            f"opencode:{stream.session_id}:part:{part_id}:turn"
                        ),
                    },
                )
            )
        return 0

    if data.get("type") != "tool":
        return 0
    tool = data.get("tool")
    state = data.get("state")
    if not isinstance(state, dict):
        return 0
    status = _opencode_tool_status(state, str(tool))
    if status is None:
        return 0
    tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
    phase = "running" if status == "running" else "complete"

    if tool == "bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command:
            return 0
        if not _native_path_matches_root(root, "", stream.agent_root):
            return 0
        workdir = tool_input.get("cwd")
        payload = {
            **context,
            "cwd": workdir if isinstance(workdir, str) else stream.agent_root,
            "tool_use_id": f"opencode:{part_id}",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
        return _append_native_tool_events(
            root,
            payload,
            status,
            f"opencode:{stream.session_id}:part:{part_id}:{phase}",
            timing,
        )

    if tool in {"edit", "write"}:
        raw_path = tool_input.get("filePath")
        if not isinstance(raw_path, str) or not raw_path:
            return 0
        if not _native_path_matches_root(root, raw_path, stream.agent_root):
            return 0
        path = relative_display(raw_path, root)
        config = is_config(path)
        if status == "running":
            title = "Writing config" if config else "Writing file"
        elif status == "failed":
            title = "Config write failed" if config else "File write failed"
        else:
            title = "Wrote config" if config else "Wrote file"
        counts = git_line_changes(root, path) if status == "success" else None
        return int(
            append_event_once(
                root,
                {
                    **hook_context(context),
                    **timing,
                    "operation_id": f"opencode:{part_id}:file",
                    "group_id": f"opencode:{part_id}",
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
                        f"opencode:{stream.session_id}:part:{part_id}:{phase}:file"
                    ),
                },
            )
        )

    if tool == "task":
        if not _native_path_matches_root(root, "", stream.agent_root):
            return 0
        detail = str(tool_input.get("subagent_type") or "subagent").strip()[:80]
        detail = " ".join(detail.split()) or "subagent"
        lifecycle = {
            "running": ("running", "Subagent started"),
            "success": ("success", "Subagent completed"),
            "failed": ("failed", "Subagent failed"),
        }
        status, title = lifecycle[status]
        return int(
            append_event_once(
                root,
                {
                    **hook_context(context),
                    **timing,
                    "operation_id": f"opencode:{part_id}:subagent",
                    "group_id": f"opencode:{part_id}",
                    "kind": "session",
                    "status": status,
                    "title": title,
                    "detail": detail,
                    "source_event_id": (
                        f"opencode:{stream.session_id}:part:{part_id}:{phase}:subagent"
                    ),
                },
            )
        )

    if status != "success":
        return 0
    if tool == "read":
        raw_path = tool_input.get("filePath")
        if not isinstance(raw_path, str) or not raw_path:
            return 0
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path(stream.agent_root or root) / path
        return _append_opencode_marker(
            root, stream, part_id, phase, timing, context,
            kind="search",
            title="Read file",
            detail=os.fspath(path),
        )
    if tool in {"grep", "glob"}:
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return 0
        title = "Searched code" if tool == "grep" else "Searched files"
        return _append_opencode_marker(
            root, stream, part_id, phase, timing, context,
            kind="search",
            title=title,
            detail="search",
        )
    if tool == "webfetch":
        url = tool_input.get("url")
        if not isinstance(url, str) or not url:
            return 0
        return _append_opencode_marker(
            root, stream, part_id, phase, timing, context,
            kind="search",
            title="Fetched web page",
            detail="web page",
        )
    if tool == "todowrite":
        todos = tool_input.get("todos")
        count = len(todos) if isinstance(todos, list) else 0
        detail = f"{count} task" + ("" if count == 1 else "s")
        return _append_opencode_marker(
            root, stream, part_id, phase, timing, context,
            kind="todo",
            title="Todo updated",
            detail=detail,
        )

    return 0


def sync_opencode_streams(
    root: Path,
    identities: dict[str, dict[str, str]],
    streams: dict[str, OpenCodeStream],
    baseline_ms: int,
    *,
    db: Path | None = None,
    listing: list[dict[str, Any]] | None = None,
    position_for: Callable[[str], int] | None = None,
) -> None:
    if db is None:
        db = opencode_db_path()
    if db is None:
        return
    desired: set[str] = set()
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for record in listing if listing is not None else opencode_session_listing():
        parent = record.get("parent_id")
        if parent:
            children_by_parent.setdefault(parent, []).append(record)

    def _refresh(stream: OpenCodeStream, identity: dict[str, str]) -> None:
        stream.agent_root = identity.get("root", stream.agent_root)
        stream.model = identity.get("model", stream.model)
        stream.effort = identity.get("effort", stream.effort)

    def _initial_position(session_id: str) -> int:
        return position_for(session_id) if position_for is not None else baseline_ms

    def _descendants(root_id: str) -> list[dict[str, Any]]:
        """Every session nested under root_id, direct and deeper."""
        result: list[dict[str, Any]] = []
        stack = list(children_by_parent.get(root_id, []))
        while stack:
            record = stack.pop()
            result.append(record)
            stack.extend(children_by_parent.get(record["id"], []))
        return result

    for identity in identities.values():
        if identity.get("agent") != "opencode":
            continue
        session_id = identity.get("session_id")
        if not session_id:
            continue
        desired.add(session_id)
        if session_id in streams:
            _refresh(streams[session_id], identity)
        else:
            # Start at the watcher's baseline, not this session's newest part. A
            # session discovered after start-up may have already written a quick
            # edit or command, and those rows still matter to a live pane. The
            # baseline is what keeps a 600MB store of old sessions from replaying.
            streams[session_id] = OpenCodeStream(
                session_id=session_id,
                db_path=db,
                position=_initial_position(session_id),
                agent_root=identity.get("root", ""),
                model=identity.get("model", ""),
                effort=identity.get("effort", ""),
            )
        # A subagent's session carries a parent id, so it has no banner of its
        # own, but its edits, tests and Git commands still belong in the pane.
        # Tail every descendant and attribute them all to the top-level parent.
        for child in _descendants(session_id):
            child_id = child["id"]
            desired.add(child_id)
            if child_id in streams:
                _refresh(streams[child_id], identity)
                continue
            streams[child_id] = OpenCodeStream(
                session_id=child_id,
                db_path=db,
                position=_initial_position(child_id),
                agent_root=identity.get("root", ""),
                model=identity.get("model", ""),
                effort=identity.get("effort", ""),
                context_session_id=session_id,
            )
    if identities:
        for session_id in set(streams) - desired:
            del streams[session_id]


def poll_opencode_events(
    root: Path,
    identities: dict[str, dict[str, str]],
    streams: dict[str, OpenCodeStream],
    baseline_ms: int | None = None,
    *,
    health: _PollHealth | None = None,
) -> int:
    """Ingest privacy-filtered native opencode events from its SQLite store."""
    if baseline_ms is None:
        baseline_ms = int(time.time() * 1000)
    sync_opencode_streams(root, identities, streams, baseline_ms)
    count = 0
    for stream in streams.values():
        try:
            connection = _open_opencode_source(stream.db_path)
        except (sqlite3.Error, ValueError):
            if health is not None:
                health.record(PollErrorCode.SQLITE)
            continue
        try:
            rows = connection.execute(
                "SELECT id, data, time_updated FROM part "
                "WHERE session_id = ? AND time_updated >= ? ORDER BY time_updated",
                (stream.session_id, stream.position),
            ).fetchall()
        except sqlite3.Error:
            if health is not None:
                health.record(PollErrorCode.SQLITE)
            continue
        finally:
            connection.close()
        count += _poll_opencode_rows(root, stream, rows, health=health)
    return count


def _opencode_cursor_signature(rows: list[tuple[str, Any]]) -> str:
    digest = hashlib.sha256()
    for part_id, raw_data in rows:
        digest.update(part_id.encode(errors="replace"))
        digest.update(b"\0")
        if isinstance(raw_data, str):
            digest.update(raw_data.encode(errors="replace"))
        elif isinstance(raw_data, bytes):
            digest.update(raw_data)
        else:
            digest.update(type(raw_data).__name__.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _poll_opencode_rows(
    root: Path,
    stream: OpenCodeStream,
    rows: Iterable[tuple[Any, Any, Any]],
    *,
    health: _PollHealth | None = None,
) -> int:
    grouped: dict[int, list[tuple[str, Any]]] = {}
    for part_id, raw_data, time_updated in rows:
        try:
            updated = int(time_updated or 0)
        except (TypeError, ValueError):
            if health is not None:
                health.record(PollErrorCode.PARSE)
            continue
        if updated >= stream.position:
            grouped.setdefault(updated, []).append((str(part_id), raw_data))
    count = 0
    for updated in sorted(grouped):
        timestamp_rows = sorted(grouped[updated], key=lambda row: row[0])
        signature = _opencode_cursor_signature(timestamp_rows)
        if updated == stream.position and signature == stream.cursor_signature:
            continue
        if updated > stream.position:
            stream.position = updated
            stream.processed.clear()
            stream.processed_order.clear()
        for part_id, raw_data in timestamp_rows:
            if not isinstance(raw_data, str):
                if health is not None:
                    health.record(PollErrorCode.PARSE)
                continue
            key = (part_id, hashlib.sha256(raw_data.encode()).hexdigest())
            if key in stream.processed:
                continue
            _remember_processed(stream, key)
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                if health is not None:
                    health.record(PollErrorCode.PARSE)
                continue
            if isinstance(data, dict):
                count += _poll_opencode_part(
                    root, stream, part_id, data, updated
                )
            elif health is not None:
                health.record(PollErrorCode.PARSE)
        stream.cursor_signature = signature
    return count


def cline_data_dir() -> Path:
    """Cline's shared data directory, honoring its documented overrides."""
    configured = os.environ.get("CLINE_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    configured_root = os.environ.get("CLINE_DIR")
    root = Path(configured_root).expanduser() if configured_root else Path.home() / ".cline"
    return root / "data"


def cline_sessions_root() -> Path:
    configured = os.environ.get("CLINE_SESSION_DATA_DIR")
    return Path(configured).expanduser() if configured else cline_data_dir() / "sessions"


def cline_db_path() -> Path | None:
    configured = os.environ.get("CLINE_DB_DATA_DIR")
    directory = Path(configured).expanduser() if configured else cline_data_dir() / "db"
    if not directory.is_absolute():
        return None
    candidate = directory / "sessions.db"
    return candidate if candidate.exists() else None


def cline_session_sources() -> tuple[Path, ...]:
    """Existing Cline stores, including the supported manifest-only mode."""
    sources: list[Path] = []
    database = cline_db_path()
    if database is not None:
        sources.append(database)
    sessions = cline_sessions_root()
    if sessions.exists():
        sources.append(sessions)
    return tuple(sources)


def _cline_epoch_ms(raw: Any) -> int:
    if not isinstance(raw, str) or not raw:
        return 0
    try:
        instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(instant.timestamp() * 1000)


def _cline_int(raw: Any) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _cline_title(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        metadata = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(metadata, dict):
        return ""
    title = metadata.get("title")
    return " ".join(title.split())[:120] if isinstance(title, str) else ""


def _cline_messages_path(session_id: str, raw_path: Any) -> Path:
    if isinstance(raw_path, str) and raw_path:
        return Path(raw_path).expanduser()
    return cline_sessions_root() / session_id / f"{session_id}.messages.json"


def _read_cline_sqlite_sessions(
    db: Path, *, health: _PollHealth | None = None
) -> list[dict[str, Any]]:
    try:
        connection = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=2.0)
    except (sqlite3.Error, ValueError):
        if health is not None:
            health.record(PollErrorCode.SQLITE)
        return []
    try:
        rows = connection.execute(
            "SELECT session_id, pid, status, cwd, workspace_root, model, "
            "metadata_json, messages_path, updated_at, started_at, "
            "parent_session_id, is_subagent FROM sessions"
        ).fetchall()
    except sqlite3.Error:
        if health is not None:
            health.record(PollErrorCode.SQLITE)
        return []
    finally:
        connection.close()
    sessions: list[dict[str, Any]] = []
    for row in rows:
        session_id = row[0]
        if not isinstance(session_id, str) or not session_id:
            continue
        messages = _cline_messages_path(session_id, row[7])
        changed = max(_cline_epoch_ms(row[8]), _cline_epoch_ms(row[9]))
        try:
            changed = max(changed, int(messages.stat().st_mtime * 1000))
        except OSError:
            pass
        sessions.append(
            {
                "id": session_id,
                "pid": _cline_int(row[1]),
                "status": str(row[2] or "unknown").casefold(),
                "directory": str(row[3] or row[4] or ""),
                "model": str(row[5] or ""),
                "title": _cline_title(row[6]),
                "messages_path": messages,
                "time_updated": changed,
                "parent_id": str(row[10] or ""),
                "is_subagent": bool(row[11]),
            }
        )
    return sessions


def _read_cline_manifest_sessions(
    *, health: _PollHealth | None = None
) -> list[dict[str, Any]]:
    """Fallback for Cline's file backend and stores not yet indexed by SQLite."""
    try:
        manifests = list(cline_sessions_root().glob("*/*.json"))
    except OSError:
        if health is not None:
            health.record(PollErrorCode.IO)
        return []
    sessions: list[dict[str, Any]] = []
    for path in manifests:
        if path.name.endswith((".messages.json", ".compaction.json")):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            if health is not None:
                health.record(PollErrorCode.IO)
            continue
        except (json.JSONDecodeError, UnicodeDecodeError):
            if health is not None:
                health.record(PollErrorCode.PARSE)
            continue
        if not isinstance(record, dict):
            if health is not None:
                health.record(PollErrorCode.PARSE)
            continue
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        metadata = record.get("metadata")
        title = metadata.get("title") if isinstance(metadata, dict) else ""
        messages = _cline_messages_path(session_id, record.get("messages_path"))
        changed = max(
            _cline_epoch_ms(record.get("updated_at")),
            _cline_epoch_ms(record.get("ended_at")),
            _cline_epoch_ms(record.get("started_at")),
        )
        try:
            changed = max(changed, int(messages.stat().st_mtime * 1000))
        except OSError:
            pass
        sessions.append(
            {
                "id": session_id,
                "pid": _cline_int(record.get("pid")),
                "status": str(record.get("status") or "unknown").casefold(),
                "directory": str(record.get("cwd") or record.get("workspace_root") or ""),
                "model": str(record.get("model") or ""),
                "title": (
                    " ".join(title.split())[:120] if isinstance(title, str) else ""
                ),
                "messages_path": messages,
                "time_updated": changed,
                "parent_id": str(record.get("parent_session_id") or ""),
                "is_subagent": bool(record.get("is_subagent")),
            }
        )
    return sessions


def cline_session_listing(
    *, health: _PollHealth | None = None
) -> list[dict[str, Any]]:
    """Every Cline session, queried or walked at most once per display tick."""
    db = cline_db_path()
    key = os.fspath(db) if db is not None else os.fspath(cline_sessions_root())
    now = time.monotonic()
    cached = CLINE_LISTING_CACHE.get(key)
    if cached is not None and now - cached[0] < CLINE_LISTING_TTL_SECONDS:
        return cached[1]
    sqlite_listing = (
        _read_cline_sqlite_sessions(db, health=health) if db is not None else []
    )
    manifest_listing = _read_cline_manifest_sessions(health=health)
    merged = {record["id"]: record for record in sqlite_listing}
    for record in manifest_listing:
        existing = merged.get(record["id"])
        if existing is None:
            merged[record["id"]] = record
            continue
        if record.get("time_updated", 0) >= existing.get("time_updated", 0):
            newer = {**existing, **record}
            # Session ancestry does not change. Preserve the richer SQLite
            # fields when an older manifest schema does not carry them.
            newer["parent_id"] = record.get("parent_id") or existing.get(
                "parent_id", ""
            )
            newer["is_subagent"] = bool(
                record.get("is_subagent") or existing.get("is_subagent")
            )
            merged[record["id"]] = newer
    listing = list(merged.values())
    CLINE_LISTING_CACHE[key] = (now, listing)
    return listing


def _cline_effective_updated(listing: list[dict[str, Any]]) -> dict[str, int]:
    records = {record["id"]: record for record in listing}
    children: dict[str, list[str]] = {}
    for record in listing:
        parent = record.get("parent_id")
        if parent in records:
            children.setdefault(parent, []).append(record["id"])
    memo: dict[str, int] = {}

    def newest(session_id: str, visiting: set[str] | None = None) -> int:
        if session_id in memo:
            return memo[session_id]
        trail = set() if visiting is None else set(visiting)
        if session_id in trail:
            return int(records[session_id].get("time_updated") or 0)
        trail.add(session_id)
        value = int(records[session_id].get("time_updated") or 0)
        for child in children.get(session_id, []):
            value = max(value, newest(child, trail))
        memo[session_id] = value
        return value

    return {session_id: newest(session_id) for session_id in records}


def cline_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Cline agents in this repository, from Cline's shared native store."""
    moment = now if now is not None else time.time()
    watched_common = git_common_dir(os.fspath(root))
    listing = cline_session_listing()
    effective = _cline_effective_updated(listing)
    identities: dict[str, dict[str, str]] = {}
    for record in listing:
        if record.get("parent_id") or record.get("is_subagent"):
            continue
        raw_directory = record.get("directory")
        if not isinstance(raw_directory, str) or not raw_directory:
            continue
        try:
            session_root = canonical_root(raw_directory)
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
        session_id = record["id"]
        age = moment - effective[session_id] / 1000
        active = (
            record.get("status") in CLINE_NON_TERMINAL_STATUSES
            and process_is_alive(record.get("pid"))
        )
        if age > CLINE_SESSION_IDENTITY_WINDOW_SECONDS and not active:
            continue
        title = record.get("title")
        identities[session_id] = {
            "agent": "cline",
            "root": os.fspath(associated),
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "working_root": os.fspath(session_root),
            "status": (
                "working"
                if active and age <= CLINE_SESSION_WORKING_SECONDS
                else "idle"
            ),
            "label": str(title or agent_label("cline")),
            "session_id": session_id,
            "model": str(record.get("model") or ""),
        }
    return identities


def load_cline_metadata(session_id: str) -> dict[str, str]:
    for record in cline_session_listing():
        if record["id"] == session_id:
            return {"model": str(record.get("model") or "")}
    return {}


def _cline_context(stream: ClineStream) -> dict[str, str]:
    context = {
        "agent": "cline",
        "session_id": stream.context_session_id or stream.session_id,
    }
    if stream.model:
        context["model"] = stream.model
    if stream.effort:
        context["effort"] = stream.effort
    if stream.turn_id:
        context["prompt_id"] = stream.turn_id
    return context


def _cline_commands(tool_input: Any) -> list[str]:
    raw = tool_input
    if isinstance(raw, dict):
        command = raw.get("command")
        args = raw.get("args")
        if isinstance(command, str) and (
            args is None
            or isinstance(args, list) and all(isinstance(arg, str) for arg in args)
        ):
            return [shlex.join([command, *(args or [])])]
        raw = raw.get("commands", raw.get("cmd"))
        if isinstance(raw, dict):
            return _cline_commands(raw)
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list):
        return []
    commands: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            commands.append(entry)
        elif isinstance(entry, dict):
            commands.extend(_cline_commands(entry))
    return commands


def _cline_patch_paths(tool_input: Any) -> list[tuple[str, str]]:
    raw = tool_input.get("input") if isinstance(tool_input, dict) else tool_input
    if not isinstance(raw, str):
        return []
    matches = re.findall(
        r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$", raw, re.MULTILINE
    )
    return [(action.casefold(), path.strip()) for action, path in matches if path.strip()]


def _cline_result_success_values(value: Any) -> list[bool]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return _cline_result_success_values(decoded)
    if isinstance(value, list):
        return [result for item in value for result in _cline_result_success_values(item)]
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("success"), bool):
        return [value["success"]]
    if "results" in value:
        return _cline_result_success_values(value["results"])
    if "content" in value:
        return _cline_result_success_values(value["content"])
    if value.get("type") == "text" and "text" in value:
        return _cline_result_success_values(value["text"])
    return []


def _cline_result_status(block: dict[str, Any]) -> str:
    if block.get("is_error") is True:
        return "failed"
    success = _cline_result_success_values(block.get("content"))
    if any(value is False for value in success):
        return "failed"
    return "success"


def _cline_command_result_statuses(
    block: dict[str, Any], command_count: int
) -> list[str]:
    if block.get("is_error") is True:
        return ["failed"] * command_count
    values = _cline_result_success_values(block.get("content"))
    if len(values) != command_count:
        return []
    return ["success" if value else "failed" for value in values]


def _append_cline_file_event(
    root: Path,
    stream: ClineStream,
    call_id: str,
    raw_path: str,
    status: str,
    phase: str,
    timing: dict[str, Any],
    *,
    index: int = 0,
    action: str = "update",
) -> int:
    if not _native_path_matches_root(root, raw_path, stream.agent_root):
        return 0
    display_path = raw_path
    if stream.agent_root and not Path(raw_path).expanduser().is_absolute():
        display_path = os.fspath(Path(stream.agent_root).expanduser() / raw_path)
    path = relative_display(display_path, root)
    config = is_config(path)
    if status == "running":
        title = "Writing config" if config else "Writing file"
    elif status == "failed":
        title = "Config write failed" if config else "File write failed"
    elif action == "delete":
        title = "Removed config" if config else "Removed file"
    else:
        title = "Wrote config" if config else "Wrote file"
    counts = git_line_changes(root, path) if status == "success" else None
    identifier = f"cline:{call_id}:{index}:file"
    return int(
        append_event_once(
            root,
            {
                **hook_context(_cline_context(stream)),
                **timing,
                "operation_id": identifier,
                "group_id": f"cline:{call_id}",
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
                    f"cline:{stream.session_id}:tool:{call_id}:{phase}:{index}:file"
                ),
            },
        )
    )


def _append_cline_tool_events(
    root: Path,
    stream: ClineStream,
    call_id: str,
    tool_name: str,
    tool_input: Any,
    status: str,
    phase: str,
    timing: dict[str, Any],
    command_statuses: list[str] | None = None,
) -> int:
    normalized_name = tool_name.casefold().replace("-", "_")
    if normalized_name in {"run_commands", "bash", "execute_command"}:
        if not _native_path_matches_root(root, "", stream.agent_root):
            return 0
        count = 0
        for index, command in enumerate(_cline_commands(tool_input)):
            command_status = (
                command_statuses[index]
                if command_statuses is not None and index < len(command_statuses)
                else status
            )
            payload = {
                **_cline_context(stream),
                "cwd": stream.agent_root,
                "tool_use_id": f"cline:{call_id}:{index}",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            }
            count += _append_native_tool_events(
                root,
                payload,
                command_status,
                f"cline:{stream.session_id}:tool:{call_id}:{phase}:{index}",
                timing,
            )
        return count
    if normalized_name in {"editor", "write_to_file", "replace_in_file", "write", "edit"}:
        raw_path = tool_input.get("path") if isinstance(tool_input, dict) else None
        return (
            _append_cline_file_event(
                root, stream, call_id, raw_path, status, phase, timing
            )
            if isinstance(raw_path, str) and raw_path
            else 0
        )
    if normalized_name == "apply_patch":
        count = 0
        for index, (action, raw_path) in enumerate(_cline_patch_paths(tool_input)):
            count += _append_cline_file_event(
                root,
                stream,
                call_id,
                raw_path,
                status,
                phase,
                timing,
                index=index,
                action=action,
            )
        return count
    if normalized_name == "spawn_agent" or normalized_name.startswith("subagent_"):
        if not _native_path_matches_root(root, "", stream.agent_root):
            return 0
        lifecycle = {
            "running": ("running", "Subagent started"),
            "success": ("success", "Subagent completed"),
            "failed": ("failed", "Subagent failed"),
        }
        event_status, title = lifecycle[status]
        identifier = f"cline:{call_id}:subagent"
        return int(
            append_event_once(
                root,
                {
                    **hook_context(_cline_context(stream)),
                    **timing,
                    "operation_id": identifier,
                    "group_id": identifier,
                    "kind": "session",
                    "status": event_status,
                    "title": title,
                    "detail": "subagent",
                    "source_event_id": (
                        f"cline:{stream.session_id}:tool:{call_id}:{phase}:subagent"
                    ),
                },
            )
        )
    return 0


def _cline_messages_signature(messages: list[Any], end: int | None = None) -> str:
    digest = hashlib.sha256()
    for message in messages[:end]:
        digest.update(
            json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
        )
        digest.update(b"\0")
    return digest.hexdigest()


def _poll_cline_messages(
    root: Path,
    stream: ClineStream,
    document: dict[str, Any],
    *,
    health: _PollHealth | None = None,
) -> int:
    messages = document.get("messages")
    if not isinstance(messages, list):
        if health is not None:
            health.record(PollErrorCode.PARSE)
        return 0
    prefix_unchanged = (
        stream.message_count <= len(messages)
        and bool(stream.message_prefix_signature)
        and _cline_messages_signature(messages, stream.message_count)
        == stream.message_prefix_signature
    )
    emit_start = stream.message_count if prefix_unchanged else 0
    if not prefix_unchanged:
        stream.processed.clear()
        stream.processed_order.clear()
    calls: dict[str, tuple[str, Any, dict[str, Any]]] = {}
    count = 0
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            if health is not None:
                health.record(PollErrorCode.PARSE)
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        user_turn = message.get("role") == "user" and (
            isinstance(content, str)
            or any(
                isinstance(block, dict) and block.get("type") != "tool_result"
                for block in blocks
            )
        )
        if user_turn:
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                stream.turn_id = message_id
        model_info = message.get("modelInfo")
        if isinstance(model_info, dict) and isinstance(model_info.get("id"), str):
            stream.model = model_info["id"]
        timing = _record_time(message, "ts")
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            call_id = block.get("id") if block_type == "tool_use" else block.get("tool_use_id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if block_type == "tool_use":
                tool_name = block.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                calls[call_id] = (tool_name, block.get("input"), timing)
                if message_index < emit_start:
                    continue
                key = (call_id, "running")
                if key in stream.processed:
                    continue
                count += _append_cline_tool_events(
                    root,
                    stream,
                    call_id,
                    tool_name,
                    block.get("input"),
                    "running",
                    "running",
                    timing,
                )
                _remember_processed(stream, key)
            elif block_type == "tool_result":
                pending = calls.get(call_id)
                if pending is None:
                    continue
                if message_index < emit_start:
                    continue
                key = (call_id, "complete")
                if key in stream.processed:
                    continue
                tool_name, tool_input, started_timing = pending
                completed_timing = dict(timing)
                started = started_timing.get("epoch_ms")
                if isinstance(started, int):
                    completed_timing["started_epoch_ms"] = started
                count += _append_cline_tool_events(
                    root,
                    stream,
                    call_id,
                    tool_name,
                    tool_input,
                    _cline_result_status(block),
                    "complete",
                    completed_timing,
                    _cline_command_result_statuses(
                        block, len(_cline_commands(tool_input))
                    ),
                )
                _remember_processed(stream, key)
    stream.message_count = len(messages)
    stream.message_prefix_signature = _cline_messages_signature(messages)
    return count


def sync_cline_streams(
    identities: dict[str, dict[str, str]],
    streams: dict[str, ClineStream],
    *,
    listing: list[dict[str, Any]] | None = None,
    health: _PollHealth | None = None,
) -> None:
    if listing is None:
        listing = cline_session_listing(health=health)
    desired: set[str] = set()
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for record in listing:
        parent = record.get("parent_id")
        if parent:
            children_by_parent.setdefault(parent, []).append(record)

    def descendants(root_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        stack = list(children_by_parent.get(root_id, []))
        seen: set[str] = set()
        while stack:
            record = stack.pop()
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            result.append(record)
            stack.extend(children_by_parent.get(record["id"], []))
        return result

    records = {record["id"]: record for record in listing}

    def lineage_root(session_id: str) -> str:
        current = session_id
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            parent = records[current].get("parent_id")
            if not isinstance(parent, str) or parent not in records:
                return current
            current = parent
        return session_id

    for identity in identities.values():
        if identity.get("agent") != "cline":
            continue
        observed_session_id = identity.get("session_id")
        if not observed_session_id or observed_session_id not in records:
            continue
        session_id = lineage_root(observed_session_id)
        if observed_session_id == session_id:
            record = records[session_id]
            candidates = [
                (record, ""),
                *((child, session_id) for child in descendants(session_id)),
            ]
        else:
            candidates = [(records[observed_session_id], session_id)]
        for candidate, context_session_id in candidates:
            candidate_id = candidate["id"]
            desired.add(candidate_id)
            candidate_root = candidate.get("directory")
            if not isinstance(candidate_root, str) or not candidate_root:
                candidate_root = (
                    identity.get("working_root", identity.get("root", ""))
                    if observed_session_id == session_id
                    or candidate_id == observed_session_id
                    else ""
                )
            stream = streams.get(candidate_id)
            if stream is None:
                path = candidate.get("messages_path")
                if not isinstance(path, Path):
                    continue
                stream = ClineStream(
                    session_id=candidate_id,
                    path=path,
                    agent_root=candidate_root,
                    model=str(candidate.get("model") or identity.get("model", "")),
                    context_session_id=context_session_id,
                )
                streams[candidate_id] = stream
            else:
                stream.agent_root = candidate_root or stream.agent_root
                stream.model = str(candidate.get("model") or identity.get("model", stream.model))
                stream.context_session_id = context_session_id
    if identities:
        for session_id in set(streams) - desired:
            del streams[session_id]


def poll_cline_events(
    root: Path,
    identities: dict[str, dict[str, str]],
    streams: dict[str, ClineStream],
) -> int:
    """Ingest Cline tool metadata without retaining prompts, output, or diffs."""
    sync_cline_streams(identities, streams)
    count = 0
    for stream in streams.values():
        try:
            stat = stream.path.stat()
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            if stream.fingerprint == fingerprint:
                continue
            document = _read_cline_document(stream.path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(document, dict):
            continue
        count += _poll_cline_messages(root, stream, document)
        stream.fingerprint = fingerprint
    return count


def _read_cline_document(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


NATIVE_POLL_PROVIDERS = frozenset({"codex", "pi", "deepseek", "antigravity"})


def _routed_provider_identities(
    targets: tuple[PollTarget, ...], provider: str
) -> tuple[tuple[PollTarget, dict[str, dict[str, Any]]], ...]:
    """Assign each machine-wide session to one canonical watched root."""
    by_root = {target.root: {} for target in targets}
    candidates: dict[str, list[tuple[int, PollTarget, AgentIdentity]]] = {}
    for index, target in enumerate(targets):
        for identity in target.for_provider(provider):
            key = identity.key.to_wire()
            candidates.setdefault(key, []).append((index, target, identity))
    for key, choices in candidates.items():

        def score(
            choice: tuple[int, PollTarget, AgentIdentity],
        ) -> tuple[int, int, int]:
            index, target, identity = choice
            reported = identity.working_root or identity.root
            try:
                location = canonical_root(reported) if reported else None
                matches = location is not None and (
                    location == target.root or location.is_relative_to(target.root)
                )
                exact = location == target.root
            except (OSError, ValueError):
                matches = exact = False
            return (
                int(exact) + int(matches),
                len(target.root.parts) if matches else 0,
                -index,
            )

        _index, owner, identity = max(choices, key=score)
        by_root[owner.root][key] = identity.to_wire()
    return tuple((target, by_root[target.root]) for target in targets)


class CodingAgentPollAdapter:
    """Cycle-safe adapter from typed polling targets to existing collectors."""

    def __init__(
        self,
        provider: str,
        *,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.provider = provider
        self._checkpoint_store = checkpoint_store
        self._native: dict[Path, dict[str, NativeAgentStream]] = {}
        self._opencode: dict[Path, dict[str, OpenCodeStream]] = {}
        self._cline: dict[Path, dict[str, ClineStream]] = {}
        self._opencode_baselines: dict[Path, int] = {}

    def poll(self, targets: tuple[PollTarget, ...]) -> PollBatch:
        events: list[tuple[Path, SafeEvent]] = []
        _POLL_EVENT_BUFFER.events = events
        try:
            return self._poll(targets, events)
        finally:
            del _POLL_EVENT_BUFFER.events

    def _poll(
        self,
        targets: tuple[PollTarget, ...],
        events: list[tuple[Path, SafeEvent]],
    ) -> PollBatch:
        started = time.monotonic()
        health = _PollHealth()
        active_roots = {target.root for target in targets}
        for mapping in (self._native, self._opencode, self._cline):
            for root in set(mapping) - active_roots:
                del mapping[root]
        for root in set(self._opencode_baselines) - active_roots:
            del self._opencode_baselines[root]

        routed = _routed_provider_identities(targets, self.provider)
        checkpoints: list[tuple[Path, StreamCheckpoint]] = []
        if self.provider in NATIVE_POLL_PROVIDERS:
            checkpoints.extend(self._poll_native(routed, health))
        elif self.provider == "opencode":
            checkpoints.extend(self._poll_opencode(routed, health))
        elif self.provider == "cline":
            self._poll_cline(routed, health)
        duration_ms = int((time.monotonic() - started) * 1000)
        return PollBatch(
            PollStats(
                self.provider,
                duration_ms=duration_ms,
                parse_errors=health.parse_errors,
                last_error=health.last_error,
            ),
            events=tuple(events),
            checkpoints=tuple(checkpoints),
        )

    def _poll_native(
        self,
        routed: tuple[tuple[PollTarget, dict[str, dict[str, Any]]], ...],
        health: _PollHealth,
    ) -> list[tuple[Path, StreamCheckpoint]]:
        checkpoints: list[tuple[Path, StreamCheckpoint]] = []
        for target, identities in routed:
            if not identities:
                self._native.pop(target.root, None)
                continue
            streams = self._native.setdefault(target.root, {})
            poll_native_agent_events(
                target.root,
                identities,
                streams,
                save_checkpoints=False,
                health=health,
            )
            checkpoints.extend(
                (
                    target.root,
                    StreamCheckpoint(
                        session=SessionKey(stream.agent, stream.session_id),
                        source=os.fspath(stream.path),
                        position=_native_checkpoint_position(stream),
                    ),
                )
                for stream in streams.values()
            )
        return checkpoints

    def _poll_opencode(
        self,
        routed: tuple[tuple[PollTarget, dict[str, dict[str, Any]]], ...],
        health: _PollHealth,
    ) -> list[tuple[Path, StreamCheckpoint]]:
        root_baselines: dict[Path, int] = {}
        root_boundaries: dict[Path, int] = {}
        for target, identities in routed:
            proposed = self._opencode_baselines.setdefault(
                target.root, int(time.time() * 1000)
            )
            baseline = self._opencode_root_baseline(
                target.root, proposed, health
            )
            root_baselines[target.root] = baseline
            # An empty identity set commonly means discovery is still catching
            # up. Seed its durable watch baseline, but do not advance past it
            # until this adapter can actually poll a known session for the root.
            if identities:
                root_boundaries[target.root] = max(
                    baseline, int(time.time() * 1000)
                )
        db = opencode_db_path()
        if db is None:
            return []
        try:
            connection = _open_opencode_source(db)
        except (sqlite3.Error, ValueError):
            health.record(PollErrorCode.SQLITE)
            return []
        listing, fresh_listing = _opencode_session_listing_for_poll(
            db, connection, health=health
        )
        owners: dict[str, tuple[Path, OpenCodeStream]] = {}
        for target, identities in routed:
            if not identities:
                self._opencode.pop(target.root, None)
                continue
            baseline = root_baselines[target.root]
            streams = self._opencode.setdefault(target.root, {})
            sync_opencode_streams(
                target.root,
                identities,
                streams,
                baseline,
                db=db,
                listing=listing,
                position_for=lambda session_id, root=target.root, start=baseline: (
                    self._opencode_resume_position(
                        root, session_id, start, health
                    )
                ),
            )
            for stream in streams.values():
                owners.setdefault(stream.session_id, (target.root, stream))
        if fresh_listing and root_boundaries:
            watched_roots = tuple(root_baselines)
            watched_common = {
                root: git_common_dir(os.fspath(root)) for root in watched_roots
            }
            for record in listing:
                session_id = record.get("id")
                directory = record.get("directory")
                if not isinstance(session_id, str) or not isinstance(directory, str):
                    continue
                try:
                    session_root = canonical_root(directory)
                    session_common = git_common_dir(os.fspath(session_root))
                except (OSError, ValueError):
                    continue
                candidates: list[tuple[tuple[int, int, int], Path]] = []
                for index, root in enumerate(watched_roots):
                    path_matches = (
                        session_root == root or session_root.is_relative_to(root)
                    )
                    same_repository = (
                        bool(watched_common[root])
                        and session_common == watched_common[root]
                    )
                    if not path_matches and not same_repository:
                        continue
                    candidates.append(
                        (
                            (
                                int(session_root == root) + int(path_matches),
                                len(root.parts) if path_matches else 0,
                                -index,
                            ),
                            root,
                        )
                    )
                if not candidates:
                    continue
                _score, root = max(candidates)
                if int(record.get("time_updated") or 0) < root_baselines[root]:
                    continue
                owner = owners.get(session_id)
                if owner is None or owner[0] != root:
                    # The machine-wide listing can lead the asynchronous identity
                    # refresh. Keep the old discovery baseline until every recent
                    # session visible for this root is actually being polled.
                    root_boundaries.pop(root, None)
        if not owners:
            connection.close()
            return (
                self._opencode_root_checkpoints(root_boundaries)
                if fresh_listing
                else []
            )
        placeholders = ",".join("?" for _ in owners)
        minimum_position = min(stream.position for _root, stream in owners.values())
        try:
            rows = connection.execute(
                "SELECT session_id, id, data, time_updated FROM part "
                f"WHERE session_id IN ({placeholders}) AND time_updated >= ? "
                "ORDER BY time_updated, id",
                (*owners, minimum_position),
            ).fetchall()
        except sqlite3.Error:
            health.record(PollErrorCode.SQLITE)
            return []
        finally:
            connection.close()
        rows_by_session: dict[str, list[tuple[Any, Any, Any]]] = {}
        for session_id, part_id, raw_data, time_updated in rows:
            owner = owners.get(str(session_id))
            if owner is None:
                continue
            rows_by_session.setdefault(str(session_id), []).append(
                (part_id, raw_data, time_updated)
            )
        for session_id, session_rows in rows_by_session.items():
            root, stream = owners[session_id]
            _poll_opencode_rows(root, stream, session_rows, health=health)
        checkpoints = [
            (
                root,
                StreamCheckpoint(
                    session=SessionKey("opencode", stream.session_id),
                    source=OPENCODE_CHECKPOINT_SOURCE,
                    position=stream.position,
                ),
            )
            for root, stream in owners.values()
        ]
        if fresh_listing:
            checkpoints.extend(
                self._opencode_root_checkpoints(root_boundaries)
            )
        return checkpoints

    @staticmethod
    def _opencode_root_checkpoints(
        boundaries: dict[Path, int],
    ) -> list[tuple[Path, StreamCheckpoint]]:
        return [
            (
                root,
                StreamCheckpoint(
                    session=SessionKey(
                        "opencode", OPENCODE_ROOT_CHECKPOINT_SESSION
                    ),
                    source=OPENCODE_ROOT_CHECKPOINT_SOURCE,
                    position=boundary,
                ),
            )
            for root, boundary in boundaries.items()
        ]

    def _opencode_root_baseline(
        self,
        root: Path,
        proposed: int,
        health: _PollHealth,
    ) -> int:
        if self._checkpoint_store is None:
            return proposed
        session = SessionKey("opencode", OPENCODE_ROOT_CHECKPOINT_SESSION)
        try:
            checkpoint = self._checkpoint_store.load(
                root, session, OPENCODE_ROOT_CHECKPOINT_SOURCE
            )
            if checkpoint is not None:
                return checkpoint.position
            self._checkpoint_store.save(
                root,
                StreamCheckpoint(
                    session=session,
                    source=OPENCODE_ROOT_CHECKPOINT_SOURCE,
                    position=proposed,
                ),
            )
        except sqlite3.Error:
            health.record(PollErrorCode.SQLITE)
            return proposed
        except OSError:
            health.record(PollErrorCode.IO)
            return proposed
        except Exception:
            health.record(PollErrorCode.UNKNOWN)
            return proposed
        return proposed

    def _opencode_resume_position(
        self,
        root: Path,
        session_id: str,
        baseline: int,
        health: _PollHealth,
    ) -> int:
        if self._checkpoint_store is None:
            return baseline
        session = SessionKey("opencode", session_id)
        try:
            checkpoint = self._checkpoint_store.load(
                root, session, OPENCODE_CHECKPOINT_SOURCE
            )
        except sqlite3.Error:
            health.record(PollErrorCode.SQLITE)
            return baseline
        except OSError:
            health.record(PollErrorCode.IO)
            return baseline
        except Exception:
            health.record(PollErrorCode.UNKNOWN)
            return baseline
        if checkpoint is not None:
            return checkpoint.position
        return baseline

    def _poll_cline(
        self,
        routed: tuple[tuple[PollTarget, dict[str, dict[str, Any]]], ...],
        health: _PollHealth,
    ) -> None:
        listing = cline_session_listing(health=health)
        owners: dict[Path, list[tuple[Path, ClineStream]]] = {}
        for target, identities in routed:
            if not identities:
                self._cline.pop(target.root, None)
                continue
            streams = self._cline.setdefault(target.root, {})
            sync_cline_streams(identities, streams, listing=listing)
            for stream in streams.values():
                owners.setdefault(stream.path.resolve(strict=False), []).append(
                    (target.root, stream)
                )
        for path, stream_owners in owners.items():
            try:
                stat = path.stat()
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                pending = [
                    owner
                    for owner in stream_owners
                    if owner[1].fingerprint != fingerprint
                ]
                if not pending:
                    continue
                document = _read_cline_document(path)
            except OSError:
                health.record(PollErrorCode.IO)
                continue
            except (json.JSONDecodeError, UnicodeDecodeError):
                health.record(PollErrorCode.PARSE)
                continue
            if not isinstance(document, dict):
                health.record(PollErrorCode.PARSE)
                continue
            for root, stream in pending:
                _poll_cline_messages(root, stream, document, health=health)
                stream.fingerprint = fingerprint

    def close(self) -> None:
        self._native.clear()
        self._opencode.clear()
        self._cline.clear()
        self._opencode_baselines.clear()


def _crush_timing(epoch_ms: int) -> dict[str, Any]:
    return _record_time({"epoch_ms": epoch_ms}, "epoch_ms")


def _crush_context(identity: AgentIdentity, message_id: str) -> dict[str, Any]:
    context: dict[str, Any] = {
        "agent": "crush",
        "session_id": identity.session_id,
        "prompt_id": message_id,
    }
    if identity.model:
        context["model"] = identity.model
    return context


def _crush_absolute_path(raw_path: str, project: CrushProject) -> str | None:
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = project.path / path
        return os.fspath(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return None


def _crush_checkpoint_source(project: CrushProject) -> str:
    digest = hashlib.sha256(os.fsencode(project.database)).hexdigest()[:16]
    return f"{CRUSH_CHECKPOINT_SOURCE}:{digest}"


def _append_crush_marker(
    root: Path,
    identity: AgentIdentity,
    call: CrushToolLifecycle,
    *,
    kind: str,
    title: str,
    detail: str,
) -> int:
    if call.status != "success":
        return 0
    identifier = f"crush:{call.session_id}:call:{call.call_id}:{kind}"
    return int(
        append_event_once(
            root,
            {
                **hook_context(_crush_context(identity, call.message_id)),
                **_crush_timing(call.epoch_ms),
                "operation_id": identifier,
                "group_id": f"crush:{call.session_id}:call:{call.call_id}",
                "kind": kind,
                "status": "success",
                "title": title,
                "detail": detail,
                "source_event_id": f"{identifier}:complete",
            },
        )
    )


def _append_crush_tool_event(
    root: Path,
    project: CrushProject,
    identity: AgentIdentity,
    call: CrushToolLifecycle,
) -> int:
    name = call.tool_name.casefold()
    arguments = call.tool_input
    source_phase = "running" if call.status == "running" else "complete"
    source = (
        f"crush:{call.session_id}:message:{call.message_id}:"
        f"call:{call.call_id}:{source_phase}"
    )
    context = _crush_context(identity, call.message_id)
    timing = _crush_timing(call.epoch_ms)
    if name in {"bash", "shell", "terminal"}:
        command = arguments.get("command")
        if not isinstance(command, str) or not command:
            return 0
        raw_working_dir = arguments.get("working_dir")
        working_dir = (
            _crush_absolute_path(raw_working_dir, project)
            if isinstance(raw_working_dir, str) and raw_working_dir
            else os.fspath(project.path)
        )
        if working_dir is None or not _native_path_matches_root(
            root, working_dir, os.fspath(project.path)
        ):
            return 0
        return _append_native_tool_events(
            root,
            {
                **context,
                "cwd": working_dir,
                "tool_use_id": f"crush:{call.session_id}:{call.call_id}",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
            call.status,
            source,
            timing,
        )
    if name in {"edit", "write"}:
        raw_path = arguments.get("file_path") or arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return 0
        path = _crush_absolute_path(raw_path, project)
        if path is None or not _native_path_matches_root(
            root, path, os.fspath(project.path)
        ):
            return 0
        return _append_native_tool_events(
            root,
            {
                **context,
                "cwd": os.fspath(project.path),
                "tool_use_id": f"crush:{call.session_id}:{call.call_id}",
                "tool_name": "Write" if name == "write" else "Edit",
                "tool_input": {"path": path},
            },
            call.status,
            source,
            timing,
        )
    if name in {"view", "read"}:
        raw_path = arguments.get("file_path") or arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return 0
        path = _crush_absolute_path(raw_path, project)
        if path is None or not _native_path_matches_root(
            root, path, os.fspath(project.path)
        ):
            return 0
        return _append_crush_marker(
            root,
            identity,
            call,
            kind="search",
            title="Read file",
            detail=path,
        )
    if name in {"grep", "glob"}:
        return _append_crush_marker(
            root,
            identity,
            call,
            kind="search",
            title="Searched code" if name == "grep" else "Searched files",
            detail="search",
        )
    if name in {"fetch", "webfetch"}:
        return _append_crush_marker(
            root,
            identity,
            call,
            kind="search",
            title="Fetched web page",
            detail="web page",
        )
    if name in {"todo", "todo_write", "todowrite"}:
        count = arguments.get("todo_count")
        count = count if isinstance(count, int) and not isinstance(count, bool) else 0
        return _append_crush_marker(
            root,
            identity,
            call,
            kind="todo",
            title="Todo updated",
            detail=f"{count} task" + ("" if count == 1 else "s"),
        )
    # The agent tool's prompt is private. Child session rows provide the safe
    # lifecycle signal, so the tool call itself is intentionally ignored.
    return 0


def _crush_finish_event(reason: str) -> tuple[str, str]:
    if reason in {"error", "content_filter"}:
        return "failed", "Subagent failed"
    if reason == "canceled":
        return "unknown", "Subagent cancelled"
    return "success", "Subagent completed"


class _CrushCheckpointUnavailable(Exception):
    def __init__(self, code: PollErrorCode) -> None:
        self.code = code


class CrushPollAdapter:
    """Read each relevant Crush database at most once per polling cycle."""

    provider = "crush"

    def __init__(self, checkpoint_store: CheckpointStore) -> None:
        self._checkpoint_store = checkpoint_store
        self._root_baselines: dict[Path, int] = {}

    def poll(self, targets: tuple[PollTarget, ...]) -> PollBatch:
        started = time.monotonic()
        events: list[tuple[Path, SafeEvent]] = []
        _POLL_EVENT_BUFFER.events = events
        try:
            try:
                return self._poll(targets, events, started)
            except _CrushCheckpointUnavailable as error:
                events.clear()
                return PollBatch(
                    PollStats(
                        self.provider,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        parse_errors=1,
                        last_error=error.code,
                    )
                )
        finally:
            del _POLL_EVENT_BUFFER.events

    def _load_position(
        self,
        root: Path,
        session: SessionKey,
        source: str,
        default: int,
    ) -> int:
        try:
            checkpoint = self._checkpoint_store.load(root, session, source)
        except sqlite3.Error:
            raise _CrushCheckpointUnavailable(PollErrorCode.SQLITE) from None
        except OSError:
            raise _CrushCheckpointUnavailable(PollErrorCode.IO) from None
        except Exception:
            raise _CrushCheckpointUnavailable(PollErrorCode.UNKNOWN) from None
        return checkpoint.position if checkpoint is not None else default

    def _root_baseline(self, root: Path) -> int:
        proposed = self._root_baselines.setdefault(root, int(time.time()) * 1000)
        session = SessionKey("crush", CRUSH_ROOT_CHECKPOINT_SESSION)
        baseline = self._load_position(
            root, session, CRUSH_ROOT_CHECKPOINT_SOURCE, proposed
        )
        if baseline != proposed:
            return baseline
        try:
            self._checkpoint_store.save(
                root,
                StreamCheckpoint(
                    session=session,
                    source=CRUSH_ROOT_CHECKPOINT_SOURCE,
                    position=proposed,
                ),
            )
        except sqlite3.Error:
            raise _CrushCheckpointUnavailable(PollErrorCode.SQLITE) from None
        except OSError:
            raise _CrushCheckpointUnavailable(PollErrorCode.IO) from None
        except Exception:
            raise _CrushCheckpointUnavailable(PollErrorCode.UNKNOWN) from None
        return proposed

    def _poll(
        self,
        targets: tuple[PollTarget, ...],
        events: list[tuple[Path, SafeEvent]],
        started: float,
    ) -> PollBatch:
        health = _PollHealth()
        active_roots = {target.root for target in targets}
        for root in set(self._root_baselines) - active_roots:
            del self._root_baselines[root]
        baselines = {target.root: self._root_baseline(target.root) for target in targets}
        identities = {
            target.root: {
                identity.session_id: identity
                for identity in target.for_provider("crush")
            }
            for target in targets
        }
        checkpoints: list[tuple[Path, StreamCheckpoint]] = []
        completed_roots: set[Path] = set()
        listing, listing_boundary, failed_projects, incomplete_projects = (
            _crush_session_snapshot(health=health)
        )
        blocked_roots = {
            root
            for project in (*failed_projects, *incomplete_projects)
            if (root := _crush_watched_root(project, baselines)) is not None
        }
        for project, sessions in listing:
            root = _crush_watched_root(project, identities)
            if root is None:
                continue
            records, trees = _crush_session_trees(sessions)
            selected_top = set(identities[root]) & set(trees)
            if any(
                top not in selected_top
                and (newest := max(record.updated_epoch_ms for record in members))
                >= baselines[root] - CRUSH_OVERLAP_MS
                and listing_boundary - newest
                <= CRUSH_SESSION_IDENTITY_WINDOW_SECONDS * 1000
                for top, members in trees.items()
            ):
                blocked_roots.add(root)
            if not selected_top:
                continue
            selected = tuple(record for top in selected_top for record in trees[top])
            checkpoint_source = _crush_checkpoint_source(project)
            positions = {
                record.session_id: self._load_position(
                    root,
                    SessionKey("crush", record.session_id),
                    checkpoint_source,
                    baselines[root],
                )
                for record in selected
            }
            try:
                calls, turns, boundaries = read_crush_activity(
                    project.database,
                    positions,
                )
            except sqlite3.Error:
                health.record(PollErrorCode.SQLITE)
                blocked_roots.add(root)
                continue
            except OSError:
                health.record(PollErrorCode.IO)
                blocked_roots.add(root)
                continue
            except ValueError:
                health.record(PollErrorCode.PARSE)
                blocked_roots.add(root)
                continue
            top_for = {
                record.session_id: _crush_top_session_id(record.session_id, records)
                for record in selected
            }
            for call in calls:
                if call.epoch_ms < positions.get(call.session_id, baselines[root]):
                    continue
                identity = identities[root].get(top_for.get(call.session_id, ""))
                if identity is not None:
                    _append_crush_tool_event(root, project, identity, call)
            for turn in turns:
                if turn.epoch_ms < positions.get(turn.session_id, baselines[root]):
                    continue
                identity = identities[root].get(top_for.get(turn.session_id, ""))
                if identity is None:
                    continue
                identifier = f"crush:{turn.session_id}:message:{turn.message_id}:turn"
                append_event_once(
                    root,
                    {
                        **hook_context(_crush_context(identity, turn.message_id)),
                        **_crush_timing(turn.epoch_ms),
                        "operation_id": identifier,
                        "group_id": identifier,
                        "kind": "session",
                        "status": turn.status,
                        "title": "Crush turn finished",
                        "detail": "",
                        "source_event_id": identifier,
                    },
                )
            for record in selected:
                if not record.parent_session_id:
                    continue
                identity = identities[root].get(top_for[record.session_id])
                if identity is None:
                    continue
                position = positions[record.session_id]
                operation = f"crush:{record.session_id}:subagent"
                if record.created_epoch_ms >= position:
                    append_event_once(
                        root,
                        {
                            **hook_context(_crush_context(identity, record.session_id)),
                            **_crush_timing(record.created_epoch_ms),
                            "operation_id": operation,
                            "group_id": operation,
                            "kind": "session",
                            "status": "running",
                            "title": "Subagent started",
                            "detail": "subagent",
                            "source_event_id": f"{operation}:running",
                        },
                    )
                if record.finished and record.updated_epoch_ms >= position:
                    status, title = _crush_finish_event(record.finish_reason)
                    append_event_once(
                        root,
                        {
                            **hook_context(_crush_context(identity, record.session_id)),
                            **_crush_timing(record.updated_epoch_ms),
                            "operation_id": operation,
                            "group_id": operation,
                            "kind": "session",
                            "status": status,
                            "title": title,
                            "detail": "subagent",
                            "source_event_id": f"{operation}:complete",
                        },
                    )
            checkpoints.extend(
                (
                    root,
                    StreamCheckpoint(
                        session=SessionKey("crush", record.session_id),
                        source=checkpoint_source,
                        position=max(
                            positions[record.session_id],
                            boundaries.get(record.session_id, positions[record.session_id]),
                        ),
                    ),
                )
                for record in selected
            )
            completed_roots.add(root)
        checkpoints.extend(
            (
                root,
                StreamCheckpoint(
                    session=SessionKey("crush", CRUSH_ROOT_CHECKPOINT_SESSION),
                    source=CRUSH_ROOT_CHECKPOINT_SOURCE,
                    position=max(baselines[root], listing_boundary),
                ),
            )
            for root in completed_roots - blocked_roots
        )
        return PollBatch(
            PollStats(
                self.provider,
                duration_ms=int((time.monotonic() - started) * 1000),
                parse_errors=health.parse_errors,
                last_error=health.last_error,
            ),
            events=tuple(events),
            checkpoints=tuple(checkpoints),
        )

    def close(self) -> None:
        self._root_baselines.clear()


def _t3code_row_status(row: T3CodePollRow) -> str:
    status = row.status.casefold()
    if status in {"failed", "error", "declined", "cancelled"}:
        return "failed"
    if row.kind == "tool.completed" or status in {"completed", "success", "succeeded"}:
        return "success"
    return "running"


def _t3code_timing(row: T3CodePollRow) -> dict[str, Any]:
    return _record_time({"created_at": row.created_at})


def _t3code_absolute_path(raw_path: str, working_root: str) -> str:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path(working_root).expanduser() / path
    return os.fspath(path.resolve(strict=False))


class T3CodePollAdapter:
    """One machine-wide T3 projection read for Cursor and Grok Build."""

    provider = "t3code"

    def __init__(self, checkpoint_store: CheckpointStore) -> None:
        self._checkpoint_store = checkpoint_store
        self._root_baselines: dict[Path, int] = {}

    def poll(self, targets: tuple[PollTarget, ...]) -> PollBatch:
        started = time.monotonic()
        events: list[tuple[Path, SafeEvent]] = []
        _POLL_EVENT_BUFFER.events = events
        try:
            return self._poll(targets, events, started)
        finally:
            del _POLL_EVENT_BUFFER.events

    def _poll(
        self,
        targets: tuple[PollTarget, ...],
        events: list[tuple[Path, SafeEvent]],
        started: float,
    ) -> PollBatch:
        active_roots = {target.root for target in targets}
        for root in set(self._root_baselines) - active_roots:
            del self._root_baselines[root]
        for root in active_roots:
            self._root_baselines.setdefault(root, int(time.time() * 1000))

        owners: dict[str, tuple[Path, AgentIdentity]] = {}
        for provider in ("cursor", "grok"):
            for target, identities in _routed_provider_identities(targets, provider):
                for key, wire in identities.items():
                    identity = AgentIdentity.from_wire(wire, key=key)
                    thread_id = str(identity.extras.get("t3code_thread_id") or "")
                    if thread_id:
                        owners.setdefault(thread_id, (target.root, identity))
        if not owners:
            return PollBatch(PollStats(self.provider))

        requests: list[T3CodePollRequest] = []
        positions: dict[str, tuple[int | None, int | None]] = {}
        try:
            for thread_id, (root, identity) in owners.items():
                activity = self._checkpoint_store.load(
                    root,
                    identity.key,
                    f"{T3CODE_ACTIVITY_SOURCE}:{thread_id}",
                )
                turns = self._checkpoint_store.load(
                    root,
                    identity.key,
                    f"{T3CODE_TURN_SOURCE}:{thread_id}",
                )
                positions[thread_id] = (
                    activity.position if activity is not None else None,
                    turns.position if turns is not None else None,
                )
                requests.append(
                    T3CodePollRequest(
                        thread_id,
                        positions[thread_id][0],
                        positions[thread_id][1],
                        self._root_baselines[root],
                    )
                )
            rows = read_t3code_poll_rows(
                t3code_database_path(), tuple(requests)
            )
        except sqlite3.Error:
            return PollBatch(
                PollStats(self.provider, last_error=PollErrorCode.SQLITE)
            )
        except OSError:
            return PollBatch(PollStats(self.provider, last_error=PollErrorCode.IO))
        except ValueError:
            return PollBatch(PollStats(self.provider, last_error=PollErrorCode.PARSE))
        except Exception:
            return PollBatch(PollStats(self.provider, last_error=PollErrorCode.UNKNOWN))

        grouped: dict[str, list[T3CodePollRow]] = {}
        for row in rows:
            if row.thread_id in owners:
                grouped.setdefault(row.thread_id, []).append(row)
        checkpoints: list[tuple[Path, StreamCheckpoint]] = []
        for thread_id, (root, identity) in owners.items():
            thread_rows = grouped.get(thread_id, [])
            activity_rows = [row for row in thread_rows if row.row_type == "activity"]
            turn_rows = [row for row in thread_rows if row.row_type == "turn"]
            activity_position, turn_position = positions[thread_id]
            maximum_activity = max(
                (row.maximum_position for row in activity_rows), default=0
            )
            maximum_turn = max((row.maximum_position for row in turn_rows), default=0)
            minimum_open_turn = next(
                (
                    row.minimum_open_turn
                    for row in turn_rows
                    if row.minimum_open_turn is not None
                ),
                None,
            )

            for row in activity_rows:
                if row.source_id and row.sequence is not None:
                    self._append_activity(root, identity, row)
            for row in turn_rows:
                if row.source_id and row.created_at:
                    self._append_turn(root, identity, row)

            if activity_position is None:
                observed = [
                    row.sequence
                    for row in activity_rows
                    if row.source_id and row.sequence is not None
                ]
                next_activity = (
                    max(observed) if observed else maximum_activity + 1
                )
            else:
                observed = [
                    row.sequence
                    for row in activity_rows
                    if row.source_id and row.sequence is not None
                ]
                next_activity = max(observed, default=activity_position)
            if turn_position is None:
                completed = [
                    row.sequence
                    for row in turn_rows
                    if row.source_id and row.sequence is not None
                ]
                next_turn = minimum_open_turn or (
                    max(completed) if completed else maximum_turn + 1
                )
            else:
                completed = [
                    row.sequence
                    for row in turn_rows
                    if row.source_id and row.sequence is not None
                ]
                next_turn = minimum_open_turn or max(completed, default=turn_position)

            checkpoints.extend(
                (
                    (
                        root,
                        StreamCheckpoint(
                            identity.key,
                            f"{T3CODE_ACTIVITY_SOURCE}:{thread_id}",
                            next_activity,
                        ),
                    ),
                    (
                        root,
                        StreamCheckpoint(
                            identity.key,
                            f"{T3CODE_TURN_SOURCE}:{thread_id}",
                            next_turn,
                        ),
                    ),
                )
            )
        return PollBatch(
            PollStats(
                self.provider,
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            events=tuple(events),
            checkpoints=tuple(checkpoints),
        )

    @staticmethod
    def _append_activity(
        root: Path, identity: AgentIdentity, row: T3CodePollRow
    ) -> None:
        status = _t3code_row_status(row)
        operation = row.tool_call_id or row.source_id
        context = {
            "agent": identity.agent,
            "session_id": identity.session_id,
            "model": identity.model,
            "effort": identity.effort,
            "prompt_id": row.turn_id,
        }
        timing = _t3code_timing(row)
        source = f"t3code:{row.thread_id}:activity:{row.source_id}:{row.kind}"
        if row.item_type == "command_execution" and row.command:
            if not _native_path_matches_root(root, "", identity.working_root):
                return
            _append_native_tool_events(
                root,
                {
                    **context,
                    "tool_use_id": operation,
                    "tool_name": "Bash",
                    "tool_input": {"command": row.command},
                    "cwd": identity.working_root,
                },
                status,
                source,
                timing,
            )
            return
        if row.item_type == "file_change":
            for index, raw_path in enumerate(row.paths):
                path = _t3code_absolute_path(raw_path, identity.working_root)
                if not _native_path_matches_root(root, path, identity.working_root):
                    continue
                _append_native_tool_events(
                    root,
                    {
                        **context,
                        "tool_use_id": f"{operation}:{index}",
                        "tool_name": "Edit",
                        "tool_input": {"path": path},
                        "cwd": identity.working_root,
                    },
                    status,
                    f"{source}:path:{index}",
                    timing,
                )

    @staticmethod
    def _append_turn(root: Path, identity: AgentIdentity, row: T3CodePollRow) -> None:
        if not _native_path_matches_root(root, "", identity.working_root):
            return
        timing = _t3code_timing(row)
        append_event_once(
            root,
            {
                "agent": identity.agent,
                "session_id": identity.session_id,
                "model": identity.model,
                "effort": identity.effort,
                "turn_id": row.turn_id,
                "operation_id": f"t3code:{row.thread_id}:turn:{row.turn_id}",
                "group_id": f"t3code:{row.thread_id}:turn:{row.turn_id}",
                "kind": "session",
                "status": "success",
                "title": "Turn completed",
                "detail": "",
                "source_event_id": f"t3code:{row.thread_id}:{row.source_id}",
                **timing,
            },
        )

    def close(self) -> None:
        return None


def create_poll_coordinator() -> PollCoordinator:
    """Build the shared collector scheduler used by terminal and browser views."""
    checkpoint_store = CheckpointStore(native_index_path)
    adapters = tuple(
        CodingAgentPollAdapter(provider, checkpoint_store=checkpoint_store)
        for provider in (
            "codex",
            "pi",
            "deepseek",
            "antigravity",
            "opencode",
            "cline",
        )
    ) + (
        CrushPollAdapter(checkpoint_store),
        T3CodePollAdapter(checkpoint_store),
    )
    return PollCoordinator(
        adapters,
        event_sink=lambda root, event: append_event_once(root, event),
        checkpoint_store=checkpoint_store,
    )


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


def claude_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Claude sessions working in this folder, read from Claude's own registry."""
    del now  # Common registry loader signature; Claude derives freshness itself.
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
        if isinstance(agent, dict) and integration_for(agent.get("agent")) is not None
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
    snapshot, because Herdr is one optional source and never a prerequisite.
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
    if integration_for(agent.get("agent")) is None:
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
        integration = integration_for(agent.get("agent"))
        if integration is None:
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
            "agent": integration.provider,
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
            identity.update(integration.metadata_loader(session_id))
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


def github_refresh_interval(
    github_status: dict[str, Any] | None, configured_interval: float
) -> float:
    """Back off GitHub reads when a branch cannot be changing quickly.

    The CLI's ``gh pr view --json`` readback uses GitHub's GraphQL quota. A
    watch can contain several worktrees, so polling every root at the same
    short interval wastes quota on branches with no PR and PRs that are
    already finished. Delivery activity and branch switches still force an
    immediate readback by resetting ``last_github_refresh``.
    """
    if configured_interval <= 0:
        return float("inf")
    if github_status is None:
        return max(configured_interval, GITHUB_NO_PR_POLL_SECONDS)
    if github_status.get("coverage") == "PARTIAL":
        return max(configured_interval, GITHUB_PARTIAL_POLL_SECONDS)
    if str(github_status.get("state", "")).upper() in {"CLOSED", "MERGED"}:
        return max(configured_interval, GITHUB_TERMINAL_POLL_SECONDS)
    return configured_interval


def github_refresh_due(
    github_status: dict[str, Any] | None,
    last_refresh: float,
    now: float,
    configured_interval: float,
) -> bool:
    return now - last_refresh >= github_refresh_interval(
        github_status, configured_interval
    )


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
        _icon, state_style = event_style(latest)
        summary = style_source_label(summary, latest, color, ANSI["dim"])
        heading = (
            f"│ {ANSI['dim']}{when}{ANSI['reset']} "
            f"{state_style}✎{ANSI['reset']} {ANSI['dim']}{summary}{ANSI['reset']}"
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
    expanded: bool = False,
    search: str = "",
) -> list[str]:
    events = unit["events"]
    ordered = sorted(
        events,
        key=lambda event: (
            event_epoch(event),
            int(event.get("_append_ordinal", 0)),
        ),
    )
    when = display_time(max(ordered, key=event_epoch))
    actor = actor_label(ordered[-1], identities)
    heading = str(unit["title"])
    if actor:
        heading = f"{actor} · {heading}"
    heading = label_summary(ordered[-1], heading, show_source)
    state, glyph, state_word = task_state(ordered, unit.get("github"))
    event_count = sum(int(event.get("repeat_count", 1)) for event in ordered)
    count_text = f"{event_count} event" + ("" if event_count == 1 else "s")
    duration = group_duration(events, now_ms)
    status_text = f"{glyph} {state_word}"
    metadata_parts = [status_text, count_text, *([duration] if duration else [])]
    metadata = " · ".join(metadata_parts)
    plain_heading = f"│ {when} ┌ {heading} · {metadata}"
    if terminal_cell_width(plain_heading) <= width and color:
        state_style = SEMANTIC_ANSI[state]
        heading = style_source_label(
            heading, ordered[-1], color, ANSI["bold"]
        )
        task_heading = (
            f"│ {ANSI['dim']}{when}{ANSI['reset']} "
            f"{state_style}{ANSI['bold']}┌{ANSI['reset']} "
            f"{heading}{ANSI['reset']} · "
            f"{state_style}{ANSI['bold']}{status_text}{ANSI['reset']}"
            f"{ANSI['dim']} · {' · '.join(metadata_parts[1:])}{ANSI['reset']}"
        )
        task_headings = [task_heading]
    elif terminal_cell_width(plain_heading) <= width:
        task_heading = plain_heading
        task_headings = [task_heading]
    else:
        heading_prefix = f"│ {when} ┌ "
        heading = crop(heading, max(1, width - terminal_cell_width(heading_prefix)))
        metadata_width = max(1, width - terminal_cell_width("│   │ "))
        metadata_lines: list[list[str]] = []
        current: list[str] = []
        for part in metadata_parts:
            candidate = " · ".join([*current, part])
            if current and terminal_cell_width(candidate) > metadata_width:
                metadata_lines.append(current)
                current = [part]
            else:
                current.append(part)
        if current:
            metadata_lines.append(current)
        if color:
            state_style = SEMANTIC_ANSI[state]
            styled_heading = style_source_label(
                heading, ordered[-1], color, ANSI["bold"]
            )
            task_headings = [
                f"│ {ANSI['dim']}{when}{ANSI['reset']} "
                f"{state_style}{ANSI['bold']}┌{ANSI['reset']} "
                f"{styled_heading}{ANSI['reset']}"
            ]
            for parts in metadata_lines:
                first, *rest = parts
                value_style = (
                    f"{state_style}{ANSI['bold']}"
                    if first == status_text
                    else ANSI["dim"]
                )
                line = (
                    f"│   {ANSI['dim']}│{ANSI['reset']} "
                    f"{value_style}{first}{ANSI['reset']}"
                )
                if rest:
                    line += f"{ANSI['dim']} · {' · '.join(rest)}{ANSI['reset']}"
                task_headings.append(line)
        else:
            task_headings = [f"{heading_prefix}{heading}"]
            task_headings.extend(
                f"│   │ {' · '.join(parts)}" for parts in metadata_lines
            )

    if expanded:
        child_lines = []
        for index, event in enumerate(ordered):
            connector = "└─" if index == len(ordered) - 1 else "├─"
            child = render_event_line(
                event,
                max(4, width - 5),
                color,
                now_ms,
                identities,
                show_source,
                search,
            )
            if color:
                child_lines.append(
                    f"│   {ANSI['dim']}{connector}{ANSI['reset']} {child[2:]}"
                )
            else:
                child_lines.append(f"│   {connector} {child[2:]}")
        return [*task_headings, *child_lines]

    pipeline = crop(
        " → ".join(str(stage) for stage in unit["stages"]),
        max(4, width - 7),
    )
    if color:
        return [
            *task_headings,
            f"│   {ANSI['dim']}└─{ANSI['reset']} {pipeline}",
        ]
    return [*task_headings, f"│   └─ {pipeline}"]


def task_state(
    events: list[dict[str, Any]], github_status: dict[str, Any] | None = None
) -> tuple[str, str, str]:
    """Use one explicit status vocabulary for task headings."""

    latest_stages: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=event_epoch):
        latest_stages[task_status_key(event)] = event
    final_events = list(latest_stages.values())
    statuses = {str(event.get("status", "unknown")) for event in final_events}
    blocked = isinstance(github_status, dict) and (
        str(github_status.get("merge_state", "")).upper() in {"BLOCKED", "DIRTY"}
        or str(github_status.get("mergeable", "")).upper() == "CONFLICTING"
    )
    github_failed = isinstance(github_status, dict) and (
        int(github_status.get("checks_failed") or 0) > 0
        or str(github_status.get("review", "")).upper() == "CHANGES_REQUESTED"
    )
    github_running = (
        isinstance(github_status, dict)
        and int(github_status.get("checks_pending") or 0) > 0
    )
    if "failed" in statuses or github_failed:
        return "failure", STATUS_GLYPHS["failed"], "failed"
    if blocked:
        return "failure", STATUS_GLYPHS["failed"], "blocked"
    if "running" in statuses or github_running:
        return "running", STATUS_GLYPHS["running"], "running"
    if "unknown" in statuses:
        return "unknown", STATUS_GLYPHS["unknown"], "unknown"
    return "success", STATUS_GLYPHS["success"], "completed"


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
    _icon, state_style = event_style(latest)
    return [
        f"│ {ANSI['dim']}{when}{ANSI['reset']} "
        f"{state_style}↗{ANSI['reset']} {ANSI['dim']}{summary}{ANSI['reset']}"
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


def truncate_activity_unit(
    unit: dict[str, Any],
    lines: list[str],
    line_limit: int,
    color: bool,
    expanded_history: bool,
) -> tuple[list[str], int]:
    """Keep a task's heading and newest child when its full expansion will not fit."""

    if (
        line_limit >= len(lines)
        or line_limit <= 0
        or unit["type"] != "pipeline"
        or not expanded_history
    ):
        return lines[: max(0, line_limit)], max(0, len(lines) - line_limit)
    child_count = len(unit["events"])
    heading_count = max(1, len(lines) - child_count)
    if line_limit <= heading_count:
        return lines[:line_limit], child_count
    child_slots = line_limit - heading_count
    if child_slots == 1:
        return [*lines[:heading_count], lines[-1]], max(0, child_count - 1)
    kept_children = max(0, child_slots - 1)
    omitted = max(0, child_count - kept_children)
    marker = f"│   … {omitted} earlier event" + ("" if omitted == 1 else "s")
    marker += " hidden"
    if color:
        marker = f"{ANSI['dim']}{marker}{ANSI['reset']}"
    visible = [*lines[:heading_count], marker]
    if kept_children:
        visible.extend(lines[-kept_children:])
    return visible[:line_limit], omitted


def render_activity_unit(
    unit: dict[str, Any],
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
    show_source: bool = True,
    search: str = "",
    expanded_history: bool = False,
) -> list[str]:
    events = unit["events"]
    if unit["type"] == "pipeline":
        return render_pipeline_card(
            unit,
            width,
            color,
            now_ms,
            identities,
            show_source,
            expanded_history,
            search,
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
    partially_hidden = 0
    selected_day: date | None = None
    today = local_date_for_epoch(now_ms, local_timezone)
    for unit in candidates:
        lines = render_activity_unit(
            unit,
            width,
            color,
            now_ms,
            identities,
            search=search,
            expanded_history=expanded_history,
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
                visible, omitted = truncate_activity_unit(
                    unit,
                    lines,
                    remaining - 1,
                    color,
                    expanded_history,
                )
                selected.append((unit_day, unit, visible))
                partially_hidden += omitted
                selected_day = unit_day
            elif not separator_cost:
                visible, omitted = truncate_activity_unit(
                    unit,
                    lines,
                    remaining,
                    color,
                    expanded_history,
                )
                selected.append((unit_day, unit, visible))
                partially_hidden += omitted
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
                unit,
                width,
                color,
                now_ms,
                identities,
                show_source=False,
                search=search,
                expanded_history=expanded_history,
            )
        lines = apply_root_gutter(lines, unit_color_index(unit), color)
        selected[index] = (unit_day, unit, lines)
        previous_source = source
    hidden = max(0, len(candidates) - selected_units) + partially_hidden
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
        style = SEMANTIC_ANSI["failure"]
    elif status.get("coverage") == "PARTIAL" or pending:
        style = SEMANTIC_ANSI["warning"]
    elif status.get("state") == "MERGED" or (
        status.get("state") == "OPEN" and status.get("merge_state") == "CLEAN"
    ):
        style = SEMANTIC_ANSI["success"]
    elif status.get("state") == "CLOSED":
        style = SEMANTIC_ANSI["idle"]
    else:
        style = SEMANTIC_ANSI["navigation"]
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
        session_id = identity.get("session_id")
        if session_id:
            identity = AgentIdentity.from_wire(identity).to_wire()
        key = (
            identity.get("pane_id")
            or (agent_session_key(agent, session_id) if session_id else "")
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
        text = render_agent_context_text(identity, width)
        if color:
            text = style_agent_context(text, identity)
            text = f"{ANSI['dim']}{text}{ANSI['reset']}"
        lines.append(text)
    return lines


def display_agent_effort(value: Any) -> str:
    """Shorten common reasoning-effort names without guessing unknown ones."""
    text = str(value or "").strip()
    return {"medium": "med", "minimal": "min"}.get(text.casefold(), text)


def display_agent_model(value: Any, agent: Any) -> str:
    """Use the familiar short form for Codex model names in agent headers."""
    text = display_model(value)
    if (
        normalize_agent(agent) == "codex"
        and text.casefold().startswith("gpt-")
        and len(text) > len("gpt-")
    ):
        return text[len("gpt-") :]
    return text


def display_agent_working_folder(identity: dict[str, str]) -> str:
    """Name the agent's actual working folder, using ``~`` when possible."""
    raw_path = identity.get("working_root") or identity.get("root")
    if not raw_path:
        return ""
    return display_root(Path(raw_path).expanduser())


def agent_status_display(value: Any) -> tuple[str, str]:
    """Return a stable semantic role and a no-color-readable status label."""
    status = str(value or "unknown").strip().casefold()
    if status in {"working", "running", "pending"}:
        return "running", f"{STATUS_GLYPHS['running']} working"
    if status in {"failed", "blocked", "error"}:
        label = "blocked" if status == "blocked" else "failed"
        return "failure", f"{STATUS_GLYPHS['failed']} {label}"
    if status in {"success", "completed", "done", "finished"}:
        return "success", f"{STATUS_GLYPHS['success']} completed"
    if status == "idle":
        return "idle", f"{STATUS_GLYPHS['idle']} idle"
    return "unknown", f"{STATUS_GLYPHS['unknown']} unknown"


def style_agent_context(
    text: str, identity: dict[str, str], *, restore: str = ANSI["dim"]
) -> str:
    """Accent identity and state independently inside an already-fitted line."""
    agent = agent_label(identity.get("agent"))
    source_label = identity.get(SOURCE_LABEL, "").strip()
    prefix = f" [{source_label}] {agent}" if source_label else f" {agent}"
    agent_at = len(prefix) - len(agent) if text.startswith(prefix) else -1
    if agent_at >= 0:
        text = (
            text[:agent_at]
            + f"{SEMANTIC_ANSI['identity']}{ANSI['bold']}{agent}"
            f"{ANSI['reset']}{restore}"
            + text[agent_at + len(agent) :]
        )
    role, status = agent_status_display(identity.get("status"))
    marker = f" · {status}"
    marker_at = text.rfind(marker)
    if marker_at >= 0:
        state_at = marker_at + len(" · ")
        text = (
            text[:state_at]
            + f"{SEMANTIC_ANSI[role]}{status}{ANSI['reset']}{restore}"
            + text[state_at + len(status) :]
        )
    return text


def render_agent_context_text(
    identity: dict[str, str], width: int, git_status: dict[str, str] | None = None
) -> str:
    """Fit one agent header while preserving its runtime and working folder."""
    agent = agent_label(identity.get("agent"))
    source_label = identity.get(SOURCE_LABEL, "").strip()
    label = identity.get("label", "").strip()
    model = (
        display_agent_model(identity.get("model"), identity.get("agent")) or "model ?"
    )
    effort = display_agent_effort(identity.get("effort")) or "effort ?"
    _status_role, status = agent_status_display(identity.get("status"))
    folder = display_agent_working_folder(identity)
    source = f"[{source_label}] " if source_label else ""
    prefix = f" {source}{agent}"
    context = f" · {label}" if label and label.casefold() != agent.casefold() else ""
    runtime = f" · {model}/{effort}"
    folder_context = f" · {folder}" if folder else ""
    state = f" · {status}"
    git = (
        f"  │  {git_status['branch']} @ {git_status['short_oid']}" if git_status else ""
    )
    full = prefix + context + runtime + folder_context + state + git
    if terminal_cell_width(full) <= width:
        return full

    # The task label is useful on a wide screen, but runtime, location, and
    # status answer the first questions when a pane is narrow. Keep those and
    # abbreviate the left side of the path so worktree names remain visible.
    compact_without_folder = prefix + runtime + state + git
    if folder:
        separator_width = terminal_cell_width(" · ")
        folder_width = max(
            1,
            width - terminal_cell_width(compact_without_folder) - separator_width,
        )
        folder_context = f" · {crop_left(folder, folder_width)}"
    compact = prefix + runtime + folder_context + state + git
    return crop(compact, width)


def render_context_banners(
    identities: dict[str, dict[str, str]],
    git_status: dict[str, str] | None,
    width: int,
    color: bool,
) -> list[str]:
    agents = active_agent_identities(identities)
    lines: list[str] = []
    for index, identity in enumerate(agents):
        text = render_agent_context_text(
            identity, width, git_status if index == 0 else None
        )
        if color:
            text = style_agent_context(text, identity)
            text = style_source_label(text, identity, color, ANSI["dim"])
            text = f"{ANSI['dim']}{text}{ANSI['reset']}"
        lines.append(text)
    if not agents and git_status:
        lines.append(render_git_banner(git_status, width, color))
    return lines


def usage_session_keys(
    records: Iterable[dict[str, Any]],
    identities: Mapping[str, Mapping[str, Any]],
    root: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    """Provider-qualified sessions already approved for this displayed root."""
    keys: set[tuple[str, str]] = set()
    for identity in identities.values():
        if root is not None:
            raw_root = identity.get("working_root") or identity.get("root")
            if not isinstance(raw_root, str):
                continue
            try:
                identity_root = canonical_root(raw_root)
                selected_root = canonical_root(root)
                if identity_root != selected_root and not identity_root.is_relative_to(
                    selected_root
                ):
                    continue
            except (OSError, ValueError):
                continue
        session_id = identity.get("session_id")
        if isinstance(session_id, str) and session_id:
            keys.add((normalize_agent(identity.get("agent")), session_id))
    for event in records:
        session_id = event.get("session_id")
        if isinstance(session_id, str) and session_id:
            keys.add((normalize_agent(event.get("agent")), session_id))
    return tuple(sorted(keys))


def usage_identity_contexts(
    identities: Mapping[str, Mapping[str, Any]],
    root: Path | None = None,
) -> dict[tuple[str, str], dict[str, str]]:
    """Keep only fields needed to label an associated session in memory."""
    contexts: dict[tuple[str, str], dict[str, str]] = {}
    for identity in identities.values():
        session_id = identity.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if root is not None:
            raw_root = identity.get("working_root") or identity.get("root")
            if not isinstance(raw_root, str):
                continue
            try:
                identity_root = canonical_root(raw_root)
                selected_root = canonical_root(root)
                if identity_root != selected_root and not identity_root.is_relative_to(
                    selected_root
                ):
                    continue
            except (OSError, ValueError):
                continue
        agent = normalize_agent(identity.get("agent"))
        contexts[(agent, session_id)] = {
            field: str(identity.get(field, ""))
            for field in ("agent", "session_id", "label", "model", "status")
        }
    return contexts


def refreshed_usage_contexts(
    previous: Mapping[tuple[str, str], Mapping[str, str]],
    identities: Mapping[str, Mapping[str, Any]],
    root: Path,
) -> dict[tuple[str, str], dict[str, str]]:
    """Retain labels for history while retiring identities no longer live."""
    refreshed = {
        key: {**context, "status": "idle"}
        for key, context in previous.items()
    }
    refreshed.update(usage_identity_contexts(identities, root))
    return refreshed


def render_usage_banner(
    report: UsageReport | LiveUsageSnapshot,
    records: Iterable[dict[str, Any]],
    identities: Mapping[str, Mapping[str, Any]],
    width: int,
    color: bool,
    sessions: Iterable[tuple[str, str]] | None = None,
    *,
    expanded: bool = False,
    root_count: int = 1,
    contexts: Iterable[Mapping[str, Any]] | None = None,
    session_cadence: float = 180.0,
    block_cadence: float = 10.0,
    max_lines: int | None = None,
) -> str:
    selected = (
        usage_session_keys(records, identities) if sessions is None else sessions
    )
    snapshot = (
        report
        if isinstance(report, LiveUsageSnapshot)
        else LiveUsageSnapshot(
            UsageReport("session", status="unavailable", detail="loading"),
            report,
            UsageBlock(detail="loading"),
        )
    )
    lines = list(
        live_usage_lines(
            snapshot,
            selected,
            identities.values() if contexts is None else contexts,
            root_count=root_count,
            session_cadence=session_cadence,
            block_cadence=block_cadence,
        )
    )
    if expanded:
        wire = usage_summary_wire(
            snapshot,
            selected,
            identities.values() if contexts is None else contexts,
            session_cadence=session_cadence,
            block_cadence=block_cadence,
        )
        session_lines: list[str] = []
        for row in wire["rows"]:
            today = f"{int(row['today_tokens']):,}"
            lifetime = f"{int(row['lifetime_tokens']):,}"
            today_cost = (
                f" / ${float(row['today_cost_usd']):.2f}"
                if "today_cost_usd" in row
                else ""
            )
            lifetime_cost = (
                f" / ${float(row['lifetime_cost_usd']):.2f}"
                if "lifetime_cost_usd" in row
                else ""
            )
            last = f" · {row['last_activity']}" if row["last_activity"] else ""
            session_lines.append(
                f"  {row['agent']} · {row['label']} · {row['status']} · "
                f"today {today} tok{today_cost} · lifetime {lifetime} tok"
                f"{lifetime_cost}{last}"
            )
        if max_lines is not None:
            detail_slots = max(0, max_lines - len(lines) - 1)
            if len(session_lines) > detail_slots:
                visible_slots = max(0, detail_slots - 1)
                hidden = len(session_lines) - visible_slots
                session_lines = session_lines[:visible_slots]
                if detail_slots:
                    session_lines.append(f"  … {hidden} more sessions")
        lines.extend(session_lines)
        if max_lines is None or len(lines) < max_lines:
            lines.append(f"  {wire['pricing_label']}")
    cropped = [crop(" " + line, width) for line in lines]
    if color:
        return "\n".join(
            f"{ANSI['dim']}{line}{ANSI['reset']}" for line in cropped
        )
    return "\n".join(cropped)


def usage_display_snapshot(
    live_report: LiveUsageSnapshot,
    live_sessions: Mapping[str, Iterable[tuple[str, str]]],
    paused_report: LiveUsageSnapshot | None,
    paused_sessions: Mapping[str, Iterable[tuple[str, str]]] | None,
) -> tuple[LiveUsageSnapshot, Mapping[str, Iterable[tuple[str, str]]]]:
    """Freeze both usage values and their root scope with the paused timeline."""
    if paused_report is not None and paused_sessions is not None:
        return paused_report, paused_sessions
    return live_report, live_sessions


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
        identity["session_id"] = session_id
        for field_name in ("model", "effort"):
            value = event.get(field_name)
            if isinstance(value, str) and value:
                identity[field_name] = value
        for field_name in (SOURCE_LABEL, SOURCE_COLOR_INDEX):
            value = event.get(field_name)
            if isinstance(value, (str, int)) and str(value):
                identity[field_name] = str(value)
        typed_identity = AgentIdentity.from_wire(
            identity, key=SessionKey(agent, session_id)
        )
        combined[typed_identity.key.to_wire()] = typed_identity.to_wire()
    return combined


def next_event_filter(event_filter: str) -> str:
    index = FILTER_ORDER.index(event_filter) if event_filter in FILTER_ORDER else 0
    return FILTER_ORDER[(index + 1) % len(FILTER_ORDER)]


def render_help(
    width: int,
    color: bool,
    newest_first: bool = True,
    root_count: int = 1,
    *,
    expanded_history: bool = False,
    event_filter: str = "all",
    paused: bool = False,
    focused_root_label: str | None = None,
    expanded_header: bool = False,
) -> list[str]:
    heading = "┌ Help"
    if color:
        heading = f"{ANSI['bold']}{ANSI['blue']}{heading}{ANSI['reset']}"
    order_note = (
        "Newest activity is at the top"
        if newest_first
        else "Newest activity is at the bottom"
    )
    detail_action = "compact detail" if expanded_history else "expand detail"
    header_action = (
        "hide header details" if expanded_header else "show header details"
    )
    pause_action = "resume display" if paused else "pause display"
    order_action = (
        "put oldest activity first"
        if newest_first
        else "put newest activity first"
    )
    entries = [
        "│ ?       toggle this help",
        f"│ E       {header_action}",
        f"│ e       {detail_action}",
        f"│ f       show {next_event_filter(event_filter)} (now {event_filter})",
        f"│ p       {pause_action}",
        "│ /       show only lines matching what you type; Esc clears it",
        "│ C       open the browser panel for these folders",
        f"│ r       {order_action}",
    ]
    if root_count > 1:
        entries.extend(
            (
                "│",
                "│ Folder colors: the block starting a line, its source badge,",
                "│ and its column title all share one color.",
                "│",
                "│ Views (default: auto)",
                "│ All     wide pane: a column per folder; narrow: one list",
                "│ Focus   one folder fills the pane",
                (
                    "│ a       show all folders again"
                    if focused_root_label
                    else "│ a       keep all folders visible"
                ),
                "│ Tab     move to the next folder",
                f"│ 1-{min(root_count, 9)}     jump to a folder by position",
                "│ --layout auto|columns|timeline selects the startup layout",
            )
        )
    entries.extend(
        (
            "│ Esc     close this help",
            "│ R       reload Side Dog with the same folders and flags",
            "│ q       confirm before quitting Side Dog",
            "│ Ctrl-C  confirm once; press twice to quit immediately",
            "│",
            "│ Folders: none named means your Herdr session, or every folder",
            '│ an agent works in ("found"); new repositories join on their own.',
            "│ Config ~/.config/side-dog/config.toml: pin, ignore, [display].",
            "│ watch @NAME opens a saved space; --save NAME writes one.",
            "│",
            f"│ {order_note}; runs of file writes fold into one line.",
            "│ A task card links one agent turn: edits, tests, commits, pushes.",
            "│ Status: ✓ completed · … running · ! warning · × failed · ○ idle · ? unknown.",
            "│ Only the folders you watch are shown; every event is saved to disk.",
            "│ Color: blue navigation · purple identity · green completed · amber running",
            "│ or warning · red failed · neutral idle/unknown. Root badges name folders.",
            f"│ Side Dog: {PROJECT_URL}",
            "└ Press ? or Esc to return",
        )
    )
    return [heading, *(crop(entry, width) for entry in entries)]


def render_footer(
    width: int,
    color: bool,
    *,
    root_count: int,
    expanded_history: bool,
    paused: bool,
    focused_root_label: str | None = None,
) -> list[str]:
    """Render only high-value actions, wrapping between actions when needed."""

    actions: list[str] = []
    if root_count > 1:
        actions.append("a all folders" if focused_root_label else "Tab folder")
    actions.extend(
        (
            f"e {'compact' if expanded_history else 'expand'}",
            f"p {'resume' if paused else 'pause'}",
            "/ find",
            "? help",
            "q quit",
        )
    )
    lines: list[str] = []
    prefix = "─ "
    continuation = "  "
    line = prefix
    for action in actions:
        separator = "" if line in {prefix, continuation} else " · "
        candidate = f"{line}{separator}{action}"
        if terminal_cell_width(candidate) <= width:
            line = candidate
            continue
        if line not in {prefix, continuation}:
            lines.append(crop(line, width))
        line = continuation + action
    if line not in {prefix, continuation}:
        lines.append(crop(line, width))
    if color:
        return [f"{ANSI['dim']}{line}{ANSI['reset']}" for line in lines]
    return lines


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


@dataclass
class QuitConfirmation:
    """Small, testable state machine for the terminal quit dialog."""

    visible: bool = False
    selected_yes: bool = False

    def request(self) -> bool:
        """Open safely; return True only when a second request means quit now."""

        if self.visible:
            return True
        self.visible = True
        self.selected_yes = False
        return False

    def handle_key(self, key: bytes) -> str:
        """Update the selection and return stay, cancel, or quit."""

        if key in {b"\t", b"\x1b[D", b"\x1b[C"}:
            self.selected_yes = not self.selected_yes
            return "stay"
        if key in {b"y", b"Y"}:
            return "quit"
        if key in {b"n", b"N", b"\x1b"}:
            self.visible = False
            self.selected_yes = False
            return "cancel"
        if key in {b"\r", b"\n"}:
            if self.selected_yes:
                return "quit"
            self.visible = False
            return "cancel"
        return "stay"


def read_terminal_key(input_descriptor: int) -> bytes:
    """Read one key, joining the three bytes used by left/right arrows."""

    key = os.read(input_descriptor, 1)
    if key != b"\x1b":
        return key

    suffix = b""
    deadline = time.monotonic() + 0.03
    while len(suffix) < 2:
        remaining = max(0.0, deadline - time.monotonic())
        if not select.select([input_descriptor], [], [], remaining)[0]:
            break
        chunk = os.read(input_descriptor, 1)
        if not chunk:
            break
        suffix += chunk
    return key + suffix


def quit_confirmation_lines(
    width: int, color: bool, selected_yes: bool = False
) -> list[str]:
    """Render a narrow-safe dialog whose selection is clear without color."""

    terminal_width = max(4, width)
    dialog_width = min(
        58, terminal_width - 4 if terminal_width >= 24 else terminal_width
    )
    inner_width = max(1, dialog_width - 4)
    title = crop(" Confirm quit ", dialog_width - 2)
    top = (
        "┌"
        + title
        + "─" * max(0, dialog_width - terminal_cell_width(title) - 2)
        + "┐"
    )
    bottom = "└" + "─" * max(0, dialog_width - 2) + "┘"

    def row(content: str = "") -> str:
        content = crop(content, inner_width)
        return f"│ {content}{' ' * max(0, inner_width - terminal_cell_width(content))} │"

    question = textwrap.wrap(
        "Are you sure you want to quit?", width=inner_width
    ) or [""]
    explanation_text = (
        "Ctrl-C twice quits now."
        if dialog_width < 24
        else "Press Ctrl-C twice to quit immediately."
    )
    controls_text = (
        "y/n · ←/→/Tab · Enter/Esc"
        if dialog_width < 24
        else "y/n · arrows/Tab · Enter · Esc"
    )
    explanation = textwrap.wrap(explanation_text, width=inner_width) or [""]
    controls = textwrap.wrap(controls_text, width=inner_width) or [""]
    yes = "> Yes <" if selected_yes else "  Yes  "
    no = "  No  " if selected_yes else "> No <"
    choices = f"{yes}  {no}"
    choice_rows = (
        [choices]
        if terminal_cell_width(choices) <= inner_width
        else [yes.center(inner_width), no.center(inner_width)]
    )
    lines = [
        top,
        row(),
        *(row(part) for part in question),
        row(),
        *(row(choice.center(inner_width)) for choice in choice_rows),
        row(),
        *(row(part) for part in explanation),
        *(row(part) for part in controls),
        row(),
        bottom,
    ]
    if not color:
        return lines

    selected = "> Yes <" if selected_yes else "> No <"
    styled: list[str] = []
    for line in lines:
        if selected in line:
            before, marker, after = line.partition(selected)
            line = (
                before
                + f"{ANSI['inverse']}{ANSI['bold']}{marker}{ANSI['reset']}"
                + after
            )
        styled.append(f"{ANSI['blue']}{line}{ANSI['reset']}")
    return styled


def render_quit_confirmation(
    screen: str,
    width: int,
    height: int,
    color: bool,
    selected_yes: bool = False,
) -> str:
    """Center the dialog over a subdued copy of the current screen."""

    background = [
        crop(ANSI_ESCAPE.sub("", line), width)
        for line in screen.splitlines()[:height]
    ]
    background.extend("" for _ in range(max(0, height - len(background))))
    if color:
        background = [f"{ANSI['dim']}{line}{ANSI['reset']}" for line in background]
    dialog = quit_confirmation_lines(width, color, selected_yes)
    start = max(0, (height - len(dialog)) // 2)
    visible_dialog_width = max(
        terminal_cell_width(ANSI_ESCAPE.sub("", line)) for line in dialog
    )
    left = max(0, (width - visible_dialog_width) // 2)
    for offset, line in enumerate(dialog):
        target = start + offset
        if target >= height:
            break
        replacement = " " * left + line
        if target < len(background):
            background[target] = replacement
        else:
            background.append(replacement)
    return "\n".join(background[:height])


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
    display_notice: str | None = None,
    search: str = "",
    worker_count: int = 0,
    repository_context: str | None = None,
    discovered: bool = False,
    discovery_mode: DiscoveryMode | None = None,
    expanded_header: bool = False,
    usage_report: LiveUsageSnapshot | None = None,
    usage_sessions: Iterable[tuple[str, str]] | None = None,
    usage_contexts: Iterable[Mapping[str, Any]] | None = None,
    usage_session_cadence: float = 180.0,
    usage_block_cadence: float = 10.0,
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
    missing = False
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
        missing = root_is_missing(root)
        gone = "folder is gone · " if missing else ""
        count = activity_count(records, int(time.time() * 1000))
        meter = activity_meter(count, count)
        watching = crop(f" Watching {gone}{display_root(root)} {meter}", width)
    if expanded_header or (root_count == 1 and missing):
        output.append(
            f"{ANSI['dim']}{watching}{ANSI['reset']}" if color else watching
        )
    if expanded_header and discovery_mode is not None:
        output.append(render_discovery_mode(discovery_mode, width, color))
    if github_status:
        output.append(render_github_banner(github_status, width, color))
    context_banners = render_context_banners(
        banner_identities, git_status if root_count == 1 else None, width, color
    )
    output.extend(context_banners)
    if display_notice and not show_help:
        output.extend(render_display_notice(display_notice, width, color))
    if not show_help and usage_report is not None and (
        usage_report.today.samples
        or usage_report.history.samples
        or usage_report.block.status in {"available", "stale"}
        or expanded_header
    ):
        output.extend(
            render_usage_banner(
                usage_report,
                records,
                banner_identities,
                width,
                color,
                usage_sessions,
                expanded=expanded_header,
                root_count=1 if focused_root_label else root_count,
                contexts=usage_contexts,
                session_cadence=usage_session_cadence,
                block_cadence=usage_block_cadence,
                max_lines=max(
                    3,
                    height
                    - len(output)
                    - len(
                        render_footer(
                            width,
                            color,
                            root_count=root_count,
                            expanded_history=expanded_history,
                            paused=paused,
                            focused_root_label=focused_root_label,
                        )
                    )
                    - 3,
                ),
            ).splitlines()
        )
    if show_help:
        output.extend(
            render_help(
                width,
                color,
                newest_first,
                root_count,
                expanded_history=expanded_history,
                event_filter=event_filter,
                paused=paused,
                focused_root_label=focused_root_label,
                expanded_header=expanded_header,
            )
        )
        footer = crop(" ? / Esc close help · q quit ", width)
        output.append(f"{ANSI['dim']}{footer}{ANSI['reset']}" if color else footer)
        return "\n".join(output[:height])
    footer = render_footer(
        width,
        color,
        root_count=root_count,
        expanded_history=expanded_history,
        paused=paused,
        focused_root_label=focused_root_label,
    )
    available = max(1, height - len(output) - 1 - len(footer))
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
    output.extend(footer)
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
    opencode_streams: dict[str, OpenCodeStream] = field(default_factory=dict)
    opencode_baseline_ms: int = 0
    cline_streams: dict[str, ClineStream] = field(default_factory=dict)
    present: bool = True
    baselined: bool = False
    scan_seconds: float = 0.0
    workers: list[str] = field(default_factory=list)
    usage_sessions: set[tuple[str, str]] = field(default_factory=set)
    usage_contexts: dict[tuple[str, str], dict[str, str]] = field(
        default_factory=dict
    )
    delivery_context_reset: bool = False


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
            session_id = identity.get("session_id")
            identity_key = (
                identity.get("pane_id")
                or (
                    agent_session_key(identity.get("agent"), session_id)
                    if session_id
                    else ""
                )
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
    usage_report: LiveUsageSnapshot | None = None,
    usage_sessions: Iterable[tuple[str, str]] | None = None,
    usage_contexts: Iterable[Mapping[str, Any]] | None = None,
    usage_session_cadence: float = 180.0,
    usage_block_cadence: float = 10.0,
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
    if usage_report is not None and (
        usage_report.today.samples
        or usage_report.history.samples
        or usage_report.block.status in {"available", "stale"}
        or expanded_header
    ):
        usage = render_usage_banner(
            usage_report,
            records,
            banner_identities,
            max(1, width - 2),
            color,
            state.usage_sessions if usage_sessions is None else usage_sessions,
            expanded=expanded_header,
            contexts=(
                state.usage_contexts.values()
                if usage_contexts is None
                else usage_contexts
            ),
            session_cadence=usage_session_cadence,
            block_cadence=usage_block_cadence,
            max_lines=max(3, height - len(output) - 3),
        )
        output.extend(f"│ {line.strip()}" for line in usage.splitlines())

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
    discovery_mode: DiscoveryMode | None = None,
    expanded_header: bool = False,
    usage_report: LiveUsageSnapshot | None = None,
    usage_sessions_by_root: Mapping[
        str, Iterable[tuple[str, str]]
    ] | None = None,
    usage_contexts_by_root: Mapping[
        str, Iterable[Mapping[str, Any]]
    ] | None = None,
    usage_session_cadence: float = 180.0,
    usage_block_cadence: float = 10.0,
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
    output = [styled_heading if color else heading]
    if expanded_header:
        output.append(
            crop(
                f" Watching {len(states)}"
                f"{' found' if discovered else ''} folders · {agent_count} {noun}"
                f"{worker_notice(len({name for s in states for name in s.workers}))}",
                width,
            )
        )
        if discovery_mode is not None:
            output.append(render_discovery_mode(discovery_mode, width, color))
    if display_notice:
        output.extend(render_display_notice(display_notice, width, color))
    footer = render_footer(
        width,
        color,
        root_count=len(states),
        expanded_history=expanded_history,
        paused=paused,
    )
    column_height = max(4, height - len(output) - len(footer))
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
                usage_report=usage_report,
                usage_sessions=(
                    usage_sessions_by_root.get(os.fspath(state.root), ())
                    if usage_sessions_by_root is not None
                    else state.usage_sessions
                ),
                usage_contexts=(
                    usage_contexts_by_root.get(os.fspath(state.root), ())
                    if usage_contexts_by_root is not None
                    else state.usage_contexts.values()
                ),
                usage_session_cadence=usage_session_cadence,
                usage_block_cadence=usage_block_cadence,
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
    output.extend(footer)
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
    history, position = read_new_events(path, 0, root)
    records: deque[dict[str, Any]] = deque(history[-200:], maxlen=500)
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
        position=position,
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
        # Always perform one initial readback. Later reads use adaptive
        # intervals based on whether the branch has an active PR.
        last_github_refresh=float("-inf"),
        usage_sessions=set(usage_session_keys(history, {})),
        usage_contexts={},
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
    """Where Pi keeps its session files, honouring PI_CODING_AGENT_DIR.

    Pi's config directory is `~/.pi/agent` unless PI_CODING_AGENT_DIR overrides
    it, and sessions live directly under it. A customised install is invisible
    otherwise.
    """
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    agent_dir = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".pi" / "agent"
    )
    return agent_dir / "sessions"


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


DSH_SESSION_HEADERS: dict[str, dict[str, Any]] = {}
DEEPSEEK_LISTING_TTL_SECONDS = 2.0
DEEPSEEK_LISTING_CACHE: dict[str, tuple[float, list[tuple[Path, float]]]] = {}


def dsh_sessions_root() -> Path:
    """Where DeepSeek Harness keeps sessions, honouring DSH_HOME."""
    configured = os.environ.get("DSH_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".dsh"
    return home / "sessions"


def dsh_session_header(path: Path) -> dict[str, Any]:
    """Read the immutable first record from raw or compressed Harness logs."""
    key = os.fspath(path)
    cached = DSH_SESSION_HEADERS.get(key)
    if cached is not None:
        return cached
    records, _ = _dsh_records(path, 0, limit_chunks=1)
    if not records or records[0].get("type") != "session":
        return {}
    DSH_SESSION_HEADERS[key] = records[0]
    return records[0]


def deepseek_session_listing() -> list[tuple[Path, float]]:
    """Every Harness session artifact with its modification time."""
    root = os.fspath(dsh_sessions_root())
    cached = DEEPSEEK_LISTING_CACHE.get(root)
    now = time.monotonic()
    if cached is not None and now - cached[0] < DEEPSEEK_LISTING_TTL_SECONDS:
        return cached[1]
    listing: list[tuple[Path, float]] = []
    try:
        candidates = [
            path
            for path in dsh_sessions_root().rglob("session.jsonl*")
            if path.name in {"session.jsonl", "session.jsonl.zstd"}
        ]
    except OSError:
        candidates = []
    for path in candidates:
        try:
            listing.append((path, path.stat().st_mtime))
        except OSError:
            continue
    DEEPSEEK_LISTING_CACHE[root] = (now, listing)
    return listing


def deepseek_recent_sessions(deadline: float) -> list[tuple[Path, float]]:
    recent = [item for item in deepseek_session_listing() if item[1] >= deadline]
    recent.sort(key=lambda item: item[1])
    return recent


def load_deepseek_session_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Top-level DeepSeek Harness agents working in this repository."""
    moment = now if now is not None else time.time()
    watched_common = git_common_dir(os.fspath(root))
    identities: dict[str, dict[str, str]] = {}
    for path, changed in deepseek_recent_sessions(
        moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = dsh_session_header(path)
        if header.get("origin") == "subagent":
            continue
        cwd = header.get("cwd")
        session_id = header.get("id")
        if not isinstance(cwd, str) or not cwd:
            continue
        if not isinstance(session_id, str) or not session_id:
            continue
        _remember_session_path(
            SESSION_PATH_CACHE, f"deepseek:{session_id}", path
        )
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
        identities[session_id] = {
            "agent": "deepseek",
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
            "label": f"DeepSeek · {where}" if where else agent_label("deepseek"),
            "session_id": session_id,
            **load_deepseek_metadata(session_id),
        }
    return identities


ANTIGRAVITY_SESSION_HEADERS: dict[
    str, tuple[int, int, dict[str, Any]]
] = {}


def antigravity_sessions_roots() -> list[Path]:
    """Where Antigravity keeps its app data and sessions."""
    configured = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
    if configured:
        return [Path(configured).expanduser()]
    gemini_home = os.environ.get("GEMINI_HOME")
    base = Path(gemini_home).expanduser() if gemini_home else Path.home() / ".gemini"
    nested = [
        base / "antigravity-cli",
        base / "antigravity",
        base / "antigravity-ide",
    ]
    candidates = ([base] if (base / "brain").is_dir() else []) + [
        path for path in nested if path.is_dir()
    ]
    return candidates or [nested[0]]


def _antigravity_app_root(path: Path) -> Path | None:
    for parent in path.parents:
        if parent.name == "brain":
            return parent.parent
    return None


def antigravity_history(app_root: Path) -> dict[str, dict[str, Any]]:
    """Latest workspace record for each CLI conversation.

    Antigravity's transcript deliberately omits its workspace. The adjacent
    ``history.jsonl`` is the CLI-owned index that joins a conversation ID to
    that folder. Only that metadata is retained here; prompts and responses
    remain in Antigravity's files.
    """
    path = app_root / "history.jsonl"
    key = os.fspath(path)
    try:
        stat = path.stat()
    except OSError:
        return {}
    cached = ANTIGRAVITY_HISTORY_CACHE.get(key)
    if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
        return cached[2]
    records: dict[str, dict[str, Any]] = {}
    try:
        with path.open("rb") as handle:
            for raw_line in transcript_lines(handle):
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                session_id = record.get("conversationId")
                workspace = record.get("workspace")
                if not isinstance(session_id, str) or not session_id:
                    continue
                if not isinstance(workspace, str) or not workspace:
                    continue
                timestamp = record.get("timestamp")
                prior = records.get(session_id)
                if prior is not None and isinstance(timestamp, (int, float)):
                    prior_timestamp = prior.get("timestamp")
                    if isinstance(prior_timestamp, (int, float)) and timestamp < prior_timestamp:
                        continue
                records[session_id] = {
                    "workspace": _codex_cwd(workspace),
                    "timestamp": timestamp,
                }
    except OSError:
        return {}
    ANTIGRAVITY_HISTORY_CACHE[key] = (stat.st_mtime_ns, stat.st_size, records)
    return records


def antigravity_session_header(path: Path) -> dict[str, Any]:
    """Extract privacy-safe identity metadata for one Antigravity transcript."""
    key = os.fspath(path)
    session_id = ""
    for part in path.parts:
        if re.fullmatch(r"[0-9a-fA-F-]{32,40}", part):
            session_id = part
            break
    app_root = _antigravity_app_root(path)
    history = antigravity_history(app_root) if app_root is not None else {}
    history_record = history.get(session_id, {})
    history_timestamp = history_record.get("timestamp")
    fingerprint_timestamp = (
        int(history_timestamp) if isinstance(history_timestamp, (int, float)) else 0
    )
    try:
        transcript_mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    cached = ANTIGRAVITY_SESSION_HEADERS.get(key)
    if cached is not None and cached[:2] == (
        transcript_mtime,
        fingerprint_timestamp,
    ):
        return cached[2]
    cwd = str(history_record.get("workspace") or "")
    model = ""
    effort = ""
    subagents: list[str] = []
    try:
        with path.open("rb") as handle:
            for _ in range(64):
                raw_line = handle.readline()
                if not raw_line:
                    break
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if not session_id:
                    sid = (
                        record.get("conversationId")
                        or record.get("session_id")
                        or record.get("conversation_id")
                    )
                    if isinstance(sid, str) and sid:
                        session_id = sid
                if not cwd:
                    paths = record.get("workspacePaths") or record.get("workspace_paths")
                    if isinstance(paths, list) and paths and isinstance(paths[0], str):
                        cwd = paths[0]
                    elif isinstance(record.get("cwd"), str):
                        cwd = record["cwd"]
                if not model:
                    m = (
                        record.get("model")
                        or record.get("model_name")
                        or record.get("modelName")
                    )
                    if isinstance(m, str) and m:
                        model = m
                if not effort:
                    e = (
                        record.get("effort")
                        or record.get("thinking_level")
                        or record.get("thinkingLevel")
                    )
                    if isinstance(e, str) and e:
                        effort = e
                if record.get("type") == "PLANNER_RESPONSE":
                    calls = record.get("tool_calls") or record.get("toolCalls") or []
                    for call in calls:
                        if isinstance(call, dict):
                            args = (
                                call.get("toolArgs")
                                or call.get("parameters")
                                or call.get("args")
                                or {}
                            )
                            if not cwd and isinstance(args, dict):
                                if isinstance(args.get("Cwd"), str):
                                    cwd = args["Cwd"]
                                elif isinstance(args.get("TargetFile"), str):
                                    target = Path(args["TargetFile"]).expanduser()
                                    if target.is_absolute():
                                        cwd = os.fspath(target.parent)
                            call_name = call.get("toolName") or call.get("name")
                            if call_name == "invoke_subagent" and isinstance(args, dict):
                                raw_subagents = args.get("Subagents") or args.get(
                                    "subagents"
                                )
                                if not isinstance(raw_subagents, list):
                                    raw_subagents = [args]
                                for sub in raw_subagents:
                                    if isinstance(sub, dict):
                                        role = (
                                            sub.get("Role")
                                            or sub.get("role")
                                            or sub.get("TypeName")
                                            or sub.get("typeName")
                                        )
                                        if isinstance(role, str) and role:
                                            subagents.append(role)
    except OSError:
        return {}
    header = {
        "id": session_id,
        "session_id": session_id,
        "cwd": cwd,
        "model": model,
        "effort": effort,
        "subagents": subagents,
    }
    ANTIGRAVITY_SESSION_HEADERS[key] = (
        transcript_mtime,
        fingerprint_timestamp,
        header,
    )
    return header


def antigravity_session_listing() -> list[tuple[Path, float]]:
    """Every Antigravity transcript file with its modification time."""
    now = time.monotonic()
    all_listings: list[tuple[Path, float]] = []
    for app_root in antigravity_sessions_roots():
        root_str = os.fspath(app_root)
        cached = ANTIGRAVITY_LISTING_CACHE.get(root_str)
        if cached is not None and now - cached[0] < ANTIGRAVITY_LISTING_TTL_SECONDS:
            all_listings.extend(cached[1])
            continue
        listing: list[tuple[Path, float]] = []
        try:
            candidates = list((app_root / "brain").rglob("transcript.jsonl"))
        except OSError:
            candidates = []
        for path in candidates:
            try:
                listing.append((path, path.stat().st_mtime))
            except OSError:
                continue
        ANTIGRAVITY_LISTING_CACHE[root_str] = (now, listing)
        all_listings.extend(listing)
    return all_listings


def antigravity_recent_sessions(deadline: float) -> list[tuple[Path, float]]:
    recent = [item for item in antigravity_session_listing() if item[1] >= deadline]
    recent.sort(key=lambda item: item[1])
    return recent


def _antigravity_transcript_workers(path: Path) -> set[str]:
    """Incrementally collect worker roles without retaining transcript content."""
    key = os.fspath(path)
    position, roles = ANTIGRAVITY_WORKER_CACHE.get(key, (0, set()))
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            if position > size:
                position, roles = 0, set()
            handle.seek(position)
            for raw_line in transcript_lines(handle):
                if b'"invoke_subagent"' not in raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                calls = record.get("tool_calls") or record.get("toolCalls") or []
                if not isinstance(calls, list):
                    continue
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    if (call.get("name") or call.get("toolName")) != "invoke_subagent":
                        continue
                    roles.update(_antigravity_subagent_roles(_antigravity_call_args(call)))
            position = handle.tell()
    except OSError:
        return set(roles)
    ANTIGRAVITY_WORKER_CACHE[key] = (position, roles)
    return set(roles)


def antigravity_workers(root: Path, now: float | None = None) -> list[str]:
    """Names of the worker subagents an Antigravity session has running in this repo."""
    deadline = (
        now if now is not None else time.time()
    ) - ANTIGRAVITY_SUBAGENT_WINDOW_SECONDS
    common = git_common_dir(os.fspath(root))
    names: list[str] = []
    for path, _ in antigravity_recent_sessions(deadline):
        header = antigravity_session_header(path)
        cwd = header.get("cwd")
        if not isinstance(cwd, str):
            continue
        try:
            same_folder = canonical_root(cwd) == root
            if not same_folder and (not common or git_common_dir(cwd) != common):
                continue
        except OSError:
            continue
        names.extend(_antigravity_transcript_workers(path))
    return sorted(set(names))


def load_antigravity_session_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Antigravity agents working in this folder."""
    moment = now if now is not None else time.time()
    watched_common = git_common_dir(os.fspath(root))
    identities: dict[str, dict[str, str]] = {}
    for path, changed in antigravity_recent_sessions(
        moment - ANTIGRAVITY_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = antigravity_session_header(path)
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
        label = " · ".join(part for part in ("Antigravity", where) if part)
        identities[session_id] = {
            "agent": "antigravity",
            "root": os.fspath(associated),
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "working_root": os.fspath(session_root),
            "status": (
                "working"
                if changed >= moment - ANTIGRAVITY_SESSION_WORKING_SECONDS
                else "idle"
            ),
            "label": label or agent_label("antigravity"),
            "session_id": session_id,
            "model": header.get("model", ""),
            "effort": header.get("effort", ""),
            **load_antigravity_metadata(session_id),
        }
    return identities


def load_agent_identities(
    root: Path, now: float | None = None
) -> dict[str, dict[str, str]]:
    """Everyone working in this folder, from every source that knows.

    Herdr sees terminal panes. Claude registers every live session whatever
    surface launched it, desktop app included. Codex, Pi, DeepSeek, and
    Antigravity each leave a session artifact per run, while Opencode and Cline
    keep shared stores. T3 Code adds launch context and supplies projected
    activity for Cursor and Grok. Herdr wins where two sources describe one
    provider's session: it alone knows the pane, tab and window, and a session
    file does not. Provider-scoped keys keep unrelated agents with
    coincidentally equal external ids apart.
    """
    moment = time.time() if now is None else now
    identities: dict[str, dict[str, str]] = {}
    known: set[str] = set()
    for source_key, identity in load_herdr_identities(root).items():
        identity = _t3code_enrich_identity(
            identity, keep_label=True, now=moment
        )
        session_id = identity.get("session_id")
        if session_id:
            typed_identity = AgentIdentity.from_wire(identity)
            identity = typed_identity.to_wire()
            key = typed_identity.key.to_wire()
            known.add(key)
            identities[key] = identity
        if source_key.startswith("pane:") or not session_id:
            identities[source_key] = identity
    for integration in INTEGRATIONS:
        source = integration.identity_loader(root, now)
        for session_id, identity in source.items():
            identity = _t3code_enrich_identity(identity, now=moment)
            typed_identity = AgentIdentity.from_wire(
                identity,
                key=SessionKey(identity.get("agent"), session_id),
            )
            key = typed_identity.key.to_wire()
            if key in known:
                continue
            known.add(key)
            identities[key] = typed_identity.to_wire()
    return identities


def worktree_root_for(path: str) -> Path | None:
    """The worktree an agent's working folder belongs to, or the folder itself."""
    try:
        folder = canonical_root(path)
        reported = git_worktree_root(os.fspath(folder))
        return canonical_root(reported) if reported else folder
    except OSError:
        return None


def claude_working_folders(moment: float) -> list[tuple[Any, bool]]:
    folders = []
    for record in claude_session_registry():
        session_id = str(record.get("sessionId") or "")
        folders.append(
            (
                record.get("cwd"),
                claude_session_status(session_id, moment) == "working",
            )
        )
    return folders


def codex_working_folders(moment: float) -> list[tuple[Any, bool]]:
    folders = []
    for path, changed in codex_recent_sessions(
        moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = codex_session_header(path)
        if header.get("thread_source") in CODEX_HELPER_THREAD_SOURCES:
            continue
        folders.append(
            (header.get("cwd"), changed >= moment - CODEX_SESSION_WORKING_SECONDS)
        )
    return folders


def deepseek_working_folders(moment: float) -> list[tuple[Any, bool]]:
    folders = []
    for path, changed in deepseek_recent_sessions(
        moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
    ):
        header = dsh_session_header(path)
        if header.get("origin") == "subagent":
            continue
        folders.append(
            (header.get("cwd"), changed >= moment - CODEX_SESSION_WORKING_SECONDS)
        )
    return folders


def pi_working_folders(moment: float) -> list[tuple[Any, bool]]:
    return [
        (
            pi_session_header(path).get("cwd"),
            changed >= moment - CODEX_SESSION_WORKING_SECONDS,
        )
        for path, changed in pi_recent_sessions(
            moment - CODEX_SESSION_IDENTITY_WINDOW_SECONDS
        )
    ]


def antigravity_working_folders(moment: float) -> list[tuple[Any, bool]]:
    return [
        (
            antigravity_session_header(path).get("cwd"),
            changed >= moment - ANTIGRAVITY_SESSION_WORKING_SECONDS,
        )
        for path, changed in antigravity_recent_sessions(
            moment - ANTIGRAVITY_SESSION_IDENTITY_WINDOW_SECONDS
        )
    ]


def opencode_working_folders(moment: float) -> list[tuple[Any, bool]]:
    listing = opencode_session_listing()
    effective = _opencode_effective_updated(listing)
    folders = []
    for record in listing:
        if record.get("parent_id"):
            continue
        age = moment - effective[record["id"]] / 1000
        if age > OPENCODE_SESSION_IDENTITY_WINDOW_SECONDS:
            continue
        folders.append(
            (record.get("directory"), age <= OPENCODE_SESSION_WORKING_SECONDS)
        )
    return folders


def crush_working_folders(moment: float) -> list[tuple[Any, bool]]:
    folders: list[tuple[Any, bool]] = []
    for project, sessions in crush_session_listing():
        _records, trees = _crush_session_trees(sessions)
        for members in trees.values():
            newest = max((record.updated_epoch_ms for record in members), default=0)
            age = moment - newest / 1000 if newest else float("inf")
            unfinished = any(not record.finished for record in members)
            if age > CRUSH_SESSION_IDENTITY_WINDOW_SECONDS:
                continue
            folders.append(
                (
                    os.fspath(project.path),
                    unfinished and age <= CRUSH_SESSION_WORKING_SECONDS,
                )
            )
    return folders


def cline_working_folders(moment: float) -> list[tuple[Any, bool]]:
    listing = cline_session_listing()
    effective = _cline_effective_updated(listing)
    folders = []
    for record in listing:
        if record.get("parent_id") or record.get("is_subagent"):
            continue
        age = moment - effective[record["id"]] / 1000
        active = (
            record.get("status") in CLINE_NON_TERMINAL_STATUSES
            and process_is_alive(record.get("pid"))
        )
        if age > CLINE_SESSION_IDENTITY_WINDOW_SECONDS and not active:
            continue
        folders.append(
            (
                record.get("directory"),
                active and age <= CLINE_SESSION_WORKING_SECONDS,
            )
        )
    return folders


def t3code_working_folders(provider: str, moment: float) -> list[tuple[Any, bool]]:
    return [
        (record.working_root, _t3code_session_status(record, moment) == "working")
        for record in t3code_session_listing()
        if record.provider == provider
        and record.native_session_id
        and _t3code_recent_session(record, moment)
    ]


def cursor_working_folders(moment: float) -> list[tuple[Any, bool]]:
    return t3code_working_folders("cursor", moment)


def grok_working_folders(moment: float) -> list[tuple[Any, bool]]:
    return t3code_working_folders("grok", moment)


def agent_working_folders(now: float | None = None) -> dict[Path, bool]:
    """Every folder a coding agent is working in right now, anywhere on this
    machine, mapped to whether that agent is working this minute.

    load_agent_identities() answers a different question - who is working in
    one folder Side Dog is already watching - so it filters everything by that
    folder's repository and cannot be asked what to watch in the first place.
    This puts the same native sources the same questions and keeps the folder
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
    for integration in INTEGRATIONS:
        for raw, working in integration.working_folders_loader(moment):
            remember(raw, working)
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
    delivery_context: dict[str, Any] | None = None
    workers: list[str] | None = None


def load_watch_root_external_refresh(
    root: Path,
    refresh_herdr: bool,
    refresh_github: bool,
    github_branch: str | None = None,
    delivery_context: dict[str, Any] | None = None,
) -> WatchRootExternalRefresh:
    workers = None
    if refresh_herdr:
        workers = sorted(set(codex_workers(root) + antigravity_workers(root)))
    return WatchRootExternalRefresh(
        identities=load_agent_identities(root) if refresh_herdr else None,
        github_result=load_github_pr(root) if refresh_github else None,
        github_branch=github_branch,
        delivery_context=delivery_context,
        workers=workers,
    )


def apply_watch_root_external_refresh(
    state: WatchRootState,
    refresh: WatchRootExternalRefresh,
    delivery_context: dict[str, Any] | None = None,
) -> None:
    if refresh.identities is not None:
        state.identities = refresh.identities
        state.usage_sessions.update(
            usage_session_keys((), refresh.identities, state.root)
        )
        state.usage_contexts = refreshed_usage_contexts(
            state.usage_contexts, refresh.identities, state.root
        )
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
                    (
                        delivery_context
                        if delivery_context is not None
                        else refresh.delivery_context
                        if refresh.delivery_context is not None
                        else latest_delivery_context(state.records)
                    ),
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
        refresh_github = github_poll > 0 and github_refresh_due(
            state.github_status,
            state.last_github_refresh,
            now,
            github_poll,
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
            (
                {}
                if state.delivery_context_reset
                else latest_delivery_context(state.records)
            ),
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
    notify: bool = True,
) -> int:
    branch_changed = False
    new_records, state.position = read_new_events(state.path, state.position, state.root)
    state.usage_sessions.update(usage_session_keys(new_records, {}))
    for record in new_records:
        state.records.append(record)
        if (
            record.get("kind") == "branch"
            and record.get("title") == "Branch switched"
        ):
            state.delivery_context_reset = False
        if record.get("kind") in {"file", "config"}:
            state.last_hook_writes[str(record.get("detail", ""))] = now
        if record.get("kind") in {"pr", "merge"}:
            state.last_github_refresh = float("-inf")
        if notify:
            notify_for_event(display_root(state.root), record)
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
                state.delivery_context_reset = True
                state.github_status = None
                state.last_github_fingerprint = None
                state.last_github_refresh = float("-inf")
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
        and github_refresh_due(
            state.github_status,
            state.last_github_refresh,
            now,
            github_poll,
        )
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
                (
                    {}
                    if state.delivery_context_reset
                    else latest_delivery_context(state.records)
                ),
            ),
            delivery_context={} if state.delivery_context_reset else None,
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
    github_poll: float = DEFAULT_GITHUB_POLL_SECONDS,
    once: bool = False,
    follow_worktrees: bool = True,
    save_space_as: str | None = None,
    follow_herdr: bool = False,
    require_herdr: bool = False,
    no_notify: bool = False,
) -> int:
    configuration = load_config()
    notify_enabled = not no_notify and config_notify_enabled(configuration)
    limit = config_limit(configuration, WATCH_ROOT_LIMIT)
    ignore = config_ignores(configuration)
    named = resolve_watch_arguments(
        [projects] if isinstance(projects, (str, os.PathLike)) else list(projects)
    )
    discovery_mode = folder_discovery_mode(
        explicit_roots=bool(named),
        follow_herdr=follow_herdr,
        require_herdr=require_herdr,
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
    # The file is where preferences start; the E, e, f and r keys still write to
    # display.json, so what was pressed last wins over what was written down.
    remembered = {**config_display(configuration), **saved}
    expanded_header = bool(remembered.get("expanded_header", False))
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
    paused_usage_report: LiveUsageSnapshot | None = None
    paused_usage_sessions: dict[str, frozenset[tuple[str, str]]] | None = None
    paused_usage_contexts: dict[str, tuple[dict[str, str], ...]] | None = None
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
    poll_coordinator = create_poll_coordinator()
    usage_monitor = UsageMonitor()
    if once:
        usage_monitor.report = load_ccusage("session")
        usage_monitor.today_report = load_ccusage(
            "session", since=datetime.now().astimezone().date().isoformat()
        )
        usage_monitor.block = load_ccusage_block()
    else:
        usage_monitor.tick()
    stdout_is_terminal = sys.stdout.isatty()
    color = not no_color and stdout_is_terminal
    interactive = stdout_is_terminal and not once
    quit_confirmation = QuitConfirmation()

    def interrupt(_signum: int, _frame: Any) -> None:
        nonlocal running
        if not interactive or quit_confirmation.request():
            running = False

    def terminate(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, terminate)
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
                    key = (
                        read_terminal_key(input_descriptor)
                        if quit_confirmation.visible
                        else os.read(input_descriptor, 1)
                    )
                    if quit_confirmation.visible:
                        decision = quit_confirmation.handle_key(key)
                        if decision == "quit":
                            running = False
                        continue
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
                            expanded_header=expanded_header,
                            event_filter=FILTER_ORDER[event_filter_index],
                        )
                        display_notice.show(
                            expanded_history_notice(expanded_history),
                            time.monotonic(),
                        )
                    elif key == b"E" and not show_help:
                        expanded_header = expanded_header_for_key(
                            key, expanded_header
                        )
                        save_display_settings(
                            newest_first=newest_first,
                            expanded_history=expanded_history,
                            expanded_header=expanded_header,
                            event_filter=FILTER_ORDER[event_filter_index],
                        )
                        display_notice.show(
                            expanded_header_notice(expanded_header),
                            time.monotonic(),
                        )
                    elif key == b"f" and not show_help:
                        event_filter_index = (event_filter_index + 1) % len(
                            FILTER_ORDER
                        )
                        save_display_settings(
                            newest_first=newest_first,
                            expanded_history=expanded_history,
                            expanded_header=expanded_header,
                            event_filter=FILTER_ORDER[event_filter_index],
                        )
                        display_notice.show(
                            event_filter_notice(FILTER_ORDER[event_filter_index]),
                            time.monotonic(),
                        )
                    elif key == b"q":
                        quit_confirmation.request()
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
                            expanded_header=expanded_header,
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
                                discovery_mode=discovery_mode,
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
                            paused_usage_report = usage_monitor.snapshot
                            paused_usage_sessions = {
                                os.fspath(state.root): frozenset(
                                    state.usage_sessions
                                )
                                for state in states
                            }
                            paused_usage_contexts = {
                                os.fspath(state.root): tuple(
                                    dict(context)
                                    for context in state.usage_contexts.values()
                                )
                                for state in states
                            }
                            paused_new_count = 0
                            paused_new_counts = {
                                os.fspath(state.root): 0 for state in states
                            }
                        else:
                            paused_records = None
                            paused_usage_report = None
                            paused_usage_sessions = None
                            paused_usage_contexts = None
                            paused_new_count = 0
                            paused_new_counts = {}
                        display_notice.show(
                            pause_notice(paused_records is not None),
                            time.monotonic(),
                        )
            now = time.monotonic()
            usage_monitor.tick(now)
            # One folder sweeps the filesystem per pass. Eight big folders on
            # every pass meant seconds of walking between frames.
            due = folder_due_for_scan(states, now, poll)
            poll_targets = tuple(
                PollTarget.from_wire(state.root, state.identities) for state in states
            )
            poll_coordinator.tick(poll_targets)
            if not interactive:
                poll_coordinator.drain()
            new_counts = [
                poll_watch_root(
                    state,
                    now,
                    poll,
                    github_poll,
                    poll_external=refresh_executor is None,
                    scan_files=state is due,
                    notify=notify_enabled,
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
            fallback_width = width if width > 0 else 80
            terminal = shutil.get_terminal_size((fallback_width, 30))
            actual_width = (
                terminal.columns if width <= 0 else min(width, terminal.columns)
            )
            current_display_notice = display_notice.current(time.monotonic()) or (
                space_notice if once else None
            )
            live_usage_sessions = {
                os.fspath(state.root): state.usage_sessions for state in states
            }
            displayed_usage_report, displayed_usage_sessions = (
                usage_display_snapshot(
                    usage_monitor.snapshot,
                    live_usage_sessions,
                    paused_usage_report,
                    paused_usage_sessions,
                )
            )
            displayed_usage_contexts = (
                paused_usage_contexts
                if paused_usage_contexts is not None
                else {
                    os.fspath(state.root): tuple(state.usage_contexts.values())
                    for state in states
                }
            )
            visible_usage_contexts = tuple(
                context
                for index in selected_watch_indexes(
                    len(states), focused_root_index
                )
                for context in displayed_usage_contexts.get(
                    os.fspath(states[index].root), ()
                )
            )
            usage_session_cadence = float(
                usage_monitor.settings.get("session_refresh_seconds", 180.0)
            )
            usage_block_cadence = float(
                usage_monitor.settings.get("block_refresh_seconds", 10.0)
            )
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
                    discovery_mode=discovery_mode,
                    expanded_header=expanded_header,
                    usage_report=displayed_usage_report,
                    usage_sessions_by_root=displayed_usage_sessions,
                    usage_contexts_by_root=displayed_usage_contexts,
                    usage_session_cadence=usage_session_cadence,
                    usage_block_cadence=usage_block_cadence,
                )
            else:
                visible_usage_sessions = {
                    session
                    for index in selected_watch_indexes(
                        len(states), focused_root_index
                    )
                    for session in displayed_usage_sessions.get(
                        os.fspath(states[index].root), ()
                    )
                }
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
                    display_notice=current_display_notice,
                    search=search,
                    repository_context=watch_repository_context(
                        [states[focused_root_index]]
                        if focused_root_index is not None
                        and focused_root_index < len(states)
                        else states
                    ),
                    discovered=discovering,
                    discovery_mode=discovery_mode,
                    expanded_header=expanded_header,
                    usage_report=displayed_usage_report,
                    usage_sessions=visible_usage_sessions,
                    usage_contexts=visible_usage_contexts,
                    usage_session_cadence=usage_session_cadence,
                    usage_block_cadence=usage_block_cadence,
                )
            if quit_confirmation.visible:
                screen = render_quit_confirmation(
                    screen,
                    actual_width,
                    terminal.lines,
                    color,
                    quit_confirmation.selected_yes,
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
        poll_coordinator.close(wait=False)
        usage_monitor.close()
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
    return [sys.executable, "-m", "side_dog.cli"]


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
    discovery_mode: DiscoveryMode | None = None,
) -> WebPanel:
    """Serve the browser panel for the watched folders and open a window."""
    launch_roots = roots
    if follow_herdr and requested_roots is not None:
        launch_roots = [root for root in roots if root in requested_roots]
    command = [
        *side_dog_command(),
        "panel",
        "--no-notify",
        *(os.fspath(root) for root in launch_roots),
        *(["--herdr"] if follow_herdr else []),
        *(
            ["--discovery-mode", discovery_mode.key]
            if discovery_mode is not None
            else []
        ),
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


def demo_tour_samples() -> tuple[tuple[int, dict[str, Any]], ...]:
    writer = {
        "agent": "codex",
        "session_id": "synthetic-writer",
        "turn_id": "synthetic-delivery",
        "model": "demo-model",
        "effort": "demo",
    }
    reviewer = {
        "agent": "claude-code",
        "session_id": "synthetic-reviewer",
        "turn_id": "synthetic-review",
        "model": "demo-model",
        "effort": "demo",
    }
    verified = {
        "number": 47,
        "state": "OPEN",
        "ci": "CI 2/3",
        "review": "REVIEW_REQUIRED",
        "merge_state": "BLOCKED",
        "coverage": "OK",
    }
    return (
        (
            0,
            {
                **writer,
                "agent": "filesystem",
                "kind": "file",
                "status": "success",
                "title": "Wrote file",
                "detail": "tour/hello.py",
            },
        ),
        (
            0,
            {
                **writer,
                "operation_id": "synthetic-tests",
                "group_id": "synthetic-tests",
                "kind": "test",
                "status": "running",
                "title": "Running tests",
                "detail": "pytest",
            },
        ),
        (
            1,
            {
                **reviewer,
                "operation_id": "synthetic-lint",
                "group_id": "synthetic-lint",
                "kind": "test",
                "status": "failed",
                "title": "Tests failed",
                "detail": "one intentional demo failure",
            },
        ),
        (
            0,
            {
                **writer,
                "operation_id": "synthetic-tests",
                "group_id": "synthetic-tests",
                "kind": "test",
                "status": "success",
                "title": "Tests passed",
                "detail": "pytest",
            },
        ),
        (
            1,
            {
                **reviewer,
                "kind": "config",
                "status": "success",
                "title": "Wrote config",
                "detail": "pyproject.toml",
            },
        ),
        (
            0,
            {
                **writer,
                "kind": "commit",
                "status": "success",
                "title": "Commit created",
                "detail": "a1b2c3d · synthetic tour",
            },
        ),
        (
            0,
            {
                **writer,
                "kind": "push",
                "status": "success",
                "title": "Branch pushed",
                "detail": "origin",
            },
        ),
        (0, github_event(verified, None, writer)),
        (
            1,
            {
                **reviewer,
                "kind": "issue",
                "status": "success",
                "title": "Closed issue",
                "detail": "#47",
            },
        ),
    )


def demo_tour(
    view: str = "panel",
    *,
    duration: float = 12.0,
    open_window: bool = True,
) -> int:
    """Run a self-contained synthetic first-run tour and remove it afterward."""
    if view not in {"panel", "watch"}:
        raise ValueError(f"unknown demo view: {view}")
    process: subprocess.Popen[bytes] | None = None
    previous_state = os.environ.get(STATE_ENV)
    interrupted = False
    with tempfile.TemporaryDirectory(prefix="side-dog-tour-") as directory:
        temporary = Path(directory)
        roots = [temporary / "demo-build", temporary / "demo-review"]
        for root in roots:
            root.mkdir()
        isolated_state = temporary / "state"
        isolated_config = temporary / "config"
        os.environ[STATE_ENV] = os.fspath(isolated_state)
        environment = {
            **os.environ,
            STATE_ENV: os.fspath(isolated_state),
            CONFIG_HOME_ENV: os.fspath(isolated_config),
        }
        command = [
            *side_dog_command(),
            view,
            "--no-notify",
            *(os.fspath(root) for root in roots),
        ]
        if view == "panel":
            command.extend(["--poll", "0.1"])
            if not open_window:
                command.append("--no-open")
        else:
            command.extend(
                [
                    "--poll",
                    "0.1",
                    "--github-poll",
                    "0",
                    "--no-follow-worktrees",
                ]
            )
        print("Side Dog first-run tour — all displayed activity is synthetic.")
        print("Two isolated demo folders will show running, success, and failure states.")
        print("Press h to switch between the timeline and live highway views.")
        try:
            process = subprocess.Popen(command, env=environment)
            if process.poll() is not None:
                return process.returncode or 1
            samples = demo_tour_samples()
            delay = max(0.0, duration) / max(1, len(samples))
            for root_index, event in samples:
                exit_code = process.poll()
                if exit_code is not None:
                    print(
                        "side-dog: demo viewer exited before the tour completed",
                        file=sys.stderr,
                    )
                    return exit_code or 1
                append_event(roots[root_index], event)
                if delay:
                    time.sleep(delay)
                exit_code = process.poll()
                if exit_code is not None:
                    print(
                        "side-dog: demo viewer exited before the tour completed",
                        file=sys.stderr,
                    )
                    return exit_code or 1
        except OSError as error:
            print(f"side-dog: could not start demo {view}: {error}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            interrupted = True
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if previous_state is None:
                os.environ.pop(STATE_ENV, None)
            else:
                os.environ[STATE_ENV] = previous_state
    print("Synthetic tour complete; temporary activity was removed.")
    return 130 if interrupted else 0


def usage_report_command(
    view: str = "daily",
    *,
    since: str | None = None,
    until: str | None = None,
    agent: str | None = None,
    root: str | None = None,
    json_output: bool = False,
    no_cost: bool = False,
    cost_mode: str = "auto",
) -> int:
    """Print one privacy-filtered ccusage report without changing local state."""
    selected_root = Path(root).expanduser().resolve(strict=False) if root else None
    if selected_root is not None and view != "session":
        print(
            "ccusage cannot scope daily or monthly reports to one folder; "
            "use side-dog usage session --root <path>",
            file=sys.stderr,
        )
        return 2
    report = load_ccusage(
        view,
        since=since,
        until=until,
        mode=cost_mode,
        no_cost=no_cost,
    )
    samples = report.samples
    if agent:
        selected_agent = normalize_agent(agent)
        samples = tuple(row for row in samples if row.agent == selected_agent)
    if selected_root is not None and view == "session":
        records, _ = read_new_events(events_path(selected_root), 0, selected_root)
        identities = load_agent_identities(selected_root)
        samples = samples_for_sessions(
            UsageReport(
                view,
                samples=samples,
                status=report.status,
                captured_epoch_ms=report.captured_epoch_ms,
                detail=report.detail,
            ),
            usage_session_keys(records, identities, selected_root),
        )
    filtered = UsageReport(
        view,
        samples=samples,
        status=report.status,
        captured_epoch_ms=report.captured_epoch_ms,
        detail=report.detail,
    )
    if json_output:
        print(json.dumps(filtered.to_wire(), indent=2, sort_keys=True))
    else:
        print(render_usage_table(filtered))
    return 0 if report.status != "unavailable" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="side-dog",
        description="Watch coding agents work in a narrow terminal pane.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser(
        "setup", help="guide agent-specific and optional project setup"
    )
    setup_parser.add_argument("project", nargs="?", default=".")
    claude_group = setup_parser.add_mutually_exclusive_group()
    claude_group.add_argument(
        "--claude",
        action="store_true",
        default=None,
        dest="claude",
        help="install project-local Claude Code hooks",
    )
    claude_group.add_argument(
        "--no-claude",
        action="store_false",
        dest="claude",
        help="skip Claude Code hooks",
    )
    herdr_group = setup_parser.add_mutually_exclusive_group()
    herdr_group.add_argument(
        "--herdr",
        action="store_true",
        default=None,
        dest="herdr",
        help="include optional Herdr session discovery in launch commands",
    )
    herdr_group.add_argument(
        "--no-herdr",
        action="store_false",
        dest="herdr",
        help="use Side Dog without Herdr session discovery",
    )

    init_parser = subparsers.add_parser(
        "init", help="install project-local Claude Code hooks"
    )
    init_parser.add_argument("project", nargs="?", default=".")
    init_parser.add_argument(
        "--print",
        action="store_true",
        dest="print_only",
        help="print merged settings without writing",
    )

    doctor_parser = subparsers.add_parser(
        "doctor", help="check local readiness without changing configuration"
    )
    doctor_parser.add_argument("project", nargs="?", default=None)
    doctor_parser.add_argument("--no-color", action="store_true")

    hook_parser = subparsers.add_parser("hook", help="receive a Claude Code hook event")
    hook_parser.add_argument("--root", help="the folder Side Dog was set up in")

    watch_parser = subparsers.add_parser(
        "watch",
        help="render the live narrow activity feed",
        description=(
            "Watch coding-agent activity. Bare `side-dog watch` discovers active "
            "agent folders; `side-dog watch .` explicitly watches only the current "
            "folder and its active worktrees."
        ),
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
        default=DEFAULT_GITHUB_POLL_SECONDS,
        help=(
            "minimum seconds between verified GitHub PR readbacks; idle and"
            " finished branches back off automatically; 0 disables"
        ),
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
    watch_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="do not send desktop notifications for events such as test failures",
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
        "--discovery-mode",
        choices=tuple(DISCOVERY_MODES),
        help=argparse.SUPPRESS,
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
    panel_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="do not send desktop notifications for events such as test failures",
    )

    usage_parser = subparsers.add_parser(
        "usage", help="report local coding-agent tokens and API-equivalent cost"
    )
    usage_parser.add_argument(
        "view",
        nargs="?",
        choices=("daily", "monthly", "session"),
        default="daily",
        help="aggregate by day, month, or coding-agent session",
    )
    usage_parser.add_argument("--since", help="first included date")
    usage_parser.add_argument("--until", help="last included date")
    usage_parser.add_argument("--agent", help="show only one coding-agent provider")
    usage_parser.add_argument(
        "--root",
        help=(
            "filter session reports to sessions already associated with this "
            "Side Dog root; unsupported for daily and monthly reports"
        ),
    )
    usage_parser.add_argument(
        "--cost-mode",
        choices=("auto", "calculate", "display"),
        default="auto",
        help="choose ccusage recorded/calculated cost handling",
    )
    usage_parser.add_argument(
        "--no-cost", action="store_true", help="report tokens without cost fields"
    )
    usage_parser.add_argument("--json", action="store_true", dest="json_output")

    pane_parser = subparsers.add_parser(
        "tmux", help="open the feed in a right-side tmux pane"
    )
    pane_parser.add_argument("project", nargs="?", default=".")
    pane_parser.add_argument("--width", type=int, default=42)

    demo_parser = subparsers.add_parser(
        "demo", help="run a self-contained synthetic first-run tour"
    )
    demo_view = demo_parser.add_mutually_exclusive_group()
    demo_view.add_argument(
        "--panel", action="store_const", const="panel", dest="view"
    )
    demo_view.add_argument(
        "--watch", action="store_const", const="watch", dest="view"
    )
    demo_parser.set_defaults(view="panel")
    demo_parser.add_argument(
        "--duration",
        type=float,
        default=12.0,
        help="seconds over which synthetic events are streamed",
    )
    demo_parser.add_argument(
        "--no-open",
        action="store_true",
        help="with --panel, print the local URL without opening a browser",
    )
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
    elif arguments[0] not in (*COMMANDS, "-h", "--help", "--version"):
        return command_error(f"unknown command {arguments[0]!r}")

    args = parser.parse_args(arguments)
    if args.command == "hook":
        return hook(args.root)
    if args.command == "setup":
        return setup(args.project, claude=args.claude, herdr=args.herdr)
    if args.command == "init":
        return init_claude(args.project, print_only=args.print_only)
    if args.command == "doctor":
        from side_dog.doctor import doctor

        return doctor(
            args.project or ".",
            no_color=args.no_color,
            project_explicit=args.project is not None,
        )
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
            no_notify=args.no_notify,
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
            discovery_mode_key=args.discovery_mode,
            no_notify=args.no_notify,
        )
    if args.command == "usage":
        return usage_report_command(
            args.view,
            since=args.since,
            until=args.until,
            agent=args.agent,
            root=args.root,
            json_output=args.json_output,
            no_cost=args.no_cost,
            cost_mode=args.cost_mode,
        )
    if args.command == "tmux":
        return tmux_pane(args.project, width=args.width)
    if args.command == "demo":
        return demo_tour(
            args.view,
            duration=args.duration,
            open_window=not args.no_open,
        )
    return 2


def command_error(message: str) -> int:
    available = ", ".join(COMMANDS)
    print(f"side-dog: {message}", file=sys.stderr)
    print(f"Available commands: {available}", file=sys.stderr)
    print("Try 'side-dog help' or 'side-dog help <command>'.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
