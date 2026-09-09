"""Shared, event-time contribution accounting. No roster or raw transcript input."""

from __future__ import annotations

from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from side_dog.integrations import CODING_AGENT_PROVIDERS, normalize_provider
from side_dog.model import agent_label, event_root

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
