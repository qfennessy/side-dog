from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TextIO

from side_dog import __version__
from side_dog.integrations import (
    INTEGRATIONS,
    AdapterHealth,
    AdapterHealthStatus,
    IntegrationDescriptor,
)


GREEN = "\x1b[38;5;78m"
YELLOW = "\x1b[38;5;221m"
RED = "\x1b[38;5;203m"
CYAN = "\x1b[38;5;80m"
RESET = "\x1b[0m"


@dataclass(frozen=True)
class Readiness:
    name: str
    status: str
    detail: str
    required: bool = False


def _completed(
    command: list[str], timeout: float = 3.0, cwd: Path | None = None
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def git_probe(root: Path) -> Readiness:
    if shutil.which("git") is None:
        return Readiness(
            "Git",
            "fail",
            "Git is not on PATH; folder and delivery activity cannot be read.",
            required=True,
        )
    inside = _completed(
        ["git", "-C", os.fspath(root), "rev-parse", "--is-inside-work-tree"]
    )
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return Readiness(
            "Git project",
            "fail",
            "The selected folder is not a Git repository or worktree.",
            required=True,
        )
    paths = _completed(
        [
            "git",
            "-C",
            os.fspath(root),
            "rev-parse",
            "--absolute-git-dir",
            "--git-common-dir",
        ]
    )
    kind = "repository"
    if paths is not None and paths.returncode == 0:
        values = paths.stdout.splitlines()
        if len(values) >= 2:
            git_dir = Path(values[0]).resolve(strict=False)
            common_dir = Path(values[1])
            if not common_dir.is_absolute():
                common_dir = (root / common_dir).resolve(strict=False)
            else:
                common_dir = common_dir.resolve(strict=False)
            if git_dir != common_dir:
                kind = "linked worktree"
    return Readiness(
        "Git project", "ok", f"Ready; selected folder is a {kind}.", required=True
    )


def _remote_host(value: str) -> str | None:
    if "://" in value:
        from urllib.parse import urlsplit

        return urlsplit(value).hostname
    if "@" in value and ":" in value:
        return value.split("@", 1)[1].split(":", 1)[0] or None
    return None


def github_probe(root: Path) -> Readiness:
    if shutil.which("gh") is None:
        return Readiness(
            "GitHub readback",
            "info",
            "Optional gh CLI is absent; pull-request status will not be verified.",
        )
    repository = _completed(["gh", "repo", "view", "--json", "url"], cwd=root)
    if repository is None or repository.returncode != 0:
        return Readiness(
            "GitHub readback",
            "warn",
            "The selected project cannot be mapped to a GitHub repository; pull-request status is unavailable.",
        )
    try:
        repository_url = json.loads(repository.stdout).get("url")
    except (AttributeError, json.JSONDecodeError):
        repository_url = None
    host = _remote_host(repository_url) if isinstance(repository_url, str) else None
    if host is None:
        return Readiness(
            "GitHub readback",
            "warn",
            "The repository selected by gh has no recognizable GitHub host; pull-request status is unavailable.",
        )
    authenticated = _completed(
        [
            "gh",
            "auth",
            "status",
            "--hostname",
            host,
            "--active",
        ]
    )
    if authenticated is None or authenticated.returncode != 0:
        return Readiness(
            "GitHub readback",
            "warn",
            "Optional gh CLI is not authenticated; pull-request status will not be verified.",
        )
    return Readiness("GitHub readback", "ok", "Optional authenticated gh CLI is ready.")


def _home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME")
    return Path(configured).expanduser() if configured else Path.home()


def _override_guidance(provider: str) -> str:
    descriptor = next(item for item in INTEGRATIONS if item.provider == provider)
    overrides = descriptor.environment_overrides
    if not overrides:
        return ""
    rendered = ", ".join(
        f"{item.name} ({item.purpose})" for item in descriptor.environment_overrides
    )
    label = "Location override" if len(overrides) == 1 else "Location overrides"
    return f" {label}: {rendered}."


def _directory_health(
    provider: str,
    path: Path,
    *,
    ready: str,
    missing: str,
    explicitly_configured: bool = False,
) -> AdapterHealth:
    guidance = _override_guidance(provider)
    try:
        exists = path.exists()
        usable = path.is_dir() and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        exists = True
        usable = False
    if usable:
        return AdapterHealth(provider, AdapterHealthStatus.AVAILABLE, ready + guidance)
    if exists or explicitly_configured:
        return AdapterHealth(
            provider,
            AdapterHealthStatus.DEGRADED,
            "The configured session location is missing, unreadable, or not a directory."
            + guidance,
        )
    return AdapterHealth(provider, AdapterHealthStatus.UNAVAILABLE, missing + guidance)


def _sqlite_supports_query(path: Path, query: str) -> bool:
    try:
        uri = f"{path.resolve(strict=False).as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    except (OSError, sqlite3.Error, ValueError):
        return False
    try:
        connection.execute(query).fetchall()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return True


_OPENCODE_SCHEMA_QUERY = (
    "SELECT id, directory, title, model, agent, parent_id, time_updated "
    "FROM session LIMIT 0"
)

_CLINE_SCHEMA_QUERY = (
    "SELECT session_id, pid, status, cwd, workspace_root, model, "
    "metadata_json, messages_path, updated_at, started_at, "
    "parent_session_id, is_subagent FROM sessions LIMIT 0"
)


def codex_readiness(_root: Path, environment: Mapping[str, str]) -> AdapterHealth:
    configured = environment.get("CODEX_HOME")
    home = (
        Path(configured).expanduser() if configured else _home(environment) / ".codex"
    )
    return _directory_health(
        "codex",
        home / "sessions",
        ready="Native local session discovery is ready; no Side Dog hooks are needed.",
        missing="No local Codex sessions were found yet; activity will appear after Codex runs.",
        explicitly_configured=bool(configured),
    )


def pi_readiness(_root: Path, environment: Mapping[str, str]) -> AdapterHealth:
    configured = environment.get("PI_CODING_AGENT_DIR")
    agent_dir = (
        Path(configured).expanduser()
        if configured
        else _home(environment) / ".pi" / "agent"
    )
    return _directory_health(
        "pi",
        agent_dir / "sessions",
        ready="Native local session discovery is ready; no Side Dog hooks are needed.",
        missing="No local Pi sessions were found yet; activity will appear after Pi runs.",
        explicitly_configured=bool(configured),
    )


def deepseek_readiness(_root: Path, environment: Mapping[str, str]) -> AdapterHealth:
    configured = environment.get("DSH_HOME")
    home = Path(configured).expanduser() if configured else _home(environment) / ".dsh"
    return _directory_health(
        "deepseek",
        home / "sessions",
        ready="Native local session discovery is ready; no Side Dog hooks are needed.",
        missing="No local DeepSeek Harness sessions were found yet; activity will appear after Harness runs.",
        explicitly_configured=bool(configured),
    )


def opencode_readiness(_root: Path, environment: Mapping[str, str]) -> AdapterHealth:
    configured = environment.get("XDG_DATA_HOME")
    base = (
        Path(configured).expanduser()
        if configured
        else _home(environment) / ".local" / "share"
    )
    database = base / "opencode" / "opencode.db"
    guidance = _override_guidance("opencode")
    if not database.exists():
        status = (
            AdapterHealthStatus.DEGRADED
            if configured
            else AdapterHealthStatus.UNAVAILABLE
        )
        detail = (
            "The configured OpenCode data location does not contain a session store."
            if configured
            else "No local OpenCode session store was found yet; activity will appear after OpenCode runs."
        )
        return AdapterHealth(
            "opencode",
            status,
            detail + guidance,
        )
    if not database.is_file() or not os.access(database, os.R_OK):
        return AdapterHealth(
            "opencode",
            AdapterHealthStatus.DEGRADED,
            "The OpenCode session store is unreadable or not a file." + guidance,
        )
    if not _sqlite_supports_query(database, _OPENCODE_SCHEMA_QUERY):
        return AdapterHealth(
            "opencode",
            AdapterHealthStatus.DEGRADED,
            "The OpenCode session store cannot be read or has an unsupported schema."
            + guidance,
        )
    return AdapterHealth(
        "opencode",
        AdapterHealthStatus.AVAILABLE,
        "The local SQLite session store is ready; no Side Dog hooks are needed."
        + guidance,
    )


def _cline_locations(environment: Mapping[str, str]) -> tuple[Path, Path]:
    configured_data = environment.get("CLINE_DATA_DIR")
    if configured_data:
        data = Path(configured_data).expanduser()
    else:
        configured_root = environment.get("CLINE_DIR")
        home = (
            Path(configured_root).expanduser()
            if configured_root
            else _home(environment) / ".cline"
        )
        data = home / "data"
    sessions = Path(
        environment.get("CLINE_SESSION_DATA_DIR", os.fspath(data / "sessions"))
    ).expanduser()
    database_directory = Path(
        environment.get("CLINE_DB_DATA_DIR", os.fspath(data / "db"))
    ).expanduser()
    return sessions, database_directory / "sessions.db"


def cline_session_sources(_root: Path, environment: Mapping[str, str]) -> AdapterHealth:
    sessions, database = _cline_locations(environment)
    guidance = _override_guidance("cline")
    sessions_ready = sessions.is_dir() and os.access(sessions, os.R_OK | os.X_OK)
    database_ready = (
        database.is_file()
        and os.access(database, os.R_OK)
        and _sqlite_supports_query(database, _CLINE_SCHEMA_QUERY)
    )
    if sessions_ready or database_ready:
        source = (
            "SQLite and file-backed"
            if sessions_ready and database_ready
            else ("file-backed" if sessions_ready else "SQLite")
        )
        return AdapterHealth(
            "cline",
            AdapterHealthStatus.AVAILABLE,
            f"The {source} session store is ready; no Side Dog hooks are needed."
            + guidance,
        )
    configured = any(name.startswith("CLINE_") for name in environment)
    if sessions.exists() or database.exists() or configured:
        return AdapterHealth(
            "cline",
            AdapterHealthStatus.DEGRADED,
            "The configured Cline session stores are unreadable or have an unsupported shape."
            + guidance,
        )
    return AdapterHealth(
        "cline",
        AdapterHealthStatus.UNAVAILABLE,
        "No local Cline session store was found yet; activity will appear after Cline runs."
        + guidance,
    )


def antigravity_readiness(_root: Path, environment: Mapping[str, str]) -> AdapterHealth:
    configured = environment.get("ANTIGRAVITY_APP_DATA_DIR")
    gemini_home = environment.get("GEMINI_HOME")
    if configured:
        candidates = [Path(configured).expanduser()]
    else:
        base = (
            Path(gemini_home).expanduser()
            if gemini_home
            else _home(environment) / ".gemini"
        )
        candidates = [
            base,
            base / "antigravity-cli",
            base / "antigravity",
            base / "antigravity-ide",
        ]
    guidance = _override_guidance("antigravity")
    for candidate in candidates:
        brain = candidate / "brain"
        if brain.is_dir() and os.access(brain, os.R_OK | os.X_OK):
            return AdapterHealth(
                "antigravity",
                AdapterHealthStatus.AVAILABLE,
                "Native local session discovery is ready; no Side Dog hooks are needed."
                + guidance,
            )
    incomplete_source = bool(configured or gemini_home) or any(
        candidate.exists() for candidate in candidates[1:]
    )
    if incomplete_source:
        return AdapterHealth(
            "antigravity",
            AdapterHealthStatus.DEGRADED,
            "The configured Antigravity session location is missing or unreadable."
            + guidance,
        )
    return AdapterHealth(
        "antigravity",
        AdapterHealthStatus.UNAVAILABLE,
        "No local Antigravity session directory yet; activity will appear after Antigravity runs."
        + guidance,
    )


def _claude_hooks_installed(settings: Path, root: Path) -> bool:
    try:
        document = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return False
    from side_dog.cli import desired_hooks, is_side_dog_hook_command

    expected_hooks = desired_hooks("side-dog hook --root .")
    ready_events: set[str] = set()
    for event_name, entries in hooks.items():
        expected_entries = expected_hooks.get(event_name)
        if not expected_entries:
            continue
        expected_matcher = expected_entries[0].get("matcher")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("matcher") != expected_matcher:
                continue
            commands = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(commands, list):
                continue
            for command_entry in commands:
                command = (
                    command_entry.get("command")
                    if isinstance(command_entry, dict)
                    else None
                )
                if not isinstance(command, str):
                    continue
                if not is_side_dog_hook_command(command):
                    continue
                try:
                    tokens = shlex.split(command)
                    if tokens[:1] == ["SIDE_DOG_MANAGED=1"]:
                        tokens = tokens[1:]
                    selected = tokens[tokens.index("--root") + 1]
                except (ValueError, IndexError):
                    continue
                executable = Path(tokens[0]).expanduser()
                if executable.is_absolute():
                    executable_ready = executable.is_file() and os.access(
                        executable, os.X_OK
                    )
                else:
                    executable_ready = shutil.which(tokens[0]) is not None
                if not executable_ready:
                    continue
                if executable.name.startswith("python"):
                    if len(tokens) < 2 or not Path(tokens[1]).is_file():
                        continue
                if Path(selected).expanduser().resolve(strict=False) == root:
                    ready_events.add(event_name)
                    break
    return ready_events.issuperset(expected_hooks)


def claude_readiness(root: Path, environment: Mapping[str, str]) -> AdapterHealth:
    registry = _home(environment) / ".claude" / "sessions"
    settings = root / ".claude" / "settings.local.json"
    discovery = registry.is_dir() and os.access(registry, os.R_OK | os.X_OK)
    hooks = _claude_hooks_installed(settings, root)
    if hooks:
        return AdapterHealth(
            "claude-code",
            AdapterHealthStatus.AVAILABLE,
            "Session discovery and project-local Side Dog activity hooks are ready.",
        )
    if discovery or settings.exists() or shutil.which("claude") is not None:
        return AdapterHealth(
            "claude-code",
            AdapterHealthStatus.DEGRADED,
            "Sessions can be named, but project activity hooks are absent; only unattributed file changes appear.",
        )
    return AdapterHealth(
        "claude-code",
        AdapterHealthStatus.UNAVAILABLE,
        "No local Claude Code sessions or project hooks were found. Run `side-dog setup . --claude` to add activity hooks for this project.",
    )


def _adapter_readiness(
    descriptor: IntegrationDescriptor, health: AdapterHealth
) -> Readiness:
    status = {
        AdapterHealthStatus.AVAILABLE: "ok",
        AdapterHealthStatus.DEGRADED: "warn",
        AdapterHealthStatus.UNAVAILABLE: "info",
        AdapterHealthStatus.DISABLED: "info",
        AdapterHealthStatus.UNKNOWN: "info",
    }[health.status]
    return Readiness(f"{descriptor.product_name} discovery", status, health.detail)


def integration_readiness(
    descriptor: IntegrationDescriptor,
    root: Path,
    environment: Mapping[str, str],
) -> AdapterHealth:
    """Run one registered local probe without letting diagnostics stop doctor."""
    probe = descriptor.readiness_probe
    if probe is None:
        return AdapterHealth(
            descriptor.provider,
            AdapterHealthStatus.UNKNOWN,
            "No readiness check is registered for this integration.",
        )
    try:
        health = probe(root, environment)
    except Exception:
        return AdapterHealth(
            descriptor.provider,
            AdapterHealthStatus.DEGRADED,
            "The local readiness check could not be completed.",
        )
    if not isinstance(health, AdapterHealth) or health.adapter != descriptor.provider:
        return AdapterHealth(
            descriptor.provider,
            AdapterHealthStatus.DEGRADED,
            "The local readiness check returned an invalid result.",
        )
    return health


def codex_probe(environment: Mapping[str, str]) -> Readiness:
    descriptor = next(item for item in INTEGRATIONS if item.provider == "codex")
    return _adapter_readiness(descriptor, codex_readiness(Path.cwd(), environment))


def cline_probe(environment: Mapping[str, str]) -> Readiness:
    descriptor = next(item for item in INTEGRATIONS if item.provider == "cline")
    return _adapter_readiness(
        descriptor, cline_session_sources(Path.cwd(), environment)
    )


def antigravity_probe(environment: Mapping[str, str]) -> Readiness:
    descriptor = next(item for item in INTEGRATIONS if item.provider == "antigravity")
    return _adapter_readiness(
        descriptor, antigravity_readiness(Path.cwd(), environment)
    )


def claude_probe(root: Path) -> Readiness:
    descriptor = next(item for item in INTEGRATIONS if item.provider == "claude-code")
    return _adapter_readiness(descriptor, claude_readiness(root, os.environ))


def herdr_probe(environment: Mapping[str, str]) -> Readiness:
    inherited = environment.get("HERDR_ENV") == "1" or bool(
        environment.get("HERDR_SOCKET_PATH")
    )
    if shutil.which("herdr") is None:
        return Readiness(
            "Herdr",
            "info",
            "Optional Herdr is absent; pane, tab, workspace, and terminal-title context are unavailable.",
        )
    snapshot = _completed(["herdr", "api", "snapshot"], timeout=2.0)
    if snapshot is None or snapshot.returncode != 0:
        context = "inherited session, but " if inherited else ""
        return Readiness(
            "Herdr",
            "warn",
            f"Optional Herdr {context}snapshot health check failed; session-scoped discovery is unavailable.",
        )
    try:
        document = json.loads(snapshot.stdout)
        value = document["result"]["snapshot"]
        if not isinstance(value, dict) or not isinstance(value.get("agents"), list):
            raise TypeError
    except (json.JSONDecodeError, KeyError, TypeError):
        return Readiness(
            "Herdr",
            "warn",
            "Optional Herdr returned an invalid snapshot; session-scoped discovery is unavailable.",
        )
    session = " with inherited session context" if inherited else ""
    return Readiness("Herdr", "ok", f"Optional snapshot is healthy{session}.")


def _render(check: Readiness, color: bool) -> str:
    labels = {
        "ok": ("OK", GREEN),
        "info": ("INFO optional", CYAN),
        "warn": ("WARN optional", YELLOW),
        "fail": ("FAIL required", RED),
    }
    label, tone = labels[check.status]
    marker = f"[{label}]"
    if color:
        marker = f"{tone}{marker}{RESET}"
    return f"{marker} {check.name}: {check.detail}"


def doctor(
    project: str = ".",
    *,
    no_color: bool = False,
    environment: Mapping[str, str] | None = None,
    output: TextIO | None = None,
    project_explicit: bool = False,
) -> int:
    stream = sys.stdout if output is None else output
    values = os.environ if environment is None else environment
    root = Path(project).expanduser().resolve(strict=False)
    checks = [
        Readiness("Side Dog", "ok", f"Version {__version__}.", required=True),
        Readiness(
            "Python",
            "ok" if sys.version_info >= (3, 11) else "fail",
            f"Version {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}.",
            required=True,
        ),
    ]
    if root.is_dir():
        checks.extend((git_probe(root), github_probe(root)))
        checks.extend(
            _adapter_readiness(
                descriptor, integration_readiness(descriptor, root, values)
            )
            for descriptor in INTEGRATIONS
        )
        checks.append(herdr_probe(values))
    else:
        checks.append(
            Readiness(
                "Project folder",
                "fail",
                "The selected folder does not exist.",
                required=True,
            )
        )

    inherited = values.get("HERDR_ENV") == "1" or bool(values.get("HERDR_SOCKET_PATH"))
    healthy_herdr = any(
        check.name == "Herdr" and check.status == "ok" for check in checks
    )
    if inherited and healthy_herdr and not project_explicit:
        mode = "Herdr session"
        watch_command = "side-dog watch"
        panel_command = "side-dog panel"
    else:
        mode = "explicit folder"
        selected = "." if project == "." else shlex.quote(os.fspath(root))
        watch_command = f"side-dog watch {selected}"
        panel_command = f"side-dog panel {selected}"

    use_color = not no_color and bool(getattr(stream, "isatty", lambda: False)())
    print(f"Side Dog doctor — {root}", file=stream)
    for check in checks:
        print(_render(check, use_color), file=stream)
    print(f"Mode: {mode}", file=stream)
    print(f"Recommended: {watch_command}", file=stream)
    print(f"Browser: {panel_command}", file=stream)
    return (
        1 if any(check.required and check.status == "fail" for check in checks) else 0
    )
