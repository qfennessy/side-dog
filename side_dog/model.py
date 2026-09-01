from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, tzinfo
from typing import Any, Iterable


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
MILESTONE_KINDS = DELIVERY_KINDS | {"session"}
FILESYSTEM_BURST_GAP_MS = 2 * 60 * 1000
GITHUB_CONFIRMATION_GAP_MS = 60 * 1000
SOURCE_KEY = "_side_dog_source_key"
SOURCE_LABEL = "_side_dog_source_label"
MODEL_VENDOR_PREFIXES = ("us.", "eu.", "apac.", "anthropic.", "claude-")
MODEL_RELEASE_SUFFIX = re.compile(r"-\d{6,8}(?:-v\d+(?::\d+)?)?$")
CONVENTIONAL_SUBJECT = re.compile(
    r"^(?P<prefix>(?:[0-9a-f]{7,12}\s+·\s+)?)"
    r"(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([^\r\n)]+\))?!?:\s*",
    re.IGNORECASE,
)


def event_source_key(event: dict[str, Any]) -> str:
    return str(event.get(SOURCE_KEY, ""))


def event_source_label(event: dict[str, Any]) -> str:
    return str(event.get(SOURCE_LABEL, ""))


def event_root(event: dict[str, Any]) -> str:
    return event_source_key(event) or str(event.get("project", ""))


def normalize_agent(value: Any) -> str:
    agent = str(value or "").strip().casefold()
    if agent in {"claude", "claude-code"}:
        return "claude-code"
    if agent == "codex":
        return "codex"
    return agent or "claude-code"


def agent_label(value: Any) -> str:
    return {
        "claude-code": "Claude",
        "codex": "Codex",
        "filesystem": "Filesystem",
        "git": "Git",
    }.get(normalize_agent(value), str(value or "Agent").title())


def display_model(value: Any) -> str:
    """Trim the vendor wrapping off a model id so a narrow pane keeps the name.

    "claude-opus-5" reads as "opus-5" and leaves room for the session title.
    An unfamiliar id is left alone rather than guessed at.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    trimmed = text.rsplit("/", 1)[-1]
    for prefix in MODEL_VENDOR_PREFIXES:
        if trimmed.casefold().startswith(prefix) and len(trimmed) > len(prefix):
            trimmed = trimmed[len(prefix) :]
    trimmed = MODEL_RELEASE_SUFFIX.sub("", trimmed)
    return trimmed or text


def display_conventional_subject(value: Any) -> str:
    text = str(value or "")
    match = CONVENTIONAL_SUBJECT.match(text)
    if not match:
        return text
    subject = text[match.end() :]
    return f"{match.group('prefix')}{subject}" if subject else text


def coalesce_operations(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    indexes: dict[tuple[str, str], int] = {}
    for append_ordinal, original in enumerate(records):
        record = {**original, "_append_ordinal": append_ordinal}
        raw_identifier = (
            None if record.get("kind") == "github" else record.get("operation_id")
        )
        identifier = (
            (event_root(record), raw_identifier)
            if isinstance(raw_identifier, str)
            else None
        )
        if identifier is not None and identifier in indexes:
            index = indexes[identifier]
            previous = output[index]
            merged = {**previous, **record}
            merged["started_epoch_ms"] = previous.get(
                "started_epoch_ms", previous.get("epoch_ms")
            )
            output[index] = merged
        else:
            if record.get("status") == "running":
                record["started_epoch_ms"] = record.get("epoch_ms")
            if identifier is not None:
                indexes[identifier] = len(output)
            output.append(record)
    return output


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
        ci = "CI —"
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
    """Identify a pull request by what the pane actually shows.

    GitHub churns fields Side Dog never displays - mergeable flips to UNKNOWN
    the moment a pull request merges - and comparing those produced "status
    updated" lines that repeated the line above them word for word.
    """
    material = {"number": status.get("number"), "detail": github_detail(status)}
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[
        :16
    ]


def display_merge_state(status: dict[str, Any]) -> str:
    """Name the merge state, unless GitHub has stopped answering the question.

    GitHub reports UNKNOWN once a pull request is merged or closed, and again
    for a few seconds after a push while it works the answer out. That word
    reads like a problem, so Side Dog leaves it out.
    """
    merge_state = str(status.get("merge_state") or "").upper()
    return "" if merge_state == "UNKNOWN" else merge_state


def carry_forward_merge_state(
    status: dict[str, Any], previous: dict[str, Any] | None
) -> dict[str, Any]:
    """Keep the last known merge state while GitHub is recomputing it.

    GitHub answers UNKNOWN for a few seconds after a push. Dropping the word
    and restoring it moments later reads as two changes that never happened.
    """
    if not isinstance(previous, dict):
        return status
    if str(status.get("merge_state") or "").upper() != "UNKNOWN":
        return status
    if status.get("state") != "OPEN" or previous.get("state") != "OPEN":
        return status
    carried = str(previous.get("merge_state") or "")
    if not carried or carried.upper() == "UNKNOWN":
        return status
    return {**status, "merge_state": carried}


def github_detail(status: dict[str, Any]) -> str:
    pieces = []
    title = str(status.get("title") or "")
    if title:
        pieces.append(title)
    pieces.extend([str(status.get("state", "UNKNOWN")), str(status.get("ci", "CI ?"))])
    if status.get("draft"):
        pieces.insert(1, "DRAFT")
    if status.get("review"):
        pieces.append(str(status["review"]))
    merge_state = display_merge_state(status)
    if merge_state:
        pieces.append(merge_state)
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
                "agent",
                "session_id",
                "turn_id",
                "group_id",
                "model",
                "effort",
                "herdr_pane_id",
                "herdr_tab_id",
                "herdr_workspace_id",
            )
            if key in event
        }
    return {}


def identity_for_event(
    event: dict[str, Any], identities: dict[str, dict[str, str]]
) -> dict[str, str]:
    session_id = str(event.get("session_id", ""))
    pane_id = str(event.get("herdr_pane_id", ""))
    source_key = event_source_key(event)
    identity = (
        identities.get(f"{source_key}:{session_id}")
        if source_key and session_id
        else None
    )
    if identity is None and source_key and pane_id:
        identity = identities.get(f"{source_key}:pane:{pane_id}")
    identity = (
        identity or identities.get(session_id) or identities.get(f"pane:{pane_id}")
    )
    if identity is not None:
        return identity
    agent = normalize_agent(event.get("agent"))
    if pane_id:
        return {
            "agent": agent,
            "pane_id": pane_id,
            "workspace_id": str(event.get("herdr_workspace_id", "")),
            "tab_id": str(event.get("herdr_tab_id", "")),
            "status": "unknown",
            "label": agent_label(agent),
            "model": str(event.get("model", "")),
            "effort": str(event.get("effort", "")),
        }
    if session_id:
        return {
            "agent": agent,
            "pane_id": "",
            "workspace_id": "",
            "tab_id": "",
            "status": "unknown",
            "label": f"{agent_label(agent)} {session_id[:8]}",
            "model": str(event.get("model", "")),
            "effort": str(event.get("effort", "")),
        }
    agent = normalize_agent(event.get("agent") or "filesystem")
    return {
        "agent": agent,
        "pane_id": "",
        "workspace_id": "",
        "tab_id": "",
        "status": "unknown",
        "label": agent_label(agent),
        "model": str(event.get("model", "")),
        "effort": str(event.get("effort", "")),
    }


def lane_key(event: dict[str, Any], identities: dict[str, dict[str, str]]) -> str:
    if not event.get("session_id"):
        return str(event.get("agent") or "filesystem")
    identity = identity_for_event(event, identities)
    return identity.get("pane_id") or str(event.get("session_id", "unknown"))


def lane_label(identity: dict[str, str]) -> str:
    pane_id = identity.get("pane_id", "")
    label = identity.get("label", "Claude")
    status = identity.get("status", "unknown")
    prefix = f"{pane_id} · " if pane_id else ""
    suffix = f" · {status}" if status not in {"", "unknown"} else ""
    return f"{prefix}{label}{suffix}"


def actor_label(event: dict[str, Any], identities: dict[str, dict[str, str]]) -> str:
    if event.get("kind") == "github":
        return ""
    agent = normalize_agent(event.get("agent"))
    if agent in {"filesystem", "git", "github"}:
        return ""
    identity = identity_for_event(event, identities)
    return agent_label(identity.get("agent", agent))


def local_date_for_epoch(
    epoch_ms: Any, local_timezone: tzinfo | None = None
) -> date | None:
    if not isinstance(epoch_ms, int):
        return None
    if local_timezone is None:
        return datetime.fromtimestamp(epoch_ms / 1000).astimezone().date()
    return datetime.fromtimestamp(epoch_ms / 1000, local_timezone).date()


def event_local_date(
    event: dict[str, Any], local_timezone: tzinfo | None = None
) -> date | None:
    return local_date_for_epoch(event.get("epoch_ms"), local_timezone)


def collapse_repeated_filesystem_events(
    events: list[dict[str, Any]], local_timezone: tzinfo | None = None
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
            event_root(event),
            event.get("agent"),
            event.get("kind"),
            event.get("status"),
            event.get("title"),
            event.get("detail"),
        )
        same_day = bool(
            collapsed
            and event_local_date(event, local_timezone)
            == event_local_date(collapsed[-1], local_timezone)
        )
        if collapsible and key == previous_key and collapsed and same_day:
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


def event_epoch(event: dict[str, Any]) -> int:
    value = event.get("epoch_ms")
    return value if isinstance(value, int) else 0


def is_native_history_event(event: dict[str, Any]) -> bool:
    source_event_id = str(event.get("source_event_id", ""))
    return any(
        marker in source_event_id
        for marker in (":history-indexed", ":history-backfill-complete")
    )


def is_passive_file_event(event: dict[str, Any]) -> bool:
    return event.get("agent") == "filesystem" and event.get("kind") in {
        "file",
        "config",
    }


def activity_title(events: list[dict[str, Any]]) -> str:
    kinds = {str(event.get("kind")) for event in events}
    if kinds & {"test", "commit", "push", "pr", "github", "merge"}:
        return "Agent task"
    if "issue" in kinds:
        return "Issue update"
    return "Git workflow"


def pipeline_stage(events: list[dict[str, Any]]) -> str:
    latest = max(events, key=event_epoch)
    kind = str(latest.get("kind", "activity"))
    status = str(latest.get("status", "success"))
    outcome = {"success": "✓", "failed": "×", "running": "…", "unknown": "?"}.get(
        status, "?"
    )
    count = sum(int(event.get("repeat_count", 1)) for event in events)
    if kind in {"file", "config"}:
        return f"Edit ×{count}"
    if kind == "test":
        return f"Tests {outcome}" if count == 1 else f"Tests ×{count} {outcome}"
    if kind == "commit":
        detail = display_conventional_subject(latest.get("detail", ""))
        match = re.match(r"([0-9a-f]{7,12})", detail, re.IGNORECASE)
        return f"Commit {match.group(1) if match else outcome}"
    if kind == "push":
        return f"Push {outcome}"
    if kind in {"pr", "github"}:
        number = (
            latest.get("github", {}).get("number")
            if isinstance(latest.get("github"), dict)
            else None
        )
        return f"PR #{number}" if number else f"PR {outcome}"
    if kind == "merge":
        return f"Merge {outcome}"
    if kind == "issue":
        title = str(latest.get("title", "Issue updated"))
        verb = (
            "opened"
            if "open" in title.casefold()
            else "closed"
            if "clos" in title.casefold()
            else "updated"
        )
        return f"Issue {verb}"
    if kind == "branch":
        return "Branch"
    if kind == "worktree":
        return "Worktree"
    return f"{kind.title()} {outcome}"


def pipeline_stages(events: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for event in sorted(events, key=event_epoch):
        kind = str(event.get("kind", "activity"))
        key = kind
        if kind in {"file", "config"}:
            key = "edit"
        elif kind in {"pr", "github"}:
            key = "pr"
        elif kind == "issue":
            key = f"issue:{event.get('title', '')}"
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(event)
    return [pipeline_stage(grouped[key]) for key in order]


def filesystem_burst_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    first = min(events, key=event_epoch)
    latest = max(events, key=event_epoch)
    count = sum(int(event.get("repeat_count", 1)) for event in events)
    paths: Counter[str] = Counter()
    removals = 0
    for event in events:
        repeats = int(event.get("repeat_count", 1))
        paths[str(event.get("detail", "unknown"))] += repeats
        if "removed" in str(event.get("title", "")).casefold():
            removals += repeats
    return {
        "first_timestamp": first.get("first_timestamp", first.get("timestamp")),
        "timestamp": latest.get("timestamp"),
        "count": count,
        "changes": count - removals,
        "removals": removals,
        "paths": paths.most_common(),
    }


def activity_unit_local_date(
    unit: dict[str, Any], local_timezone: tzinfo | None = None
) -> date | None:
    epochs = [
        event.get("epoch_ms")
        for event in unit["events"]
        if isinstance(event.get("epoch_ms"), int)
    ]
    return local_date_for_epoch(max(epochs), local_timezone) if epochs else None


def is_github_confirmation(event: dict[str, Any]) -> bool:
    """Report whether an event is Side Dog first noticing a pull request.

    Watching several roots means a sweep of these on start-up. They are
    bookkeeping, not activity, so they collapse into a single line.
    """
    return event.get("kind") == "github" and str(event.get("title", "")).endswith(
        "confirmed"
    )


def github_burst_numbers(events: list[dict[str, Any]]) -> list[int]:
    numbers: list[int] = []
    for event in events:
        status = event.get("github")
        number = status.get("number") if isinstance(status, dict) else None
        if isinstance(number, int) and number not in numbers:
            numbers.append(number)
    return sorted(numbers)


def build_activity_units(
    events: list[dict[str, Any]],
    expanded_history: bool,
    local_timezone: tzinfo | None = None,
) -> list[dict[str, Any]]:
    latest_history_indexes: dict[tuple[str, str], int] = {}
    for index, event in enumerate(events):
        if is_native_history_event(event):
            latest_history_indexes[
                (event_root(event), str(event.get("session_id", "")))
            ] = index
    latest_github_state: dict[tuple[str, int], str] = {}
    semantic_events: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if is_native_history_event(event) and latest_history_indexes.get(
            (event_root(event), str(event.get("session_id", "")))
        ) != index:
            continue
        status = event.get("github")
        if event.get("kind") == "github" and isinstance(status, dict):
            number = status.get("number")
            if isinstance(number, int):
                state_key = (event_root(event), number)
                fingerprint = github_fingerprint(status)
                if latest_github_state.get(state_key) == fingerprint:
                    continue
                latest_github_state[state_key] = fingerprint
        semantic_events.append(event)
    events = collapse_repeated_filesystem_events(semantic_events, local_timezone)
    groups: dict[tuple[str, str], list[int]] = {}
    for index, event in enumerate(events):
        if (
            event.get("kind") == "github"
            or event.get("agent") == "filesystem"
            or is_native_history_event(event)
        ):
            continue
        group = event.get("turn_id") or event.get("group_id")
        if isinstance(group, str) and group:
            groups.setdefault((event_root(event), group), []).append(index)
    pipeline_groups = {
        group: indexes
        for group, indexes in groups.items()
        if len(indexes) > 1
        and any(events[index].get("kind") in MILESTONE_KINDS for index in indexes)
    }
    grouped_indexes = {
        index for indexes in pipeline_groups.values() for index in indexes
    }
    units: list[dict[str, Any]] = []
    for group, indexes in pipeline_groups.items():
        group_events = [events[index] for index in indexes]
        units.append(
            {
                "type": "pipeline",
                "events": group_events,
                "epoch": max(event_epoch(event) for event in group_events),
                "index": max(
                    int(events[index].get("_append_ordinal", index))
                    for index in indexes
                ),
                "group": group,
                "root": group[0],
                "title": activity_title(group_events),
                "stages": pipeline_stages(group_events),
            }
        )
    for index, event in enumerate(events):
        if index in grouped_indexes:
            continue
        units.append(
            {
                "type": "event",
                "events": [event],
                "epoch": event_epoch(event),
                "index": int(event.get("_append_ordinal", index)),
                "root": event_root(event),
            }
        )
    units.sort(key=lambda unit: int(unit["index"]))
    if expanded_history:
        return units
    merged: list[dict[str, Any]] = []
    for unit in units:
        event = unit["events"][0]
        if unit["type"] == "event" and is_github_confirmation(event):
            if (
                merged
                and merged[-1]["type"] == "github_burst"
                and int(unit["epoch"]) - int(merged[-1]["epoch"])
                <= GITHUB_CONFIRMATION_GAP_MS
                and activity_unit_local_date(unit, local_timezone)
                == activity_unit_local_date(merged[-1], local_timezone)
            ):
                merged[-1]["events"].append(event)
                merged[-1]["epoch"] = unit["epoch"]
                continue
            merged.append(
                {
                    "type": "github_burst",
                    "events": [event],
                    "epoch": unit["epoch"],
                    "index": unit["index"],
                    "root": unit["root"],
                }
            )
            continue
        if unit["type"] != "event" or not is_passive_file_event(event):
            merged.append(unit)
            continue
        if (
            merged
            and merged[-1]["type"] == "filesystem_burst"
            and unit["root"] == merged[-1]["root"]
            and int(unit["epoch"]) - int(merged[-1]["epoch"])
            <= FILESYSTEM_BURST_GAP_MS
            and activity_unit_local_date(unit, local_timezone)
            == activity_unit_local_date(merged[-1], local_timezone)
        ):
            merged[-1]["events"].append(event)
            merged[-1]["epoch"] = unit["epoch"]
            merged[-1]["summary"] = filesystem_burst_summary(merged[-1]["events"])
            continue
        merged.append(
            {
                "type": "filesystem_burst",
                "events": [event],
                "epoch": unit["epoch"],
                "index": unit["index"],
                "root": unit["root"],
                "summary": filesystem_burst_summary([event]),
            }
        )
    return merged
