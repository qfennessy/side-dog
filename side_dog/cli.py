from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


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
DELIVERY_KINDS = {
    "branch",
    "commit",
    "github",
    "issue",
    "merge",
    "pr",
    "push",
    "test",
    "worktree",
}


def canonical_root(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


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
    context = {"session_id": str(payload.get("session_id", "unknown"))}
    prompt_id = payload.get("prompt_id")
    if isinstance(prompt_id, str) and prompt_id:
        context["turn_id"] = prompt_id
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
            matches.append((match.start(), (kind, title, detail)))
    matches.sort(key=lambda item: item[0])
    return [item for _, item in matches]


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
    for index, (kind, running_title, detail) in enumerate(classified):
        if status == "running":
            title = running_title
        elif status == "failed":
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
        else:
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
        append_event(
            root,
            {
                **context,
                "operation_id": f"{identifier}:{index}:{kind}",
                "group_id": identifier,
                "kind": kind,
                "status": status,
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
    return f"{base} --root {shlex.quote(os.fspath(root))}"


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


def is_side_dog_entry(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    hooks = value.get("hooks")
    if not isinstance(hooks, list):
        return False
    return any(
        isinstance(item, dict)
        and any(
            marker in str(item.get("command", ""))
            for marker in ("side-dog", "side_dog")
        )
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
    if kind == "github":
        github_state = event.get("github_state")
        if github_state == "MERGED":
            return "⇉", ANSI["green"]
        if github_state == "CLOSED":
            return "×", ANSI["yellow"]
        return "↗", ANSI["red"]
    styles = {
        "file": ("✎", ANSI["cyan"]),
        "config": ("⚙", ANSI["magenta"]),
        "test": ("✓", ANSI["green"]),
        "branch": ("⑂", ANSI["blue"]),
        "worktree": ("⌘", ANSI["blue"]),
        "commit": ("◆", ANSI["magenta"]),
        "push": ("↑", ANSI["cyan"]),
        "pr": ("↗", ANSI["red"]),
        "merge": ("⇉", ANSI["green"]),
        "issue": ("◈", ANSI["yellow"]),
        "session": ("◇", ANSI["blue"]),
    }
    return styles.get(str(kind), ("·", ANSI["dim"]))


def coalesce_operations(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    for record in records:
        identifier = record.get("operation_id")
        if isinstance(identifier, str) and identifier in indexes:
            index = indexes[identifier]
            previous = output[index]
            merged = {**previous, **record}
            merged["started_epoch_ms"] = previous.get(
                "started_epoch_ms", previous.get("epoch_ms")
            )
            output[index] = merged
        else:
            record = dict(record)
            if record.get("status") == "running":
                record["started_epoch_ms"] = record.get("epoch_ms")
            if isinstance(identifier, str):
                indexes[identifier] = len(output)
            output.append(record)
    return output


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
    for agent in agents:
        if not isinstance(agent, dict) or agent.get("agent") != "claude":
            continue
        raw_cwd = agent.get("foreground_cwd") or agent.get("cwd")
        if not isinstance(raw_cwd, str):
            continue
        try:
            if canonical_root(raw_cwd) != root:
                continue
        except OSError:
            continue
        pane_id = str(agent.get("pane_id", ""))
        identity = {
            "pane_id": pane_id,
            "workspace_id": str(agent.get("workspace_id", "")),
            "tab_id": str(agent.get("tab_id", "")),
            "status": str(agent.get("agent_status", "unknown")),
            "label": str(
                agent.get("terminal_title_stripped")
                or agent.get("terminal_title")
                or pane_id
                or "Claude"
            ),
        }
        session = agent.get("agent_session")
        if isinstance(session, dict) and isinstance(session.get("value"), str):
            identities[session["value"]] = identity
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


def normalize_github_pr(raw: dict[str, Any]) -> dict[str, Any]:
    checks = raw.get("statusCheckRollup")
    if not isinstance(checks, list):
        checks = []
    failed_conclusions = {
        "ACTION_REQUIRED",
        "CANCELLED",
        "FAILURE",
        "STALE",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
    failed = 0
    pending = 0
    passed = 0
    for check in checks:
        if not isinstance(check, dict):
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        context_state = str(check.get("state") or "").upper()
        if conclusion in failed_conclusions or context_state in {
            "ERROR",
            "FAILURE",
        }:
            failed += 1
        elif context_state == "SUCCESS":
            passed += 1
        elif context_state in {"EXPECTED", "PENDING"}:
            pending += 1
        elif status and status != "COMPLETED":
            pending += 1
        elif conclusion in {"SUCCESS", "SKIPPED", "NEUTRAL"}:
            passed += 1
        else:
            pending += 1
    total = failed + pending + passed
    if failed:
        ci = f"CI {failed} failed"
    elif pending:
        ci = f"CI {passed}/{total}"
    elif total:
        ci = f"CI {total}/{total}"
    else:
        ci = "CI none"
    return {
        "number": raw["number"],
        "url": str(raw.get("url") or ""),
        "title": str(raw.get("title") or ""),
        "state": str(raw.get("state") or "UNKNOWN").upper(),
        "draft": bool(raw.get("isDraft")),
        "branch": str(raw.get("headRefName") or ""),
        "review": str(raw.get("reviewDecision") or "").upper(),
        "merge_state": str(raw.get("mergeStateStatus") or "").upper(),
        "mergeable": str(raw.get("mergeable") or "").upper(),
        "ci": ci,
        "checks_total": total,
        "checks_passed": passed,
        "checks_pending": pending,
        "checks_failed": failed,
        "created_at": raw.get("createdAt"),
        "updated_at": raw.get("updatedAt"),
        "closed_at": raw.get("closedAt"),
        "merged_at": raw.get("mergedAt"),
        "coverage": "OK",
    }


def github_fingerprint(status: dict[str, Any]) -> str:
    material = {
        key: status.get(key)
        for key in (
            "number",
            "state",
            "draft",
            "review",
            "merge_state",
            "mergeable",
            "ci",
            "updated_at",
        )
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[
        :16
    ]


def github_detail(status: dict[str, Any]) -> str:
    pieces = [str(status.get("state", "UNKNOWN")), str(status.get("ci", "CI ?"))]
    if status.get("draft"):
        pieces.insert(1, "DRAFT")
    if status.get("review"):
        pieces.append(str(status["review"]))
    if status.get("merge_state"):
        pieces.append(str(status["merge_state"]))
    if status.get("coverage") == "PARTIAL":
        pieces.append("PARTIAL")
    return " · ".join(pieces)


def latest_delivery_context(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(list(records)):
        if event.get("kind") not in {"pr", "merge", "push", "commit"}:
            continue
        return {
            key: event[key]
            for key in (
                "session_id",
                "turn_id",
                "group_id",
                "herdr_pane_id",
                "herdr_tab_id",
                "herdr_workspace_id",
            )
            if key in event
        }
    return {}


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
    return {
        **context,
        "operation_id": f"github-pr-{number}",
        "group_id": context.get("group_id", f"github-pr-{number}"),
        "kind": "github",
        "status": "success",
        "title": title,
        "detail": github_detail(status),
        "github_state": state,
        "github": status,
        "github_fingerprint": github_fingerprint(status),
    }


def identity_for_event(
    event: dict[str, Any], identities: dict[str, dict[str, str]]
) -> dict[str, str]:
    session_id = str(event.get("session_id", ""))
    pane_id = str(event.get("herdr_pane_id", ""))
    identity = identities.get(session_id) or identities.get(f"pane:{pane_id}")
    if identity is not None:
        return identity
    if pane_id:
        return {
            "pane_id": pane_id,
            "workspace_id": str(event.get("herdr_workspace_id", "")),
            "tab_id": str(event.get("herdr_tab_id", "")),
            "status": "unknown",
            "label": "Claude",
        }
    if session_id:
        return {
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "status": "unknown",
            "label": f"Claude {session_id[:8]}",
        }
    return {
        "pane_id": "",
        "workspace_id": "",
        "tab_id": "",
        "status": "unknown",
        "label": "filesystem",
    }


def lane_key(event: dict[str, Any], identities: dict[str, dict[str, str]]) -> str:
    if event.get("agent") == "filesystem" and not event.get("session_id"):
        return "filesystem"
    identity = identity_for_event(event, identities)
    return identity.get("pane_id") or str(event.get("session_id", "unknown"))


def lane_label(identity: dict[str, str]) -> str:
    pane_id = identity.get("pane_id", "")
    label = identity.get("label", "Claude")
    status = identity.get("status", "unknown")
    prefix = f"{pane_id} · " if pane_id else ""
    suffix = f" · {status}" if status not in {"", "unknown"} else ""
    return f"{prefix}{label}{suffix}"


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


def collapse_repeated_filesystem_events(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any]] = []
    previous_key: tuple[Any, ...] | None = None
    for original in events:
        event = dict(original)
        collapsible = event.get("agent") == "filesystem" and event.get("kind") in {
            "file",
            "config",
        }
        key = (
            event.get("agent"),
            event.get("kind"),
            event.get("status"),
            event.get("title"),
            event.get("detail"),
        )
        if collapsible and key == previous_key and collapsed:
            previous = collapsed[-1]
            previous.setdefault("first_timestamp", previous.get("timestamp"))
            previous.setdefault("first_epoch_ms", previous.get("epoch_ms"))
            previous["timestamp"] = event.get("timestamp")
            previous["epoch_ms"] = event.get("epoch_ms")
            previous["repeat_count"] = int(previous.get("repeat_count", 1)) + 1
            continue
        event["repeat_count"] = 1
        collapsed.append(event)
        previous_key = key if collapsible else None
    return collapsed


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


def render_event_line(
    event: dict[str, Any], width: int, color: bool, now_ms: int
) -> str:
    when = display_time(event)
    icon, style = event_style(event)
    detail = str(event.get("detail", ""))
    title = str(event.get("title", "Activity"))
    duration = format_duration(event, now_ms)
    summary = f"{title} · {detail}" if detail else title
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


def render_card_child(event: dict[str, Any], width: int, color: bool) -> str:
    icon, style = event_style(event)
    detail = str(event.get("detail", ""))
    title = str(event.get("title", "Activity"))
    summary = f"{title} · {detail}" if detail else title
    summary = crop(summary, max(4, width - 8))
    if color:
        return f"│   {style}{icon}{ANSI['reset']} {summary}"
    return f"│   {icon} {summary}"


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


def card_title(events: list[dict[str, Any]]) -> str:
    kinds = {str(event.get("kind")) for event in events}
    if kinds & {"test", "commit", "push", "pr", "github", "merge"}:
        return "Delivery"
    if "issue" in kinds:
        return "Issue update"
    return "Git workflow"


def render_delivery_card(
    events: list[dict[str, Any]], width: int, color: bool, now_ms: int
) -> list[str]:
    timestamp = str(events[0].get("timestamp", ""))
    try:
        when = datetime.fromisoformat(timestamp).astimezone().strftime("%H:%M")
    except ValueError:
        when = "--:--"
    duration = group_duration(events, now_ms)
    heading = card_title(events)
    if duration:
        heading += f" · {duration}"
    heading = crop(heading, max(4, width - 12))
    if color:
        first = (
            f"│ {ANSI['dim']}{when}{ANSI['reset']} "
            f"{ANSI['bold']}{ANSI['blue']}┌ {heading}{ANSI['reset']}"
        )
    else:
        first = f"│ {when} ┌ {heading}"
    return [first, *(render_card_child(event, width, color) for event in events)]


def render_lane_activity(
    events: list[dict[str, Any]],
    line_budget: int,
    width: int,
    color: bool,
    now_ms: int,
) -> list[str]:
    events = collapse_repeated_filesystem_events(events)
    grouped: dict[str, list[dict[str, Any]]] = {}
    singles: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") not in DELIVERY_KINDS:
            singles.append(event)
            continue
        group = event.get("turn_id") or event.get("group_id")
        if not isinstance(group, str) or not group:
            singles.append(event)
            continue
        grouped.setdefault(group, []).append(event)

    blocks: list[tuple[int, list[str]]] = []
    for event in singles:
        blocks.append(
            (
                int(event.get("epoch_ms") or 0),
                [render_event_line(event, width, color, now_ms)],
            )
        )
    for group_events in grouped.values():
        if len(group_events) == 1:
            event = group_events[0]
            lines = [render_event_line(event, width, color, now_ms)]
        else:
            lines = render_delivery_card(group_events, width, color, now_ms)
        blocks.append(
            (
                max(int(event.get("epoch_ms") or 0) for event in group_events),
                lines,
            )
        )
    blocks.sort(key=lambda block: block[0])

    selected: list[list[str]] = []
    remaining = max(1, line_budget)
    for _, lines in reversed(blocks):
        if len(lines) <= remaining:
            selected.append(lines)
            remaining -= len(lines)
        elif not selected:
            if remaining == 1:
                selected.append(lines[:1])
            else:
                selected.append([lines[0], *lines[-(remaining - 1) :]])
            remaining = 0
        if remaining <= 0:
            break
    return [line for block in reversed(selected) for line in block]


def render_github_banner(status: dict[str, Any], width: int, color: bool) -> str:
    number = status.get("number")
    prefix = f" PR #{number} " if number else " GitHub "
    text = crop(prefix + github_detail(status), width)
    if not color:
        return text
    if status.get("coverage") == "PARTIAL":
        style = ANSI["yellow"]
    elif status.get("state") == "MERGED":
        style = ANSI["green"]
    elif status.get("state") == "CLOSED":
        style = ANSI["yellow"]
    else:
        style = ANSI["red"]
    return f"{ANSI['bold']}{style}{text}{ANSI['reset']}"


def render(
    records: list[dict[str, Any]],
    root: Path,
    width: int,
    height: int,
    color: bool,
    identities: dict[str, dict[str, str]] | None = None,
    session_filter: str | None = None,
    github_status: dict[str, Any] | None = None,
) -> str:
    identities = identities or {}
    width = max(28, min(width, 80))
    header = f" SIDE DOG  {root.name} "
    line = "─" * max(0, width - len(header))
    if color:
        output = [f"{ANSI['bold']}{ANSI['blue']}{header}{line}{ANSI['reset']}"]
    else:
        output = [header + line]
    if github_status:
        output.append(render_github_banner(github_status, width, color))
    available = max(1, height - 3 - int(github_status is not None))
    coalesced = coalesce_operations(records)
    lanes: dict[str, tuple[dict[str, str], list[dict[str, Any]]]] = {}
    for event in coalesced:
        identity = identity_for_event(event, identities)
        if not matches_session_filter(event, identity, session_filter):
            continue
        key = lane_key(event, identities)
        lanes.setdefault(key, (identity, []))[1].append(event)

    if not lanes:
        message = crop("waiting for Claude Code activity…", width - 2)
        output.append(f"  {message}")
    else:
        ordered = sorted(lanes.values(), key=lambda lane: lane_label(lane[0]))
        event_budget = max(1, (available - len(ordered)) // len(ordered))
        now_ms = int(time.time() * 1000)
        for identity, lane_events in ordered:
            label = crop(lane_label(identity), max(4, width - 2))
            header_line = f"┌ {label}"
            if color:
                header_line = (
                    f"{ANSI['bold']}{ANSI['blue']}{header_line}{ANSI['reset']}"
                )
            output.append(header_line)
            output.extend(
                render_lane_activity(lane_events, event_budget, width, color, now_ms)
            )
    footer = crop(" Ctrl-C quit · hooks never block Claude ", width)
    output.append((f"{ANSI['dim']}{footer}{ANSI['reset']}" if color else footer))
    return "\n".join(output[:height])


def watch(
    project: str,
    *,
    width: int,
    poll: float,
    no_color: bool,
    session_filter: str | None = None,
    github_poll: float = 15.0,
) -> int:
    root = canonical_root(project)
    path = events_path(root)
    records: deque[dict[str, Any]] = deque(latest_events(path), maxlen=500)
    position = path.stat().st_size if path.exists() else 0
    known_files = snapshot(root)
    last_hook_writes: dict[str, float] = {}
    identities: dict[str, dict[str, str]] = {}
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
    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    color = not no_color and sys.stdout.isatty()
    if color:
        sys.stdout.write("\x1b[?25l\x1b[?1049h")
        sys.stdout.flush()
    try:
        last_scan = 0.0
        last_herdr_refresh = -10.0
        last_github_refresh = -max(1.0, github_poll)
        while running:
            new_records, position = read_new_events(path, position)
            now = time.monotonic()
            for record in new_records:
                records.append(record)
                if record.get("kind") in {"file", "config"}:
                    last_hook_writes[str(record.get("detail", ""))] = now
                if record.get("kind") in {"pr", "merge"}:
                    last_github_refresh = -max(1.0, github_poll)
            if now - last_scan >= max(0.5, poll):
                current = snapshot(root)
                for changed in sorted(
                    path
                    for path, value in current.items()
                    if known_files.get(path) != value
                ):
                    if now - last_hook_writes.get(changed, -100.0) < 2.0:
                        continue
                    record = {
                        "schema": SCHEMA,
                        "timestamp": datetime.now(timezone.utc).isoformat(
                            timespec="milliseconds"
                        ),
                        "epoch_ms": int(time.time() * 1000),
                        "agent": "filesystem",
                        "project": os.fspath(root),
                        "kind": "config" if is_config(changed) else "file",
                        "status": "success",
                        "title": "Config changed"
                        if is_config(changed)
                        else "File changed",
                        "detail": changed,
                    }
                    append_event(
                        root,
                        {
                            key: value
                            for key, value in record.items()
                            if key not in {"schema", "timestamp", "epoch_ms", "project"}
                        },
                    )
                known_files = current
                last_scan = now
            if now - last_herdr_refresh >= 2.0:
                identities = load_herdr_identities(root)
                last_herdr_refresh = now
            if github_poll > 0 and now - last_github_refresh >= github_poll:
                verified, github_error = load_github_pr(root)
                if verified is not None:
                    fingerprint = github_fingerprint(verified)
                    if fingerprint != last_github_fingerprint:
                        append_event(
                            root,
                            github_event(
                                verified,
                                github_status,
                                latest_delivery_context(records),
                            ),
                        )
                        last_github_fingerprint = fingerprint
                    github_status = verified
                elif github_status is not None:
                    github_status = {
                        **github_status,
                        "coverage": "PARTIAL",
                        "error": github_error,
                    }
                elif any(record.get("kind") in {"pr", "merge"} for record in records):
                    github_status = {
                        "state": "UNKNOWN",
                        "ci": "CI ?",
                        "coverage": "PARTIAL",
                        "error": github_error,
                    }
                last_github_refresh = now
            terminal = shutil.get_terminal_size((width, 30))
            actual_width = min(width, terminal.columns)
            screen = render(
                list(records),
                root,
                actual_width,
                terminal.lines,
                color,
                identities,
                session_filter,
                github_status,
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
        description="Watch Claude Code work in a narrow terminal pane.",
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
    watch_parser.add_argument("project", nargs="?", default=".")
    watch_parser.add_argument("--width", type=int, default=42)
    watch_parser.add_argument(
        "--poll", type=float, default=0.75, help="filesystem scan interval"
    )
    watch_parser.add_argument(
        "--session",
        dest="session_filter",
        help="show one Claude session by pane, title, or session-id prefix",
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
            args.project,
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
