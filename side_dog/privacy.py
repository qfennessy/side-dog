"""Privacy policy for coding-agent observations crossing into durable events.

Raw observations exist only in memory.  Only :class:`SafeEvent` values created
here may be written to Side Dog's JSONL history or sent to the browser panel.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field as dataclass_field, fields
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from .integrations import (
    ACTIVITY_SCHEMA,
    MAX_SAFE_INTEGER,
    PANEL_SAFE_EVENT_FIELDS,
    SAFE_EVENT_FIELDS,
    SafeEvent,
)


class PrivacyRejectionReason(StrEnum):
    NOT_AN_EVENT = "not_an_event"
    UNEXPECTED_FIELD = "unexpected_field"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_AGENT = "invalid_agent"
    INVALID_KIND = "invalid_kind"
    INVALID_STATUS = "invalid_status"
    INVALID_VALUE = "invalid_value"
    PROJECT_MISMATCH = "project_mismatch"
    OUTSIDE_PROJECT = "outside_project"
    AMBIGUOUS_OBSERVATION = "ambiguous_observation"


class PrivacyRejection(ValueError):
    """A fixed, privacy-safe reason that never includes rejected input."""

    def __init__(self, reason: PrivacyRejectionReason):
        self.reason = PrivacyRejectionReason(reason)
        super().__init__(f"activity rejected: {self.reason.value}")


@dataclass(frozen=True, slots=True)
class EventObservation:
    """Ephemeral collector output; raw source values must never be serialized."""

    agent: str = "unknown"
    session_id: str | None = None
    kind: str = ""
    status: str = "unknown"
    title: str = ""
    detail: str = ""
    timestamp: str = ""
    epoch_ms: int | None = None
    operation_id: str = ""
    task_stage_id: str = ""
    group_id: str = ""
    source_event_id: str = ""
    turn_id: str = ""
    model: str = ""
    effort: str = ""
    started_epoch_ms: int | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    url: str = ""
    git_oid: str = ""
    herdr_pane_id: str = ""
    herdr_tab_id: str = ""
    herdr_workspace_id: str = ""
    github_state: str = ""
    github_fingerprint: str = ""
    github: Mapping[str, Any] | None = None
    path: str = dataclass_field(default="", repr=False)
    cwd: str = dataclass_field(default="", repr=False)
    command: str = dataclass_field(default="", repr=False)


_SAFE_TITLES_BY_KIND = {
    "branch": frozenset(
        {
            "Branch command finished",
            "Branch created",
            "Branch creation failed",
            "Branch switched",
            "Branch switch command finished",
            "Branch switch failed",
            "Creating branch",
            "Switching branch",
        }
    ),
    "command": frozenset({"Command failed"}),
    "commit": frozenset(
        {
            "Commit command finished",
            "Commit created",
            "Commit failed",
            "Creating commit",
        }
    ),
    "config": frozenset(
        {
            "Claude config changed",
            "Config changed",
            "Config removed",
            "Config write failed",
            "Removed config",
            "Writing config",
            "Wrote config",
        }
    ),
    "file": frozenset(
        {
            "File changed",
            "File removed",
            "File write failed",
            "Removed file",
            "Writing file",
            "Wrote file",
        }
    ),
    "issue": frozenset(
        {
            "Closed issue",
            "Closing issue",
            "Issue command finished",
            "Issue update failed",
            "Opened issue",
            "Opening issue",
            "Reopened issue",
            "Reopening issue",
        }
    ),
    "merge": frozenset(
        {
            "Branch merge command finished",
            "Branch merge failed",
            "Branch merged",
            "Merging branch",
            "Merging pull request",
            "PR merge command finished",
            "PR merge command succeeded",
            "Pull request merge failed",
        }
    ),
    "pr": frozenset(
        {
            "Opening pull request",
            "PR create command finished",
            "PR create command succeeded",
            "Pull request creation failed",
        }
    ),
    "push": frozenset(
        {
            "Branch pushed",
            "Push failed",
            "Push command finished",
            "Pushing branch",
        }
    ),
    "search": frozenset(
        {"Fetched web page", "Read file", "Searched code", "Searched files"}
    ),
    "session": frozenset(
        {
            "Agent activity omitted",
            "Antigravity turn started",
            "Claude session active",
            "Claude session ended",
            "Claude turn finished",
            "Crush turn finished",
            "DeepSeek turn finished",
            "Opencode turn finished",
            "Observed activity",
            "Pi session active",
            "Pi turn finished",
            "Session started",
            "Side Dog history backfill complete",
            "Side Dog caught up on earlier activity",
            "Subagent active",
            "Subagent cancelled",
            "Subagent completed",
            "Subagent failed",
            "Subagent started",
            "Subagent status unknown",
            "Turn completed",
            "Turn started",
            "Transcript backfill complete",
        }
    ),
    "test": frozenset(
        {"Running tests", "Tests failed", "Tests finished", "Tests passed"}
    ),
    "todo": frozenset({"Todo updated"}),
    "worktree": frozenset(
        {
            "Creating worktree",
            "Removing worktree",
            "Worktree command finished",
            "Worktree update failed",
            "Worktree updated",
        }
    ),
}
_GITHUB_TITLE = re.compile(
    r"^PR #[1-9][0-9]* (?:approved|changes requested|checks failed|"
    r"checks passed|checks started|closed|confirmed|merged|reopened|status updated)$"
)
_SAFE_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@-]{0,63}$")
_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,254}$")
_SAFE_GIT_OID_DETAIL = re.compile(r"^[0-9a-fA-F]{7,64}(?: · [^\r\n]{1,240})?$")
_SAFE_ISSUE_NUMBER = re.compile(r"^(?:issue )?#[1-9][0-9]*$")
_SAFE_HISTORY_DETAIL = re.compile(r"^[0-9]+ earlier events? (?:added|already saved)$")
_SAFE_LEGACY_HISTORY_DETAIL = re.compile(
    r"^[0-9]+ (?:(?:activity|native) )?events? (?:available|recovered)$"
)
_SAFE_SUBAGENT_DETAIL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+@,-]{0,79}$")
_SAFE_TEST_DETAILS = frozenset(
    {
        "bun",
        "cargo test",
        "go test",
        "jest",
        "make",
        "mix test",
        "npm",
        "one intentional demo failure",
        "pnpm",
        "pytest",
        "rspec",
        "test suite",
        "unittest",
        "vitest",
        "yarn",
    }
)
_SAFE_TASK_STAGE_ID = re.compile(
    r"^(branch|commit|issue|merge|pr|push|test|worktree):[0-9a-f]{16}$"
)
_CLAUDE_SESSION_SOURCES = frozenset({"clear", "compact", "resume", "start", "startup"})
_CLAUDE_END_REASONS = frozenset(
    {"clear", "complete", "logout", "other", "prompt_input_exit"}
)
_SESSION_EMPTY_DETAILS = frozenset(
    {
        "Antigravity turn started",
        "Claude turn finished",
        "Crush turn finished",
        "DeepSeek turn finished",
        "Opencode turn finished",
        "Pi session active",
        "Pi turn finished",
    }
)
_SUBAGENT_TITLES = frozenset(
    {
        "Subagent active",
        "Subagent cancelled",
        "Subagent completed",
        "Subagent failed",
        "Subagent started",
        "Subagent status unknown",
    }
)


def _observation_fields(observation: EventObservation) -> dict[str, Any]:
    source_only = {"path", "cwd", "command"}
    return {
        item.name: getattr(observation, item.name)
        for item in fields(observation)
        if item.name not in source_only
    }


def _event_time(timestamp: Any, epoch_ms: Any, now: datetime | None) -> tuple[str, int]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    parsed_epoch: int | None = None
    parsed_timestamp: datetime | None = None
    if epoch_ms not in (None, ""):
        if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
        parsed_epoch = epoch_ms
        if parsed_epoch < 0 or parsed_epoch > MAX_SAFE_INTEGER:
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    if timestamp not in (None, ""):
        if not isinstance(timestamp, str):
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE) from None
        if parsed_timestamp.tzinfo is None:
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    if (
        parsed_epoch is not None
        and parsed_timestamp is not None
        and int(parsed_timestamp.timestamp() * 1000) != parsed_epoch
    ):
        raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    if parsed_epoch is None and parsed_timestamp is not None:
        parsed_epoch = int(parsed_timestamp.timestamp() * 1000)
    if parsed_timestamp is None and parsed_epoch is not None:
        try:
            parsed_timestamp = datetime.fromtimestamp(parsed_epoch / 1000, timezone.utc)
        except (OSError, OverflowError, ValueError):
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE) from None
    parsed_timestamp = parsed_timestamp or current
    parsed_epoch = (
        parsed_epoch if parsed_epoch is not None else int(current.timestamp() * 1000)
    )
    return parsed_timestamp.isoformat(timespec="milliseconds"), parsed_epoch


def _resolved_root(root: Path) -> Path:
    try:
        return root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE) from None


def _safe_event_title(kind: str, title: str) -> bool:
    if not title or any(ord(character) < 32 for character in title):
        return False
    if kind == "github":
        return bool(_GITHUB_TITLE.fullmatch(title))
    return title in _SAFE_TITLES_BY_KIND.get(kind, ())


def _safe_event_semantics(root: Path, wire: dict[str, Any]) -> dict[str, Any]:
    """Validate title/detail meaning, then remove command-derived free text."""

    kind = wire.get("kind")
    title = wire.get("title")
    detail = wire.get("detail", "")
    if not isinstance(kind, str) or not isinstance(title, str):
        raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    if kind == "pr" and not title:
        # Early v1 PR markers carried only their kind. Preserve their refresh
        # signal with a fixed label rather than trusting absent free text.
        title = "PR create command finished"
    if not isinstance(detail, str) or not _safe_event_title(kind, title):
        raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    safe = dict(wire)
    safe["title"] = title
    task_stage_id = safe.get("task_stage_id", "")
    if task_stage_id:
        task_stage_match = (
            _SAFE_TASK_STAGE_ID.fullmatch(task_stage_id)
            if isinstance(task_stage_id, str)
            else None
        )
        if task_stage_match is None or task_stage_match.group(1) != kind:
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)

    if kind in {"file", "config"}:
        safe["detail"] = (
            normalize_project_path(root, detail)
            if detail
            else "unknown config"
            if kind == "config"
            else "unknown file"
        )
    elif kind == "command":
        if not _SAFE_PROGRAM.fullmatch(detail):
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    elif kind == "test":
        if not detail:
            safe["detail"] = "test suite"
        elif detail not in _SAFE_TEST_DETAILS:
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    elif kind == "pr":
        # Legacy Claude hooks extracted --title. The UI needs the action, not
        # user-supplied command arguments, so all create phases use this label.
        safe["detail"] = "gh pr create"
    elif kind == "merge":
        safe["detail"] = (
            "git merge" if "branch" in title.casefold() else "gh pr merge"
        )
    elif kind == "issue":
        actions = {
            "Opening issue": "gh issue create",
            "Opened issue": "gh issue create",
            "Closing issue": "gh issue close",
            "Closed issue": "gh issue close",
            "Reopening issue": "gh issue reopen",
            "Reopened issue": "gh issue reopen",
        }
        if detail in actions.values():
            safe["detail"] = detail
        elif _SAFE_ISSUE_NUMBER.fullmatch(detail):
            safe["detail"] = detail
        else:
            safe["detail"] = actions.get(title, "gh issue")
    elif kind == "push":
        safe["detail"] = detail if _SAFE_BRANCH.fullmatch(detail) else "git push"
    elif kind == "worktree":
        safe["detail"] = (
            detail
            if detail in {"git worktree", "git worktree add"}
            else "git worktree add" if title == "Creating worktree" else "git worktree"
        )
    elif kind == "branch":
        safe["detail"] = (
            detail
            if detail == "git branch" or _SAFE_BRANCH.fullmatch(detail)
            else "git branch"
        )
    elif kind == "commit":
        safe["detail"] = (
            detail
            if detail == "git commit" or _SAFE_GIT_OID_DETAIL.fullmatch(detail)
            else "git commit"
        )
    elif kind == "github":
        # The renderer derives this text from the separately validated GitHub
        # mapping. Keeping a second free-text copy adds no value.
        safe["detail"] = ""
    elif kind == "todo":
        if not re.fullmatch(r"[0-9]+ tasks?", detail):
            raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    elif kind == "search":
        if title == "Read file":
            safe["detail"] = normalize_project_path(root, detail)
        elif title == "Fetched web page":
            safe["detail"] = "web page"
        elif title == "Searched code":
            safe["detail"] = "code"
        else:
            safe["detail"] = "files"
    elif kind == "session":
        if title in _SESSION_EMPTY_DETAILS:
            safe["detail"] = ""
        elif title in _SUBAGENT_TITLES:
            safe["detail"] = (
                detail if _SAFE_SUBAGENT_DETAIL.fullmatch(detail) else "subagent"
            )
        elif title in {
            "Side Dog caught up on earlier activity",
            "Side Dog history backfill complete",
            "Transcript backfill complete",
        }:
            pattern = (
                _SAFE_HISTORY_DETAIL
                if title == "Side Dog caught up on earlier activity"
                else _SAFE_LEGACY_HISTORY_DETAIL
            )
            if not pattern.fullmatch(detail):
                raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
        elif title == "Claude session active":
            safe["detail"] = detail if detail in _CLAUDE_SESSION_SOURCES else "start"
        elif title == "Claude session ended":
            safe["detail"] = detail if detail in _CLAUDE_END_REASONS else "complete"
        elif title == "Agent activity omitted":
            if detail not in {reason.value for reason in PrivacyRejectionReason}:
                raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
        else:
            safe["detail"] = ""
    return safe


def safe_event(
    root: Path,
    event: Mapping[str, Any] | SafeEvent,
    *,
    now: datetime | None = None,
) -> SafeEvent:
    """Validate an allowlisted event and bind it to the authoritative root.

    Valid source timing is preserved so transcript events keep their duration
    and order.  Missing timing is filled from ``now`` at the boundary.
    """

    if isinstance(event, SafeEvent):
        wire = event.to_wire()
    elif isinstance(event, Mapping):
        wire = dict(event)
    else:
        raise PrivacyRejection(PrivacyRejectionReason.NOT_AN_EVENT)
    if set(wire) - SAFE_EVENT_FIELDS:
        raise PrivacyRejection(PrivacyRejectionReason.UNEXPECTED_FIELD)
    if wire.get("schema", ACTIVITY_SCHEMA) != ACTIVITY_SCHEMA:
        raise PrivacyRejection(PrivacyRejectionReason.INVALID_SCHEMA)
    authoritative_root = _resolved_root(root)
    supplied_project = wire.get("project")
    if supplied_project:
        try:
            supplied_root = (
                Path(str(supplied_project)).expanduser().resolve(strict=False)
            )
        except (OSError, RuntimeError):
            raise PrivacyRejection(PrivacyRejectionReason.PROJECT_MISMATCH) from None
        if supplied_root != authoritative_root:
            raise PrivacyRejection(PrivacyRejectionReason.PROJECT_MISMATCH)
    wire = _safe_event_semantics(authoritative_root, wire)
    timestamp, epoch_ms = _event_time(wire.get("timestamp"), wire.get("epoch_ms"), now)
    wire.update(
        schema=ACTIVITY_SCHEMA,
        project=os.fspath(authoritative_root),
        timestamp=timestamp,
        epoch_ms=epoch_ms,
    )
    try:
        return SafeEvent.from_wire(wire)
    except (TypeError, ValueError) as error:
        message = str(error)
        if "agent provider" in message:
            reason = PrivacyRejectionReason.INVALID_AGENT
        elif "event kind" in message:
            reason = PrivacyRejectionReason.INVALID_KIND
        elif "event status" in message:
            reason = PrivacyRejectionReason.INVALID_STATUS
        else:
            reason = PrivacyRejectionReason.INVALID_VALUE
        raise PrivacyRejection(reason) from None


def normalize_project_path(root: Path, raw_path: str, cwd: str = "") -> str:
    """Return a relative display path only when it resolves inside ``root``."""

    if not isinstance(raw_path, str) or not raw_path:
        raise PrivacyRejection(PrivacyRejectionReason.INVALID_VALUE)
    authoritative_root = _resolved_root(root)
    try:
        source = Path(raw_path).expanduser()
        base = Path(cwd).expanduser() if cwd else authoritative_root
        if not base.is_absolute():
            base = authoritative_root / base
        base = base.resolve(strict=False)
        base.relative_to(authoritative_root)
        target = source if source.is_absolute() else base / source
        target = target.resolve(strict=False)
        return target.relative_to(authoritative_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        raise PrivacyRejection(PrivacyRejectionReason.OUTSIDE_PROJECT) from None


def safe_events(
    root: Path,
    observation: EventObservation,
    *,
    now: datetime | None = None,
) -> tuple[SafeEvent, ...]:
    """Normalize one ephemeral observation into zero or more safe events."""

    if not isinstance(observation, EventObservation):
        raise PrivacyRejection(PrivacyRejectionReason.NOT_AN_EVENT)
    raw_sources = sum(bool(item) for item in (observation.path, observation.command))
    if raw_sources > 1:
        raise PrivacyRejection(PrivacyRejectionReason.AMBIGUOUS_OBSERVATION)
    values = _observation_fields(observation)
    if observation.path:
        values["detail"] = normalize_project_path(
            root, observation.path, observation.cwd
        )
    if observation.command:
        classified = classify_command(observation.command, observation.status)
        if classified is None:
            return ()
        values.update(classified)
    return (safe_event(root, values, now=now),)


_COMMAND_WRAPPERS = frozenset({"command", "env", "exec", "nohup", "sudo", "time"})


def _command_words(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        return None
    if any(token and set(token) <= set(";&|") for token in words):
        return None
    return words


def _program_name(words: list[str]) -> str:
    for index, word in enumerate(words):
        if not word or "=" in word:
            continue
        name = Path(word).name.strip("\"'")
        if not name:
            continue
        if name.casefold() in _COMMAND_WRAPPERS:
            # Wrapper option grammars vary, and several options consume the
            # following token. Fail closed instead of mistaking that private
            # operand for the executable name.
            following = words[index + 1 :]
            next_non_assignment = next(
                (candidate for candidate in following if "=" not in candidate),
                "",
            )
            if next_non_assignment.startswith("-"):
                return "command"
            continue
        if word.startswith("-"):
            return "command"
        return name[:64]
    return "command"


def _phase_title(
    status: str, *, running: str, success: str, failed: str, finished: str
) -> str:
    return {
        "running": running,
        "success": success,
        "failed": failed,
    }.get(status, finished)


def classify_command(command: str, status: str = "unknown") -> dict[str, str] | None:
    """Reduce a raw command to a fixed, argument-free activity description."""

    words = _command_words(command)
    if not words:
        return None
    lowered = [word.casefold() for word in words]
    program = _program_name(words)
    direct_tests = {"pytest", "unittest", "vitest", "jest", "rspec"}
    is_test = any(Path(word).name.casefold() in direct_tests for word in words)
    is_test = is_test or any(
        lowered[index : index + 2] in (["cargo", "test"], ["go", "test"])
        for index in range(len(lowered) - 1)
    )
    is_test = is_test or any(
        Path(word).name.casefold() in {"npm", "pnpm", "yarn", "bun"}
        and any(
            candidate in {"test", "vitest", "jest"}
            for candidate in lowered[index + 1 :]
        )
        for index, word in enumerate(words)
    )
    if is_test:
        title = {
            "running": "Running tests",
            "success": "Tests passed",
            "failed": "Tests failed",
        }.get(status, "Tests finished")
        runner = next(
            (
                Path(word).name
                for word in words
                if Path(word).name.casefold() in direct_tests
            ),
            "test suite",
        )
        return {"kind": "test", "title": title, "detail": runner}
    for index, word in enumerate(words[:-1]):
        if Path(word).name.casefold() != "git":
            continue
        action = lowered[index + 1]
        remainder = lowered[index + 2 :]
        if action == "commit":
            title = _phase_title(
                status,
                running="Creating commit",
                success="Commit created",
                failed="Commit failed",
                finished="Commit command finished",
            )
            return {"kind": "commit", "title": title, "detail": "git commit"}
        if action == "push":
            title = _phase_title(
                status,
                running="Pushing branch",
                success="Branch pushed",
                failed="Push failed",
                finished="Push command finished",
            )
            return {"kind": "push", "title": title, "detail": "git push"}
        if action in {"checkout", "switch"}:
            creates = any(flag in {"-b", "-c", "--create"} for flag in remainder)
            title = (
                _phase_title(
                    status,
                    running="Creating branch",
                    success="Branch created",
                    failed="Branch creation failed",
                    finished="Branch command finished",
                )
                if creates
                else _phase_title(
                    status,
                    running="Switching branch",
                    success="Branch switched",
                    failed="Branch switch failed",
                    finished="Branch switch command finished",
                )
            )
            return {"kind": "branch", "title": title, "detail": "git branch"}
        if action == "worktree":
            adds = bool(remainder and remainder[0] == "add")
            removes = bool(remainder and remainder[0] in {"prune", "remove"})
            running_title = (
                "Creating worktree"
                if adds
                else "Removing worktree" if removes else "Creating worktree"
            )
            title = _phase_title(
                status,
                running=running_title,
                success="Worktree updated",
                failed="Worktree update failed",
                finished="Worktree command finished",
            )
            detail = "git worktree add" if adds else "git worktree"
            return {"kind": "worktree", "title": title, "detail": detail}
        if action == "merge":
            title = _phase_title(
                status,
                running="Merging branch",
                success="Branch merged",
                failed="Branch merge failed",
                finished="Branch merge command finished",
            )
            return {"kind": "merge", "title": title, "detail": "git merge"}
    github_actions = {
        ("pr", "create"): (
            "pr",
            (
                "Opening pull request",
                "PR create command succeeded",
                "Pull request creation failed",
                "PR create command finished",
            ),
        ),
        ("pr", "merge"): (
            "merge",
            (
                "Merging pull request",
                "PR merge command succeeded",
                "Pull request merge failed",
                "PR merge command finished",
            ),
        ),
        ("issue", "create"): (
            "issue",
            (
                "Opening issue",
                "Opened issue",
                "Issue update failed",
                "Issue command finished",
            ),
        ),
        ("issue", "close"): (
            "issue",
            (
                "Closing issue",
                "Closed issue",
                "Issue update failed",
                "Issue command finished",
            ),
        ),
        ("issue", "reopen"): (
            "issue",
            (
                "Reopening issue",
                "Reopened issue",
                "Issue update failed",
                "Issue command finished",
            ),
        ),
    }
    for index, word in enumerate(words[:-2]):
        action = tuple(lowered[index + 1 : index + 3])
        if Path(word).name.casefold() == "gh" and action in github_actions:
            kind, titles = github_actions[action]
            title = {
                "running": titles[0],
                "success": titles[1],
                "failed": titles[2],
            }.get(status, titles[3])
            return {"kind": kind, "title": title, "detail": f"gh {' '.join(action)}"}
    if status == "failed":
        return {"kind": "command", "title": "Command failed", "detail": program[:64]}
    return None


def rejection_diagnostic(
    root: Path,
    provider: str,
    reason: PrivacyRejectionReason,
    *,
    now: datetime | None = None,
) -> SafeEvent:
    """Build a diagnostic that exposes a fixed reason, never rejected data."""

    selected = PrivacyRejectionReason(reason)
    return safe_event(
        root,
        {
            "agent": provider,
            "kind": "session",
            "status": "unknown",
            "title": "Agent activity omitted",
            "detail": selected.value,
        },
        now=now,
    )


# Exported for panel defense-in-depth; values are derived from the domain type.
SAFE_PANEL_WIRE_FIELDS = PANEL_SAFE_EVENT_FIELDS


__all__ = [
    "EventObservation",
    "PrivacyRejection",
    "PrivacyRejectionReason",
    "SAFE_PANEL_WIRE_FIELDS",
    "classify_command",
    "normalize_project_path",
    "rejection_diagnostic",
    "safe_event",
    "safe_events",
]
