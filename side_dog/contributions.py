"""Shared, event-time contribution accounting. No roster or raw transcript input."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from side_dog.integrations import CODING_AGENT_PROVIDERS, normalize_provider
from side_dog.model import actor_label, event_root

WINDOW_MS = 24 * 60 * 60 * 1000


def work_reference(event: Mapping[str, Any]) -> tuple[str, str]:
    """Use structured GitHub metadata only; never mine titles or branch names."""
    github = event.get("github") or {}
    url = str(github.get("url") or event.get("url") or "")
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) == 4 and parts[2] in {"pull", "issues"} and parts[3].isdigit():
        if parsed.scheme in {"https", "http"} and parsed.hostname and int(parts[3]) > 0:
            repository = f"{parsed.netloc}/{'/'.join(parts[:2])}"
            kind = "PR" if parts[2] == "pull" else "issue"
            return repository, f"{kind} #{int(parts[3])}"
    number = github.get("number")
    if isinstance(number, int) and not isinstance(number, bool) and number > 0:
        kind = "issue" if event.get("kind") == "issue" else "PR"
        return event_root(dict(event)), f"{kind} #{number}"
    return event_root(dict(event)), "unlinked work"


def is_observation(event: Mapping[str, Any]) -> bool:
    return (
        event.get("kind") == "github"
        or normalize_provider(event.get("agent")) not in CODING_AGENT_PROVIDERS
    )


def event_identity(event: Mapping[str, Any]) -> tuple[str, str, str]:
    if is_observation(event):
        return "observation", "unknown", "unknown"
    return (
        normalize_provider(event.get("agent")),
        str(event.get("session_id") or "unknown"),
        str(event.get("model") or "unknown"),
    )


def _context_key(event: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    session = str(event.get("session_id") or "")
    group = str(event.get("turn_id") or event.get("group_id") or "")
    if not session or session == "unknown" or not group:
        return None
    return event_root(dict(event)), str(event.get("agent")), session, group


def attributed_events(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Resolve only unambiguous references inside a recorded session turn.

    Only an agent action with a structured reference can name other work in
    its turn. Polling may carry a stale triggering identity, so observations
    never supply attribution evidence. Ambiguous turns retain unlinked actions.
    """
    records = [dict(event) for event in events]
    # An exact recorded commit ID can be matched to an observed PR head in
    # this folder. The author/model still comes solely from the commit event.
    # Shared heads (several PRs) remain ambiguous; timestamps/branches are not
    # substitutes for object identity.
    heads: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for event in records:
        github = event.get("github") or {}
        oid = github.get("head_oid")
        repo, work = work_reference(event)
        if event.get("kind") == "github" and oid and work.startswith("PR #"):
            heads.setdefault((event_root(event), str(oid)), set()).add(
                (repo, work, str(github.get("url") or ""))
            )
    for event in records:
        if (
            event.get("kind") != "commit"
            or is_observation(event)
            or event.get("status") != "success"
        ):
            continue
        if work_reference(event)[1] != "unlinked work":
            continue
        matches = heads.get((event_root(event), str(event.get("git_oid") or "")), set())
        if len(matches) == 1:
            _repo, work, url = next(iter(matches))
            event["github"] = {"number": int(work[4:]), **({"url": url} if url else {})}
    references: dict[tuple[str, str, str, str], set[tuple[str, str, str]]] = {}
    for event in records:
        key = _context_key(event)
        repo, work = work_reference(event)
        if key and work != "unlinked work" and not is_observation(event):
            url = str((event.get("github") or {}).get("url") or event.get("url") or "")
            references.setdefault(key, set()).add((repo, work, url))
    for event in records:
        repo, work = work_reference(event)
        url = str((event.get("github") or {}).get("url") or event.get("url") or "")
        candidates = references.get(_context_key(event), set())
        if work == "unlinked work" and len(candidates) == 1:
            repo, work, url = next(iter(candidates))
        event["_contribution_repository"] = repo
        event["_contribution_work"] = work
        event["_contribution_url"] = url
    return records


@dataclass(frozen=True)
class Contribution:
    repository: str
    work: str
    agent: str
    session: str
    model: str
    events: tuple[dict[str, Any], ...]

    @property
    def key(self) -> tuple[str, ...]:
        return self.repository, self.work, self.agent, self.session, self.model

    @property
    def latest(self) -> dict[str, Any]:
        return self.events[-1]

    @property
    def url(self) -> str:
        return next(
            (
                str(e.get("_contribution_url"))
                for e in reversed(self.events)
                if e.get("_contribution_url")
            ),
            "",
        )

    @property
    def summary(self) -> str:
        if self.agent == "observation":
            github = self.latest.get("github") or {}
            return "observation · " + " · ".join(
                str(github[k])
                for k in ("state", "ci", "review", "coverage")
                if github.get(k)
            )
        counts = Counter()
        for event in self.events:
            kind, status = event.get("kind"), event.get("status")
            if kind in {"file", "config"}:
                name = "edits"
            elif kind == "commit":
                name = "commits"
            elif kind == "test":
                name = f"tests {status}"
            elif kind in {"pr", "merge", "push", "issue"}:
                name = "PR actions" if kind in {"pr", "merge"} else str(kind)
            else:
                continue
            if kind != "test" and status != "success":
                name += f" {status}"
            counts[name] += 1
        return (
            " · ".join(f"{count} {name}" for name, count in counts.items())
            or "session activity"
        )


def contributions(
    events: Iterable[Mapping[str, Any]], now_ms: int, window_ms: int = WINDOW_MS
) -> list[Contribution]:
    scoped = [
        e for e in events if now_ms - window_ms <= int(e.get("epoch_ms") or 0) <= now_ms
    ]
    # A command start and completion are one operation. Distinct operations
    # (including repeated tests) remain distinct; polling adds no effort.
    operations: dict[tuple[Any, ...], dict[str, Any]] = {}
    for event in sorted(
        attributed_events(scoped), key=lambda e: int(e.get("epoch_ms") or 0)
    ):
        identity = event_identity(event)
        operation = event.get("operation_id") or event.get("source_event_id")
        if identity[0] == "observation":
            operation = (
                event.get("kind"),
                event.get("github_fingerprint") or event.get("git_oid") or operation,
            )
        if not operation:
            operation = (
                event.get("epoch_ms"),
                event.get("kind"),
                event.get("title"),
                event.get("detail"),
            )
        key = (
            event_root(event),
            event["_contribution_repository"],
            event["_contribution_work"],
            *identity,
            event.get("kind"),
            str(operation),
        )
        operations[key] = event
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for event in operations.values():
        key = (
            event["_contribution_repository"],
            event["_contribution_work"],
            *event_identity(event),
        )
        grouped.setdefault(key, []).append(event)
    rows = [
        Contribution(
            *key, tuple(sorted(values, key=lambda e: int(e.get("epoch_ms") or 0)))
        )
        for key, values in grouped.items()
    ]
    return sorted(
        rows, key=lambda row: int(row.latest.get("epoch_ms") or 0), reverse=True
    )


def contribution_actor(row: Contribution) -> str:
    return actor_label(row.latest, {})


def render_contributions(
    rows: list[Contribution],
    width: int,
    height: int,
    now_ms: int,
    *,
    selected: int = 0,
    scope: str = "all saved folders",
    partial: bool = False,
) -> str:
    # Local import keeps this module's accounting usable without the CLI.
    from side_dog.board import crop

    width, height = max(20, width), max(4, height)
    lines = [
        crop("SIDE DOG · contributions · last 24h", width),
        crop(("PARTIAL · " if partial else "") + "last 24h · " + scope, width),
    ]
    if rows:
        selected %= len(rows)
        # Separate lines preserve work, identity and counts at narrow widths.
        room = max(1, (height - 3) // 5)
        start = (selected // room) * room
        for index in range(start, min(len(rows), start + room)):
            row = rows[index]
            mark = "> " if index == selected else "  "
            age = max(0, (now_ms - int(row.latest.get("epoch_ms") or 0)) // 60000)
            lines.extend(
                [
                    crop(mark + f"{row.work} · {row.repository}", width),
                    crop("  " + contribution_actor(row), width),
                    crop("  session " + row.session, width),
                    crop("  " + row.summary, width),
                    crop(
                        f"  last {row.latest.get('status', 'unknown')} · {age}m ago",
                        width,
                    ),
                ]
            )
    else:
        lines.append(crop("No recorded activity in last 24h", width))
    lines = lines[: height - 1]
    lines.append(
        crop("a roster · j/k select · o open · w watch · ? help · q quit", width)
    )
    return "\n".join(lines)
