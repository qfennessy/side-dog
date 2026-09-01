from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, TextIO

from side_dog import __version__


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
    command: list[str], timeout: float = 3.0
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
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


def _git_remote_host(root: Path) -> str:
    remote = _completed(["git", "-C", os.fspath(root), "remote", "get-url", "origin"])
    if remote is None or remote.returncode != 0:
        return "github.com"
    value = remote.stdout.strip()
    if "://" in value:
        from urllib.parse import urlsplit

        return urlsplit(value).hostname or "github.com"
    if "@" in value and ":" in value:
        return value.split("@", 1)[1].split(":", 1)[0] or "github.com"
    return "github.com"


def github_probe(root: Path) -> Readiness:
    if shutil.which("gh") is None:
        return Readiness(
            "GitHub readback",
            "info",
            "Optional gh CLI is absent; pull-request status will not be verified.",
        )
    authenticated = _completed(
        ["gh", "auth", "status", "--hostname", _git_remote_host(root)]
    )
    if authenticated is None or authenticated.returncode != 0:
        return Readiness(
            "GitHub readback",
            "warn",
            "Optional gh CLI is not authenticated; pull-request status will not be verified.",
        )
    return Readiness("GitHub readback", "ok", "Optional authenticated gh CLI is ready.")


def codex_probe(environment: Mapping[str, str]) -> Readiness:
    configured = environment.get("CODEX_HOME")
    home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    sessions = home / "sessions"
    if not sessions.is_dir():
        return Readiness(
            "Codex discovery",
            "info",
            "No local Codex session directory yet; Codex activity will appear after Codex runs.",
        )
    return Readiness(
        "Codex discovery",
        "ok",
        "Native local session discovery is ready; no Side Dog hooks are needed.",
    )


def _claude_hooks_installed(settings: Path, root: Path) -> bool:
    try:
        document = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return False
    from side_dog.cli import is_side_dog_entry

    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not is_side_dog_entry(entry):
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
                try:
                    tokens = shlex.split(command)
                    selected = tokens[tokens.index("--root") + 1]
                except (ValueError, IndexError):
                    continue
                if Path(selected).expanduser().resolve(strict=False) == root:
                    return True
    return False


def claude_probe(root: Path) -> Readiness:
    registry = Path.home() / ".claude" / "sessions"
    settings = root / ".claude" / "settings.local.json"
    discovery = registry.is_dir()
    hooks = _claude_hooks_installed(settings, root)
    if hooks:
        return Readiness(
            "Claude discovery",
            "ok",
            "Session discovery and project-local Side Dog activity hooks are ready.",
        )
    if discovery:
        return Readiness(
            "Claude discovery",
            "warn",
            "Sessions can be named, but project activity hooks are absent; only unattributed file changes appear.",
        )
    return Readiness(
        "Claude discovery",
        "info",
        "No local Claude sessions or project hooks found; Claude-specific activity is unavailable.",
    )


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
        checks.extend(
            (
                git_probe(root),
                github_probe(root),
                codex_probe(values),
                claude_probe(root),
                herdr_probe(values),
            )
        )
    else:
        checks.append(
            Readiness(
                "Project folder",
                "fail",
                "The selected folder does not exist.",
                required=True,
            )
        )

    inherited = values.get("HERDR_ENV") == "1" or bool(
        values.get("HERDR_SOCKET_PATH")
    )
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
    return 1 if any(check.required and check.status == "fail" for check in checks) else 0
