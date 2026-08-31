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
import subprocess
import sys
import termios
import time
import tty
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from side_dog.model import (
    MILESTONE_KINDS,
    SOURCE_KEY,
    SOURCE_LABEL,
    activity_unit_local_date,
    actor_label,
    agent_label,
    build_activity_units,
    coalesce_operations,
    display_conventional_subject,
    event_epoch,
    event_source_label,
    github_detail,
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

ANSI = {
    "reset": "\x1b[0m",
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "blue": "\x1b[38;5;75m",
    "cyan": "\x1b[38;5;80m",
    "green": "\x1b[38;5;78m",
    "magenta": "\x1b[38;5;176m",
    "red": "\x1b[38;5;203m",
    "yellow": "\x1b[38;5;221m",
}

GITHUB_PR_FIELDS = (
    "number,url,title,state,isDraft,headRefName,reviewDecision,mergeStateStatus,"
    "mergeable,statusCheckRollup,createdAt,updatedAt,closedAt,mergedAt"
)
FILTER_ORDER = ("all", "milestones", "files")
CODEX_METADATA_CACHE: dict[str, tuple[int, dict[str, str]]] = {}
CLAUDE_METADATA_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def canonical_root(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def display_root(root: Path) -> str:
    try:
        return os.fspath(Path("~") / root.relative_to(Path.home()))
    except ValueError:
        return os.fspath(root)


def label_summary(event: dict[str, Any], summary: str) -> str:
    label = event_source_label(event)
    return f"[{label}] {summary}" if label else summary


def project_key(root: Path) -> str:
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    return f"{root.name}-{digest}"


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


def classify_commands(command: str) -> list[tuple[str, str, str]]:
    collapsed = " ".join(command.split())
    if not collapsed:
        return []

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
            if (match := re.search(pattern, collapsed, re.IGNORECASE))
        ),
        None,
    )
    if test_match:
        runner = _safe_arg(
            collapsed,
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
        match = re.search(pattern, collapsed, re.IGNORECASE)
        if match:
            if kind == "pr" and "create" in pattern:
                detail = _safe_title_flag(command, ("gh", "pr", "create")) or detail
            elif kind == "issue" and "create" in pattern:
                detail = _safe_title_flag(command, ("gh", "issue", "create")) or detail
            elif kind == "issue" and "close" in pattern:
                detail = _safe_arg(
                    collapsed,
                    r"\bgh\s+issue\s+close\s+#?(\d+)",
                    detail,
                )
                if detail.isdigit():
                    detail = f"issue #{detail}"
            elif kind == "issue" and "reopen" in pattern:
                detail = _safe_arg(
                    collapsed,
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


def operation_id(payload: dict[str, Any]) -> str:
    raw = payload.get("tool_use_id")
    if isinstance(raw, str) and raw:
        return raw
    session = str(payload.get("session_id", "unknown"))
    material = json.dumps(payload.get("tool_input", {}), sort_keys=True, default=str)
    return hashlib.sha256(f"{session}:{material}".encode()).hexdigest()[:16]


def emit_tool_event(payload: dict[str, Any], root: Path, *, status: str) -> None:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    context = hook_context(payload)
    identifier = operation_id(payload)

    if tool_name in EDIT_TOOLS:
        path = edit_path(tool_input, root)
        config = is_config(path)
        if status == "running":
            title = "Writing config" if config else "Writing file"
        elif status == "failed":
            title = "Config write failed" if config else "File write failed"
        else:
            title = "Wrote config" if config else "Wrote file"
        append_event(
            root,
            {
                **context,
                "operation_id": identifier,
                "group_id": identifier,
                "kind": "config" if config else "file",
                "status": status,
                "title": title,
                "detail": path,
            },
        )
        return

    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return
    command = tool_input.get("command")
    if not isinstance(command, str):
        return
    classified = classify_commands(command)
    if not classified:
        return
    event_status = status
    if status != "running" and shell_command_is_compound(command):
        event_status = "unknown"
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
        append_event(
            root,
            {
                **context,
                **extra,
                "operation_id": f"{identifier}:{index}:{kind}",
                "group_id": identifier,
                "kind": kind,
                "status": event_status,
                "title": title,
                "detail": detail,
            },
        )


def hook(explicit_root: str | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
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
        raise SystemExit(f"project does not exist: {root}")
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


def iter_project_files(root: Path) -> Iterable[Path]:
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in IGNORED_DIRS]
        current_path = Path(current)
        for name in files:
            path = current_path / name
            try:
                if path.is_symlink() or path.stat().st_size > 5_000_000:
                    continue
            except OSError:
                continue
            yield path


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    result: dict[str, tuple[int, int]] = {}
    for path in iter_project_files(root):
        try:
            stat = path.stat()
            result[os.fspath(path.relative_to(root))] = (stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError):
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
    repository = common_path.parent.name if common_path.name == ".git" else root.name
    return {
        "oid": oid,
        "short_oid": oid[:7],
        "branch": branch,
        "common_dir": os.fspath(common_path),
        "repository": repository,
    }


@lru_cache(maxsize=128)
def git_common_dir(path: str) -> str:
    state = load_git_state(canonical_root(path))
    return state.get("common_dir", "") if state else ""


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
    if width <= 1:
        return text[:width]
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


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
    }
    return styles.get(str(kind), ("·", ANSI["dim"]))


@lru_cache(maxsize=64)
def codex_session_path(session_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", session_id):
        return None
    codex_root = Path.home() / ".codex"
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
            for raw_line in handle:
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


@lru_cache(maxsize=64)
def claude_session_path(session_id: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-fA-F-]{32,40}", session_id):
        return None
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
            for raw_line in handle:
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


def load_herdr_identities(root: Path) -> dict[str, dict[str, str]]:
    if shutil.which("herdr") is None:
        return {}
    try:
        completed = subprocess.run(
            ["herdr", "api", "snapshot"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            return {}
        document = json.loads(completed.stdout)
        agents = document["result"]["snapshot"]["agents"]
    except (
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ):
        return {}

    identities: dict[str, dict[str, str]] = {}
    watched_common_dir = git_common_dir(os.fspath(root))
    for agent in agents:
        if not isinstance(agent, dict) or agent.get("agent") not in {
            "claude",
            "codex",
        }:
            continue
        raw_cwd = agent.get("foreground_cwd") or agent.get("cwd")
        if not isinstance(raw_cwd, str):
            continue
        try:
            agent_root = canonical_root(raw_cwd)
            same_root = agent_root == root
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
            "pane_id": pane_id,
            "workspace_id": str(agent.get("workspace_id", "")),
            "tab_id": str(agent.get("tab_id", "")),
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
            if identity["agent"] == "codex":
                identity.update(load_codex_metadata(session_id))
            elif identity["agent"] == "claude-code":
                identity.update(load_claude_metadata(session_id))
            identities[session_id] = identity
        if pane_id:
            identities[f"pane:{pane_id}"] = identity
    return identities


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
        title = f"PR #{number} status updated"
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
    return f"{minutes}m{remainder:02d}s"


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


def display_detail(event: dict[str, Any]) -> str:
    github_status = event.get("github")
    if event.get("kind") == "github" and isinstance(github_status, dict):
        return display_github_detail(github_status)
    detail = str(event.get("detail", ""))
    if event.get("kind") in {"commit", "pr"}:
        return display_conventional_subject(detail)
    return detail


def render_event_line(
    event: dict[str, Any],
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
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
    summary = label_summary(event, summary)
    if duration:
        summary += f" · {duration}"
    repeats = int(event.get("repeat_count", 1))
    suffix = ""
    if repeats > 1:
        suffix = f" · ×{repeats}"
    summary_width = max(4, width - len(when) - 6)
    summary = crop(summary, max(1, summary_width - len(suffix))) + suffix
    if color:
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
    unit: dict[str, Any], width: int, color: bool
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
    summary = label_summary(
        latest, f"Files · {' · '.join(actions)} · {len(paths)} paths"
    )
    summary = crop(summary, max(4, width - len(when) - 6))
    if color:
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
) -> list[str]:
    when = display_time(event)
    icon, style = event_style(event)
    actor = actor_label(event, identities)
    label = milestone_label(event)
    heading = f"{actor} · {label}" if actor else label
    source = event_source_label(event)
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
) -> list[str]:
    events = unit["events"]
    ordered = sorted(events, key=event_epoch)
    when = display_time(max(ordered, key=event_epoch))
    actor = actor_label(ordered[-1], identities)
    heading = str(unit["title"])
    if actor:
        heading = f"{actor} · {heading}"
    heading = label_summary(ordered[-1], heading)
    duration = group_duration(events, now_ms)
    if duration:
        heading += f" · {duration}"
    pipeline = " → ".join(str(stage) for stage in unit["stages"])
    heading = crop(heading, max(4, width - len(when) - 6))
    pipeline = crop(pipeline, max(4, width - 6))
    if color:
        return [
            f"│ {ANSI['dim']}{when}{ANSI['reset']} {ANSI['bold']}{ANSI['blue']}┌ {heading}{ANSI['reset']}",
            f"│   {ANSI['bold']}{pipeline}{ANSI['reset']}",
        ]
    return [f"│ {when} ┌ {heading}", f"│   {pipeline}"]


def render_activity_unit(
    unit: dict[str, Any],
    width: int,
    color: bool,
    now_ms: int,
    identities: dict[str, dict[str, str]],
) -> list[str]:
    events = unit["events"]
    if unit["type"] == "pipeline":
        return render_pipeline_card(unit, width, color, now_ms, identities)
    if unit["type"] == "filesystem_burst" and len(events) > 1:
        return render_filesystem_burst(unit, width, color)
    event = events[0]
    if event.get("kind") in MILESTONE_KINDS:
        return render_milestone_card(event, width, color, now_ms, identities)
    return [render_event_line(event, width, color, now_ms, identities)]


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
) -> tuple[list[str], int]:
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
    selected: list[tuple[date | None, list[str]]] = []
    remaining = max(1, line_budget)
    selected_units = 0
    selected_day: date | None = None
    today = local_date_for_epoch(now_ms, local_timezone)
    for unit in candidates:
        lines = render_activity_unit(unit, width, color, now_ms, identities)
        unit_day = activity_unit_local_date(unit, local_timezone)
        needs_separator = unit_day is not None and unit_day != selected_day
        separator_cost = int(needs_separator and today is not None)
        if len(lines) + separator_cost <= remaining:
            selected.append((unit_day, lines))
            remaining -= len(lines) + separator_cost
            selected_units += 1
            selected_day = unit_day
        elif not selected:
            if separator_cost and remaining > 1:
                selected.append((unit_day, lines[: remaining - 1]))
                selected_day = unit_day
            elif not separator_cost:
                selected.append((unit_day, lines[:remaining]))
            else:
                continue
            selected_units += 1
            remaining = 0
        if remaining <= 0:
            break
    if not newest_first:
        selected.reverse()
    hidden = max(0, len(candidates) - selected_units)
    rendered: list[str] = []
    displayed_day: date | None = None
    for unit_day, lines in selected:
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
        if agent not in {"claude-code", "codex"}:
            continue
        key = identity.get("pane_id") or f"{agent}:{identity.get('label', '')}"
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
        model = identity.get("model") or "model ?"
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
        label = identity.get("label", "").strip()
        model = identity.get("model") or "model ?"
        effort = identity.get("effort") or "effort ?"
        status = identity.get("status") or "unknown"
        context = (
            f" · {label}" if label and label.casefold() != agent.casefold() else ""
        )
        text = f" {agent}{context} · {model} · {effort} · {status}"
        if index == 0 and git_status:
            text += f"  │  {git_status['branch']} @ {git_status['short_oid']}"
        text = crop(text, width)
        lines.append(f"{ANSI['dim']}{text}{ANSI['reset']}" if color else text)
    if not agents and git_status:
        lines.append(render_git_banner(git_status, width, color))
    return lines


def display_identities(
    records: list[dict[str, Any]], identities: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    combined = dict(identities)
    for event in records:
        session_id = str(event.get("session_id", ""))
        agent = normalize_agent(event.get("agent"))
        if not session_id or agent not in {"claude-code", "codex"}:
            continue
        identity = dict(identity_for_event(event, identities))
        identity["agent"] = agent
        for field in ("model", "effort"):
            value = event.get(field)
            if isinstance(value, str) and value:
                identity[field] = value
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
        "│ r       toggle newest-first / oldest-first order",
    ]
    if root_count > 1:
        entries.extend(
            (
                "│ a       show all watched roots",
                "│ Tab     cycle the focused root",
                f"│ 1-{min(root_count, 9)}     focus a root by position",
            )
        )
    entries.extend(
        (
            "│ Esc     close this help",
            "│ Ctrl-C  quit Side Dog",
            "│",
            f"│ {order_note}; filesystem bursts collapse.",
            "│ Delivery cards connect edits, tests, commits, pushes, and PRs.",
            "│ Activity is scoped to watched roots; JSONL keeps every event.",
            "│ Header: blue open · yellow pending · green clean · red failure.",
            "└ Press ? or Esc to return",
        )
    )
    return [heading, *(crop(entry, width) for entry in entries)]


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
) -> str:
    identities = identities or {}
    width = max(28, min(width, 160))
    shown_identities = display_identities(records, identities)
    banner_identities = (
        identities if active_agent_identities(identities) else shown_identities
    )
    agents = len(active_agent_identities(banner_identities))
    project_name = (
        "multi-root"
        if root_count > 1
        else git_status.get("repository", root.name)
        if git_status
        else root.name
    )
    header = f" SIDE DOG  {project_name} "
    line = "─" * max(0, width - len(header))
    if color:
        output = [f"{ANSI['bold']}{ANSI['blue']}{header}{line}{ANSI['reset']}"]
    else:
        output = [header + line]
    if root_count > 1:
        scope = (
            f"{focused_root_label} · 1 of {root_count} roots"
            if focused_root_label
            else f"{root_count} roots"
        )
        noun = "agent" if agents == 1 else "agents"
        watching = crop(f" Watching {scope} · {agents} {noun}", width)
    else:
        watching = crop(f" Watching {display_root(root)}", width)
    output.append(f"{ANSI['dim']}{watching}{ANSI['reset']}" if color else watching)
    if root_summaries:
        summary = crop(f" {' · '.join(root_summaries)}", width)
        output.append(f"{ANSI['dim']}{summary}{ANSI['reset']}" if color else summary)
    elif github_status:
        output.append(render_github_banner(github_status, width, color))
    context_banners = render_context_banners(
        banner_identities, git_status if root_count == 1 else None, width, color
    )
    output.extend(context_banners)
    if show_help:
        output.extend(render_help(width, color, newest_first, root_count))
        footer = crop(" ? / Esc close help · Ctrl-C quit ", width)
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
        )
        detail_label = "expanded" if expanded_history else "compact"
        order_label = "newest first" if newest_first else "oldest first"
        timeline_header = f"┌ {order_label} · {detail_label} · {event_filter}"
        if hidden:
            hidden_direction = "below" if newest_first else "above"
            timeline_header += f" · {hidden} {hidden_direction}"
        if paused:
            timeline_header += f" · PAUSED · {new_event_count} new"
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
        f" a all · Tab root · 1-{min(root_count, 9)} jump ·" if root_count > 1 else ""
    )
    footer = crop(
        f"{root_actions} r {order_action} · e {detail_action} · f {event_filter} · p {pause_action} · ? help · Ctrl-C quit ",
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
        if not root.is_dir():
            raise SystemExit(f"project does not exist: {root}")
        if root in seen:
            raise SystemExit(f"duplicate project root: {root}")
        roots.append(root)
        seen.add(root)
    return roots


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
        known_files=snapshot(root),
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


def watch_root_summary(state: WatchRootState, label: str) -> str:
    summary = label
    if state.git_status and state.git_status.get("short_oid"):
        summary += f" @ {state.git_status['short_oid']}"
    if isinstance(state.github_status, dict):
        number = state.github_status.get("number")
        if isinstance(number, int) and not label.startswith("PR #"):
            summary += f" · PR #{number}"
        lifecycle = str(state.github_status.get("state") or "").upper()
        merge_state = str(state.github_status.get("merge_state") or "").upper()
        if lifecycle:
            summary += f" {lifecycle}"
        if merge_state and merge_state != lifecycle:
            summary += f" {merge_state}"
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
            tagged.append((event_epoch(record), root_index, append_index, record))
    tagged.sort(key=lambda item: item[:3])
    return [record for _, _, _, record in tagged]


def aggregate_watch_identities(
    states: list[WatchRootState], focused_index: int | None
) -> dict[str, dict[str, str]]:
    identities: dict[str, dict[str, str]] = {}
    for root_index in selected_watch_indexes(len(states), focused_index):
        state = states[root_index]
        source_key = os.fspath(state.root)
        for key, identity in state.identities.items():
            identities.setdefault(key, identity)
            identities[f"{source_key}:{key}"] = identity
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


def load_watch_root_external_refresh(
    root: Path,
    refresh_herdr: bool,
    refresh_github: bool,
    github_branch: str | None = None,
) -> WatchRootExternalRefresh:
    return WatchRootExternalRefresh(
        identities=load_herdr_identities(root) if refresh_herdr else None,
        github_result=load_github_pr(root) if refresh_github else None,
        github_branch=github_branch,
    )


def apply_watch_root_external_refresh(
    state: WatchRootState, refresh: WatchRootExternalRefresh
) -> None:
    if refresh.identities is not None:
        state.identities = refresh.identities
    if refresh.github_result is None:
        return
    current_branch = state.git_status.get("branch") if state.git_status else None
    if refresh.github_branch != current_branch:
        return
    verified, github_error = refresh.github_result
    if verified is not None:
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


def poll_watch_root(
    state: WatchRootState,
    now: float,
    poll: float,
    github_poll: float,
    *,
    poll_external: bool = True,
) -> int:
    new_records, state.position = read_new_events(state.path, state.position)
    for record in new_records:
        state.records.append(record)
        if record.get("kind") in {"file", "config"}:
            state.last_hook_writes[str(record.get("detail", ""))] = now
        if record.get("kind") in {"pr", "merge"}:
            state.last_github_refresh = -max(1.0, github_poll)
    if now - state.last_scan >= max(0.5, poll):
        current = snapshot(state.root)
        for changed in sorted(
            path
            for path, value in current.items()
            if state.known_files.get(path) != value
        ):
            if now - state.last_hook_writes.get(changed, -100.0) < 2.0:
                continue
            append_event(
                state.root,
                {
                    "agent": "filesystem",
                    "kind": "config" if is_config(changed) else "file",
                    "status": "success",
                    "title": "Config changed" if is_config(changed) else "File changed",
                    "detail": changed,
                },
            )
        for removed in sorted(set(state.known_files) - set(current)):
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
    session_filter: str | None = None,
    github_poll: float = 15.0,
) -> int:
    roots = canonical_watch_roots(projects)
    states = [initialize_watch_root(root, github_poll) for root in roots]
    running = True
    show_help = False
    expanded_history = False
    newest_first = True
    event_filter_index = 0
    focused_root_index: int | None = None
    paused_records: dict[str, list[dict[str, Any]]] | None = None
    paused_new_count = 0
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
    if color:
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
                    if key == b"?":
                        show_help = not show_help
                    elif key == b"\x1b" and show_help:
                        show_help = False
                    elif key == b"e" and not show_help:
                        expanded_history = not expanded_history
                    elif key == b"f" and not show_help:
                        event_filter_index = (event_filter_index + 1) % len(
                            FILTER_ORDER
                        )
                    elif key == b"r" and not show_help:
                        newest_first = not newest_first
                    elif not show_help and (
                        key in {b"a", b"\t"} or (len(key) == 1 and key.isdigit())
                    ):
                        focused_root_index = root_focus_for_key(
                            key, focused_root_index, len(states)
                        )
                    elif key == b"p" and not show_help:
                        if paused_records is None:
                            paused_records = {
                                os.fspath(state.root): list(state.records)
                                for state in states
                            }
                            paused_new_count = 0
                        else:
                            paused_records = None
                            paused_new_count = 0
            now = time.monotonic()
            new_count = sum(
                poll_watch_root(
                    state,
                    now,
                    poll,
                    github_poll,
                    poll_external=refresh_executor is None,
                )
                for state in states
            )
            if refresh_executor is not None:
                apply_completed_watch_root_refreshes(states, pending_refreshes)
                schedule_watch_root_refreshes(
                    states,
                    now,
                    github_poll,
                    refresh_executor,
                    pending_refreshes,
                )
                if not color:
                    wait_for_watch_root_refreshes(states, pending_refreshes)
            if paused_records is not None:
                paused_new_count += new_count
            labels = watch_root_labels(states)
            records = aggregate_watch_records(
                states, labels, paused_records, focused_root_index
            )
            identities = aggregate_watch_identities(states, focused_root_index)
            selected_indexes = selected_watch_indexes(len(states), focused_root_index)
            primary_index = selected_indexes[0]
            primary = states[primary_index]
            multi_root = len(states) > 1
            summaries = (
                tuple(
                    watch_root_summary(states[index], labels[index])
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
                root_summaries=summaries,
            )
            if color:
                sys.stdout.write("\x1b[H\x1b[2J" + screen)
                sys.stdout.flush()
            else:
                sys.stdout.write(screen + "\n")
                sys.stdout.flush()
                return 0
            time.sleep(0.15)
    finally:
        if refresh_executor is not None:
            refresh_executor.shutdown(wait=False, cancel_futures=True)
        if input_descriptor is not None and terminal_state is not None:
            termios.tcsetattr(input_descriptor, termios.TCSADRAIN, terminal_state)
        if color:
            sys.stdout.write("\x1b[?1049l\x1b[?25h")
            sys.stdout.flush()
    return 0


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
    hook_parser.add_argument("--root", help="initialized project root")

    watch_parser = subparsers.add_parser(
        "watch", help="render the live narrow activity feed"
    )
    watch_parser.add_argument(
        "projects",
        nargs="*",
        default=["."],
        metavar="ROOT",
        help="one or more project roots to consolidate",
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
    watch_parser.add_argument("--no-color", action="store_true")

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
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        return hook(args.root)
    if args.command == "init":
        return init_claude(args.project, print_only=args.print_only)
    if args.command == "watch":
        return watch(
            args.projects,
            width=args.width,
            poll=args.poll,
            no_color=args.no_color,
            session_filter=args.session_filter,
            github_poll=args.github_poll,
        )
    if args.command == "tmux":
        return tmux_pane(args.project, width=args.width)
    if args.command == "demo":
        return emit_demo(args.project)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
