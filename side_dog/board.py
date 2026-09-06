"""The board: one row per live coding-agent session on the machine.

``side-dog watch`` answers "what is happening in this folder" with a timeline.
The board answers a different question that comes up several times an hour
when Claude Code and Codex run in Herdr panes, Codex Desktop, and Claude
Desktop at once: which agent, on which surface, is on which branch and pull
request right now, and is it still moving?

Everything here is pure. ``cli.py`` gathers identities, Git state, GitHub
readbacks, and event tails from the collectors it already runs, hands them in
as :class:`BoardSource` values, and gets rows and a rendered frame back. No
function in this module touches the filesystem, a subprocess, or the clock,
so every table in the design document is a unit test with a literal fixture.
See ``docs/design/board.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Iterable, Mapping, NamedTuple, Sequence

from side_dog.integrations import (
    CODING_AGENT_PROVIDERS,
    AgentStatus,
    normalize_provider,
)
from side_dog.model import agent_label, agent_session_key, github_ci_phase

# A finished session stays on the board this long after its last activity, so
# "done" reads as news rather than history. Matches the identity windows the
# collectors already use.
DONE_ROW_SECONDS = 900

# Rows sort by what needs eyes first.
STATUS_ORDER = {
    AgentStatus.WORKING: 0,
    AgentStatus.BLOCKED: 1,
    AgentStatus.IDLE: 2,
    AgentStatus.UNKNOWN: 3,
    AgentStatus.DONE: 4,
}

# Filled for a session doing something, hollow for one that is not, and a
# dotted ring for one waiting on a person. The word after the glyph carries
# the meaning without color.
STATUS_GLYPHS = {
    AgentStatus.WORKING: "●",
    AgentStatus.BLOCKED: "◌",
    AgentStatus.IDLE: "○",
    AgentStatus.DONE: "○",
    AgentStatus.UNKNOWN: "?",
}

GROUPS = ("none", "surface", "repo")

UNKNOWN_SURFACE = "unknown"

# A successful ``gh issue view``/``develop`` confirms a link for this long.
ISSUE_COMMAND_WINDOW_MS = 3_600_000

# Where an issue number hides in a branch name: ``139-fix``, ``issue-139``,
# ``issue/139``, ``fix-139``, ``fix/139``, and ``#139`` inside a segment.
_BRANCH_ISSUE_PATTERNS = (
    re.compile(r"^([1-9][0-9]*)-"),
    re.compile(r"(?:^|[/_-])issues?[-/_]?([1-9][0-9]*)(?=$|[/_-])", re.IGNORECASE),
    re.compile(r"[-/]([1-9][0-9]*)$"),
    re.compile(r"#([1-9][0-9]*)"),
)
# A non-digit boundary after the number: ordinary title punctuation such as
# ``(...).`` or a trailing comma must not hide a mention.
_TITLE_ISSUE_PATTERN = re.compile(r"#([1-9][0-9]*)(?!\d)|/issues/([1-9][0-9]*)(?!\d)")
_WEB_URL_PATTERN = re.compile(
    r"https?://([^/?#]+)/([^/?#]+)/([^/?#]+)/(?:pull|issues)/[1-9][0-9]*(?:[/?#]|$)",
    re.IGNORECASE,
)
_REMOTE_PATTERN = re.compile(
    r"(?:https?://(?:[^@/]+@)?|ssh://(?:[^@/]+@)?|git@|[A-Za-z0-9_.-]+@)"
    r"([A-Za-z0-9_.-]+)(?::[0-9]+)?[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?",
    re.IGNORECASE,
)


class IssueCommand(NamedTuple):
    """One persisted ``gh issue view``/``develop`` success, as the reader saw it.

    ``url`` is the one the normalizer rebuilt from validated parts, or "" when
    the command named no repository and the folder's origin should stand in.
    """

    epoch_ms: int
    number: int
    url: str


class LinkedIssue(NamedTuple):
    """``(repository, number, confirmed)``; repository is ``host/owner/name``."""

    repository: str
    number: int
    confirmed: bool


def repository_from_web_url(url: str) -> str:
    """``host/owner/name`` from a pull request or issue URL, or ""."""
    match = _WEB_URL_PATTERN.match(str(url or "").strip())
    if match is None:
        return ""
    host = match.group(1).casefold()
    if host == "www.github.com":
        host = "github.com"
    return f"{host}/{match.group(2)}/{match.group(3)}"


def repository_from_remote(url: str) -> str:
    """``host/owner/name`` from a Git remote URL in any common spelling."""
    match = _REMOTE_PATTERN.fullmatch(str(url or "").strip())
    if match is None:
        return ""
    return f"{match.group(1).casefold()}/{match.group(2)}/{match.group(3)}"

# The same terminal-theme escapes ``cli.py`` uses, kept here so this module
# does not import the CLI. Themes pick the final colors, which is what keeps
# the accents readable on light and dark backgrounds alike.
ANSI = {
    "reset": "\x1b[0m",
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "blue": "\x1b[34m",
    "green": "\x1b[32m",
    "magenta": "\x1b[35m",
    "red": "\x1b[31m",
    "yellow": "\x1b[33m",
}

STATUS_COLORS = {
    AgentStatus.WORKING: ANSI["yellow"],
    AgentStatus.BLOCKED: ANSI["red"],
    AgentStatus.IDLE: ANSI["dim"],
    AgentStatus.DONE: ANSI["green"],
    AgentStatus.UNKNOWN: ANSI["dim"],
}


@dataclass(frozen=True, slots=True)
class BoardSource:
    """One watched folder as the board sees it.

    ``identities`` is the mapping ``load_agent_identities()`` returns for the
    folder. ``branches`` names the current branch of every worktree an agent
    in this folder is working in, keyed by that worktree's path, because a
    Codex Desktop worktree is not the folder being watched and its branch is
    not the folder's. ``activity`` is the newest event time per
    provider-qualified session key (``codex:<id>``), in epoch milliseconds,
    read from the folder's own history tail. ``github_repository`` is the
    folder's ``host/owner/name``, from its PR's URL or its origin remote, and
    ``issue_commands`` holds the successful ``gh issue view``/``develop``
    events from the same tail, keyed the same way as ``activity``.
    """

    root: str
    repository: str = ""
    # What tells one repository from another: the Git common directory.
    # ``repository`` is only its display name, and two unrelated checkouts
    # can be called ``api``. Empty falls back to the name.
    repository_key: str = ""
    branch: str = ""
    github: Mapping[str, Any] | None = None
    identities: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    branches: Mapping[str, str] = field(default_factory=dict)
    activity: Mapping[str, int] = field(default_factory=dict)
    github_repository: str = ""
    issue_commands: Mapping[str, Sequence[IssueCommand]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BoardRow:
    """One live session, ready to render."""

    key: str
    agent: str
    surface: str
    repository: str
    branch: str
    root: str
    working_root: str
    status: AgentStatus
    age_seconds: float | None
    repository_key: str = ""
    label: str = ""
    model: str = ""
    session_id: str = ""
    pane_id: str = ""
    github: Mapping[str, Any] | None = None
    github_repository: str = ""
    issues: tuple[LinkedIssue, ...] = ()

    @property
    def agent_name(self) -> str:
        return agent_label(self.agent)

    @property
    def repository_id(self) -> str:
        """The value to count and group by; the name when no key is known."""
        return self.repository_key or self.repository


def branch_issue_numbers(branch: str) -> tuple[int, ...]:
    """Issue numbers a branch name suggests, in the order they appear."""
    found: list[tuple[int, int]] = []
    for pattern in _BRANCH_ISSUE_PATTERNS:
        for match in pattern.finditer(branch or ""):
            number = int(match.group(1))
            if all(number != seen for _, seen in found):
                found.append((match.start(1), number))
    return tuple(number for _, number in sorted(found))


def title_issue_numbers(title: str) -> tuple[int, ...]:
    """``#N`` and ``/issues/N`` mentions in a pull request title."""
    numbers: list[int] = []
    for match in _TITLE_ISSUE_PATTERN.finditer(title or ""):
        number = int(match.group(1) or match.group(2))
        if number not in numbers:
            numbers.append(number)
    return tuple(numbers)


def linked_issues(
    *,
    repository: str,
    github: Mapping[str, Any] | None,
    commands: Sequence[IssueCommand],
    branch: str,
    now_ms: int,
) -> tuple[LinkedIssue, ...]:
    """Every issue a session is on, confirmed sources first, then by number.

    Confirmed: the pull request's closing issues, then a successful single
    ``gh issue view``/``develop`` within the last hour. Inferred: a number in
    the branch name, then ``#N`` or ``/issues/N`` in the PR title. The same
    ``(repository, number)`` appears once, and confirmation wins over
    inference. ``repository`` is the row's own ``host/owner/name`` and stands
    in wherever a source names none.
    """
    found: dict[tuple[str, int], bool] = {}

    def add(issue_repository: str, number: int, confirmed: bool) -> None:
        key = (issue_repository, number)
        found[key] = found.get(key, False) or confirmed

    pr_repository = repository_from_web_url(str((github or {}).get("url") or "")) or repository
    closing = (github or {}).get("closing_issues")
    if isinstance(closing, (list, tuple)):
        for number in closing:
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                add(pr_repository, number, True)
    for command in commands:
        if now_ms - command.epoch_ms > ISSUE_COMMAND_WINDOW_MS or command.epoch_ms > now_ms:
            continue
        add(repository_from_web_url(command.url) or repository, command.number, True)
    for number in branch_issue_numbers(branch):
        add(repository, number, False)
    if github:
        for number in title_issue_numbers(str(github.get("title") or "")):
            add(pr_repository, number, False)
    return tuple(
        LinkedIssue(issue_repository, number, confirmed)
        for (issue_repository, number), confirmed in sorted(
            found.items(), key=lambda item: (not item[1], item[0][1], item[0][0])
        )
    )


def row_key(identity: Mapping[str, str]) -> str:
    """The identity a row keeps across polls.

    A session id makes the ``SessionKey``. Without one, a Herdr pane keeps its
    own ``pane:<id>`` key: ``SessionKey`` would fold every such pane into
    ``<provider>:unknown`` and hide all but one live pane. Anything else is
    told apart by where it is and what it is called.
    """
    agent = normalize_provider(identity.get("agent"))
    session_id = str(identity.get("session_id") or "").strip()
    if session_id and session_id != "unknown":
        return agent_session_key(agent, session_id)
    pane_id = str(identity.get("pane_id") or "").strip()
    if pane_id:
        return f"pane:{pane_id}"
    return ":".join(
        (agent, str(identity.get("working_root") or ""), str(identity.get("label") or ""))
    )


def surface_label(identity: Mapping[str, str]) -> str:
    """Where to find the window, as well as this phase can say.

    Herdr wins when it knows the pane: it alone can name the tab and window.
    Otherwise the loader's own ``surface`` (the Claude registry entrypoint or
    the Codex originator) is used, and when nobody knows the answer is the
    literal ``unknown`` rather than a guess.
    """
    pane_id = str(identity.get("pane_id") or "").strip()
    if pane_id:
        parts = ["Herdr"]
        workspace_id = str(identity.get("workspace_id") or "").strip()
        if workspace_id:
            parts.append(workspace_id)
        parts.append(f"pane {pane_id}")
        return " · ".join(parts)
    surface = str(identity.get("surface") or "").strip()
    return surface or UNKNOWN_SURFACE


def _path_within(child: str, parent: str) -> bool:
    if not child or not parent:
        return False
    try:
        child_path = PurePath(child)
        parent_path = PurePath(parent)
    except (TypeError, ValueError):
        return False
    return child_path == parent_path or child_path.is_relative_to(parent_path)


def _seat(identity: Mapping[str, str], sources: Sequence[BoardSource]) -> BoardSource:
    """The folder a session belongs to: the deepest one containing its cwd."""
    working_root = str(identity.get("working_root") or "")
    exact = [source for source in sources if _path_within(working_root, source.root)]
    if exact:
        return max(exact, key=lambda source: len(PurePath(source.root).parts))
    return sources[0]


def rows_from_sources(
    sources: Iterable[BoardSource], now_ms: int
) -> list[BoardRow]:
    """Flatten every folder's identities into one row per session.

    Herdr reports an agent for every worktree of the repository it sits in,
    so the same session arrives under several folders. The row is seated in
    the folder that actually contains its working directory, and a finished
    session older than :data:`DONE_ROW_SECONDS` is left off.
    """
    appearances: dict[str, tuple[Mapping[str, str], list[BoardSource]]] = {}
    for source in sources:
        for identity in source.identities.values():
            if normalize_provider(identity.get("agent")) not in CODING_AGENT_PROVIDERS:
                continue
            key = row_key(identity)
            current = appearances.get(key)
            if current is None:
                appearances[key] = (identity, [source])
            elif source not in current[1]:
                current[1].append(source)
    rows: list[BoardRow] = []
    for key, (identity, seen_in) in appearances.items():
        source = _seat(identity, seen_in)
        working_root = str(identity.get("working_root") or source.root)
        session_id = str(identity.get("session_id") or "").strip()
        if session_id == "unknown":
            session_id = ""
        status = AgentStatus.from_wire(identity.get("status"))
        # The session's events may have been written under another folder
        # that reported it, such as the repository root a capped watch used
        # before the board found its worktree, so every appearance is asked.
        epochs = [
            value
            for value in (seen.activity.get(key) for seen in seen_in)
            if isinstance(value, int) and value
        ] if session_id else []
        epoch = max(epochs) if epochs else None
        age = (now_ms - epoch) / 1000 if epoch is not None else None
        if status is AgentStatus.DONE and age is not None and age > DONE_ROW_SECONDS:
            continue
        in_root = _path_within(working_root, source.root) and (
            PurePath(working_root) == PurePath(source.root)
            or working_root not in source.branches
        )
        branch = source.branches.get(working_root) or (source.branch if in_root else "")
        github = source.github
        if github is not None and not in_root:
            # The readback belongs to the watched folder's branch. Another
            # worktree of the repository only shares it when on that branch.
            if str(github.get("branch") or "") != branch or not branch:
                github = None
        # Another worktree of the folder shares its remote: the repository is
        # per clone, not per branch, so the row keeps the folder's.
        github_repository = source.github_repository
        # Like the age, the session's issue commands may sit in any folder's
        # history that reported it; the row key is the provider-qualified one.
        commands = tuple(
            command for seen in seen_in for command in seen.issue_commands.get(key, ())
        ) if session_id else ()
        rows.append(
            BoardRow(
                key=key,
                agent=normalize_provider(identity.get("agent")),
                surface=surface_label(identity),
                repository=source.repository,
                repository_key=source.repository_key,
                branch=branch,
                root=source.root,
                working_root=working_root,
                status=status,
                age_seconds=age,
                label=str(identity.get("label") or ""),
                model=str(identity.get("model") or ""),
                session_id=session_id,
                pane_id=str(identity.get("pane_id") or ""),
                github=github,
                github_repository=github_repository,
                issues=linked_issues(
                    repository=github_repository,
                    github=github,
                    commands=commands,
                    branch=branch,
                    now_ms=now_ms,
                ),
            )
        )
    return rows


def event_belongs_to_row(event: Mapping[str, Any], row: BoardRow) -> bool:
    """Exactly this session's event, never a look-alike's.

    ``matches_session_filter()`` in the CLI is a substring match over session
    id, pane id, and label, so pane ``w1:p1`` would also claim ``w1:p10``.
    The detail pane needs the row itself: the provider-qualified session key
    when the row has a session, otherwise the Herdr pane the event was
    recorded under.
    """
    if row.session_id:
        session_id = str(event.get("session_id") or "").strip()
        if not session_id:
            return False
        return agent_session_key(event.get("agent"), session_id) == row.key
    if row.pane_id:
        return str(event.get("herdr_pane_id") or "").strip() == row.pane_id
    return False


MAX_CONFLICTS = 3
CONFLICT_OVERFLOW_PREFIX = "… "

# What ``load_git_state()`` reports for a checkout with no branch. Two such
# worktrees share the word, not a branch.
DETACHED_BRANCH = "detached"


def _live(row: BoardRow) -> bool:
    return row.status is not AgentStatus.DONE


def _pair_label(first: BoardRow, second: BoardRow) -> str:
    return f"{first.surface} and {second.surface}"


CONFLICT_WORKTREE = "worktree"
CONFLICT_BRANCH = "branch"
CONFLICT_ISSUE = "issue"


class Conflict(NamedTuple):
    """One pair of live sessions that can undo each other.

    ``kind`` and the sorted pair of row keys identify the conflict. ``text``
    is the strip line, which names surfaces in display order; that order
    follows status, so the line can change while the conflict has not.
    ``repository``, ``branch``, and ``issue`` carry what the line is about
    for renderers that want to phrase it differently.
    """

    kind: str
    keys: tuple[str, str]
    repository: str
    branch: str
    issue: int | None
    text: str

    @property
    def identity(self) -> str:
        return f"{self.kind}:{self.keys[0]}+{self.keys[1]}"


def conflicts(rows: Sequence[BoardRow]) -> list[str]:
    """The strip: the ways two live sessions can silently undo each other."""
    return conflict_lines(detect_conflicts(rows))


def shown_conflicts(details: Sequence[Conflict]) -> list[Conflict]:
    """The conflicts the strip names, once the overflow line takes a slot."""
    if len(details) > MAX_CONFLICTS:
        return list(details[: MAX_CONFLICTS - 1])
    return list(details)


def conflict_lines(details: Sequence[Conflict]) -> list[str]:
    """Strip lines for the conflicts found, at most three."""
    found = [conflict.text for conflict in shown_conflicts(details)]
    if len(details) > MAX_CONFLICTS:
        hidden = len(details) - (MAX_CONFLICTS - 1)
        found.append(f"{CONFLICT_OVERFLOW_PREFIX}{hidden} more conflicts")
    return found


def detect_conflicts(rows: Sequence[BoardRow]) -> list[Conflict]:
    """Every way two live sessions can silently undo each other, uncapped.

    Same worktree: two agents editing one checkout. Same branch of one
    repository in different worktrees: one push discards the other's
    commits. Same issue: two agents solving one problem. Each line names
    both surfaces so the person can decide which window to stop. Each pair
    is reported once, for the first kind that applies.
    """
    live = [row for row in sort_rows(rows) if _live(row)]
    found: list[Conflict] = []
    seen_pairs: set[tuple[str, str]] = set()

    def note(
        kind: str,
        first: BoardRow,
        second: BoardRow,
        text: str,
        *,
        repository: str = "",
        branch: str = "",
        issue: int | None = None,
    ) -> None:
        pair = tuple(sorted((first.key, second.key)))
        if pair in seen_pairs:
            return
        seen_pairs.add(pair)  # type: ignore[arg-type]
        found.append(Conflict(kind, pair, repository, branch, issue, text))  # type: ignore[arg-type]

    for index, first in enumerate(live):
        for second in live[index + 1 :]:
            if first.working_root and first.working_root == second.working_root:
                folder = PurePath(first.working_root).name or first.working_root
                note(
                    CONFLICT_WORKTREE,
                    first,
                    second,
                    f"two sessions in {folder}: {_pair_label(first, second)}",
                    repository=first.repository,
                    branch=first.branch if first.branch == second.branch else "",
                )
    for index, first in enumerate(live):
        for second in live[index + 1 :]:
            if (
                first.branch
                and first.branch != DETACHED_BRANCH
                and first.repository_id
                and first.repository_id == second.repository_id
                and first.branch == second.branch
                and first.working_root != second.working_root
            ):
                where = f"{first.repository} {first.branch}".strip()
                note(
                    CONFLICT_BRANCH,
                    first,
                    second,
                    f"two sessions on {where}: {_pair_label(first, second)}",
                    repository=first.repository,
                    branch=first.branch,
                )
    for index, first in enumerate(live):
        if not first.issues:
            continue
        for second in live[index + 1 :]:
            # An issue without a repository (a bare number from a branch name
            # in a checkout with no recognised GitHub origin) says which
            # issue only inside one repository: two unrelated checkouts on
            # `fix/12` are not on the same issue.
            same_repository = bool(first.repository_id) and (
                first.repository_id == second.repository_id
            )

            def issue_keys(row: BoardRow) -> set[tuple[str, int]]:
                return {
                    (issue.repository, issue.number)
                    for issue in row.issues
                    if issue.repository or same_repository
                }

            shared = issue_keys(first) & issue_keys(second)
            if not shared:
                continue
            repository, number = sorted(shared, key=lambda item: (item[1], item[0]))[0]
            name = repository.rsplit("/", 1)[-1] if repository else ""
            first_where = f" ({first.branch})" if first.branch else ""
            second_where = f" ({second.branch})" if second.branch else ""
            note(
                CONFLICT_ISSUE,
                first,
                second,
                f"two sessions on {name}#{number}: {first.surface}{first_where}"
                f" and {second.surface}{second_where}",
                repository=name or first.repository,
                issue=number,
            )
    return found


# Notifications: what changed between two frames that a person who is not
# looking at the table would want to hear about. Everything a message says is
# already on the board row - agent, surface, repository, branch, pull request
# and issue numbers - never a path and never event text.
TRANSITION_CI_PASSED = "ci-passed"
TRANSITION_APPROVED = "approved"
TRANSITION_BLOCKED = "blocked"
TRANSITION_CONFLICT = "conflict"
PR_TRANSITIONS = frozenset({TRANSITION_CI_PASSED, TRANSITION_APPROVED})
RESTING_STATUSES = frozenset({AgentStatus.IDLE, AgentStatus.DONE})


class BoardNotification(NamedTuple):
    """One desktop message about the board.

    ``key`` is the condition's identity - ``(row key, transition)`` for a
    row, ``(conflict identity, "conflict")`` for a conflict - so callers can
    tell two frames' messages about the same thing apart from two different
    things.
    """

    key: tuple[str, str]
    title: str
    body: str


def _row_where(row: BoardRow) -> str:
    parts = [row.agent_name, row.surface]
    where = f"{row.repository} {row.branch}".strip()
    if where:
        parts.append(where)
    number = (row.github or {}).get("number")
    if isinstance(number, int):
        parts.append(f"PR #{number}")
    if row.issues:
        own = row.github_repository
        parts.append(", ".join(issue_label(issue, own) for issue in row.issues))
    return " · ".join(part for part in parts if part)


def _pr_number(row: BoardRow) -> int | None:
    """The pull request a row shows, or None while the board has not read one.

    A failed readback leaves a placeholder with no number so the cell can say
    ``PR ?``; that is not a pull request the board has seen.
    """
    number = (row.github or {}).get("number")
    return number if isinstance(number, int) else None


def _pr_conditions(row: BoardRow) -> list[str]:
    """Which pull-request conditions a resting row satisfies right now."""
    github = row.github
    if not github or row.status not in RESTING_STATUSES:
        return []
    if _pr_number(row) is None:
        return []
    if str(github.get("state") or "").upper() in {"MERGED", "CLOSED"}:
        return []
    kinds: list[str] = []
    if github_ci_phase(dict(github)) == "passed":
        kinds.append(TRANSITION_CI_PASSED)
    if str(github.get("review") or "").upper() == "APPROVED":
        kinds.append(TRANSITION_APPROVED)
    return kinds


def _blocked_alone(row: BoardRow, rows: Sequence[BoardRow]) -> bool:
    """Blocked, with no other session working in the same repository.

    While another agent is still moving in that repository the person is
    probably about to look anyway; when nothing else is, the blocked one is
    the only thing keeping the repository from making progress.
    """
    if row.status is not AgentStatus.BLOCKED:
        return False
    return not any(
        other.key != row.key
        and other.status is AgentStatus.WORKING
        and other.repository_id
        and other.repository_id == row.repository_id
        for other in rows
    )


def board_conditions(
    rows: Sequence[BoardRow], conflicts: Sequence[Conflict]
) -> dict[tuple[str, str], BoardNotification]:
    """Every notifiable condition one frame satisfies, keyed by identity.

    ``conflicts`` are the ones the strip names, from :func:`shown_conflicts`.
    A conflict is keyed by kind and pair, not by its line: when the two
    sessions trade working and idle the line names them the other way round
    while the conflict never lapsed.
    """
    found: dict[tuple[str, str], BoardNotification] = {}
    for row in rows:
        where = _row_where(row)
        for kind in _pr_conditions(row):
            number = (row.github or {}).get("number")
            what = "checks passed" if kind == TRANSITION_CI_PASSED else "approved"
            resting = "finished" if row.status is AgentStatus.DONE else "idle"
            key = (row.key, kind)
            found[key] = BoardNotification(key, f"PR #{number} {what}", f"{where} is {resting}")
        if _blocked_alone(row, rows):
            key = (row.key, TRANSITION_BLOCKED)
            body = where
            if row.repository:
                body = f"{where}; nothing else is working in {row.repository}"
            found[key] = BoardNotification(key, f"{row.agent_name} is blocked", body)
    for conflict in conflicts:
        key = (conflict.identity, TRANSITION_CONFLICT)
        found[key] = BoardNotification(key, "Board conflict", conflict.text)
    return found


def board_transitions(
    previous: Sequence[BoardRow],
    current: Sequence[BoardRow],
    previous_conflicts: Sequence[Conflict],
    current_conflicts: Sequence[Conflict],
) -> list[BoardNotification]:
    """The conditions ``current`` meets that ``previous`` did not.

    A condition that holds in both frames is not repeated, and one that
    lapses and returns - the checks go red and green again, or the session
    works and rests again - is news both times. A row that was not on the
    previous frame is discovery, not a transition, and a pull request the
    previous frame did not show by number - unread, a failed readback's
    placeholder, or a different request - is the board catching up rather
    than the request changing, so neither notifies. A conflict new to the
    strip always does.
    """
    before = board_conditions(previous, previous_conflicts)
    after = board_conditions(current, current_conflicts)
    known = {row.key: row for row in previous}
    now = {row.key: row for row in current}
    found: list[BoardNotification] = []
    for key, notification in after.items():
        if key in before:
            continue
        row_key, kind = key
        if kind == TRANSITION_CONFLICT:
            found.append(notification)
            continue
        earlier = known.get(row_key)
        current_row = now.get(row_key)
        if earlier is None or current_row is None:
            continue
        if kind in PR_TRANSITIONS and _pr_number(earlier) != _pr_number(current_row):
            continue
        found.append(notification)
    return found


class BoardNotifier:
    """Remembers the last frame so each ``tick`` reports only what changed.

    The first tick is a baseline: opening the board on a green pull request is
    not news. Holds rows and conflicts only; no clock, no I/O.
    """

    def __init__(self) -> None:
        self._rows: tuple[BoardRow, ...] | None = None
        self._conflicts: tuple[Conflict, ...] = ()

    def tick(
        self, rows: Sequence[BoardRow], conflicts: Sequence[Conflict]
    ) -> list[BoardNotification]:
        current = tuple(rows)
        current_conflicts = tuple(conflicts)
        if self._rows is None:
            found: list[BoardNotification] = []
        else:
            found = board_transitions(
                self._rows, current, self._conflicts, current_conflicts
            )
        self._rows = current
        self._conflicts = current_conflicts
        return found


def selected_index(rows: Sequence[BoardRow], selected: str | None) -> int | None:
    """Where the selected row sits after a re-sort; the first row by default."""
    if not rows:
        return None
    for index, row in enumerate(rows):
        if row.key == selected:
            return index
    return 0


def move_selection(rows: Sequence[BoardRow], selected: str | None, step: int) -> str | None:
    """The key ``step`` rows away from the selection, clamped to the ends."""
    index = selected_index(rows, selected)
    if index is None:
        return None
    return rows[max(0, min(len(rows) - 1, index + step))].key


def issue_url(issue: LinkedIssue) -> str:
    """The web page for a linked issue, or "" when its repository is unknown."""
    if not issue.repository or "/" not in issue.repository:
        return ""
    return f"https://{issue.repository}/issues/{issue.number}"


def pr_url(row: BoardRow) -> str:
    url = str((row.github or {}).get("url") or "")
    return url if url.startswith(("http://", "https://")) else ""


def detail_title(row: BoardRow) -> str:
    """The header of the detail pane: who, where, and on what."""
    parts = [row.agent_name, row.surface]
    where = f"{row.repository} {row.branch}".strip()
    if where:
        parts.append(where)
    if row.model:
        parts.append(row.model)
    if row.issues:
        own = row.github_repository
        parts.append(", ".join(issue_label(issue, own) for issue in row.issues))
    return " · ".join(part for part in parts if part)


def sort_rows(rows: Iterable[BoardRow], group: str = "none") -> list[BoardRow]:
    """Working first, then blocked, idle, unknown, done; newest first within."""

    def rank(row: BoardRow) -> tuple[Any, ...]:
        head: tuple[Any, ...] = ()
        if group == "surface":
            head = (row.surface == UNKNOWN_SURFACE, row.surface.casefold())
        elif group == "repo":
            head = (not row.repository, row.repository.casefold(), row.repository_id)
        return (
            *head,
            STATUS_ORDER.get(row.status, len(STATUS_ORDER)),
            row.age_seconds is None,
            row.age_seconds if row.age_seconds is not None else 0.0,
            row.agent_name.casefold(),
            row.key,
        )

    return sorted(rows, key=rank)


def format_age(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def status_cell(row: BoardRow) -> str:
    word = {
        AgentStatus.WORKING: "working",
        AgentStatus.BLOCKED: "blocked",
        AgentStatus.IDLE: "idle",
        AgentStatus.DONE: "done",
        AgentStatus.UNKNOWN: "unknown",
    }[row.status]
    age = format_age(row.age_seconds)
    return " ".join(part for part in (STATUS_GLYPHS[row.status], word, age) if part)


def pr_cell(github: Mapping[str, Any] | None) -> str:
    """``#151 ✓ci ○rev``: the number, then checks, then review, at a glance."""
    if not github:
        return "—"
    number = github.get("number")
    if not isinstance(number, int):
        return "PR ?"
    state = str(github.get("state") or "").upper()
    if state == "MERGED":
        return f"#{number} merged"
    if state == "CLOSED":
        return f"#{number} closed"
    phase = github_ci_phase(dict(github))
    ci = {"passed": "✓ci", "failed": "✗ci", "pending": "…ci"}[phase]
    review = str(github.get("review") or "").upper()
    rev = {"APPROVED": "✓rev", "CHANGES_REQUESTED": "✗rev"}.get(review, "○rev")
    pieces = [f"#{number}", ci, rev]
    if github.get("draft"):
        pieces.append("draft")
    if github.get("coverage") == "PARTIAL":
        pieces.append("?")
    return " ".join(pieces)


def pr_color(github: Mapping[str, Any] | None) -> str:
    if not github:
        return ANSI["dim"]
    state = str(github.get("state") or "").upper()
    if state in {"MERGED", "CLOSED"}:
        return ANSI["dim"]
    if str(github.get("review") or "").upper() == "CHANGES_REQUESTED":
        return ANSI["red"]
    return {
        "passed": ANSI["green"],
        "failed": ANSI["red"],
        "pending": ANSI["yellow"],
    }[github_ci_phase(dict(github))]


def issue_label(issue: LinkedIssue, own_repository: str) -> str:
    """``#139`` confirmed, ``#139?`` inferred, prefixed by a foreign repository."""
    text = f"#{issue.number}" if issue.confirmed else f"#{issue.number}?"
    if issue.repository and issue.repository != own_repository:
        # The value is a ``host/owner/name`` triple, not a URL: take the host
        # apart by structure and drop it only when it is exactly github.com,
        # so ``evil-github.com/o/n`` and ``github.com.evil/o/n`` keep theirs.
        host, _, rest = issue.repository.partition("/")
        repository = rest if host == "github.com" and rest else issue.repository
        return f"{repository}{text}"
    return text


def issue_cell(row: BoardRow) -> str:
    """The first linked issue and how many more there are: ``#139 +1``."""
    if not row.issues:
        return "—"
    text = issue_label(row.issues[0], row.github_repository)
    if len(row.issues) > 1:
        text += f" +{len(row.issues) - 1}"
    return text


def issue_color(row: BoardRow) -> str:
    if not row.issues:
        return ANSI["dim"]
    return ANSI["blue"] if row.issues[0].confirmed else ANSI["dim"]


def repo_cell(row: BoardRow, group: str) -> str:
    if group == "repo":
        return row.branch
    if row.repository and row.branch:
        return f"{row.repository}  {row.branch}"
    return row.repository or row.branch


def cell_width(text: str) -> int:
    from wcwidth import wcswidth

    measured = wcswidth(text)
    return measured if measured >= 0 else len(text)


def crop(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    if width == 1:
        return "…"
    kept: list[str] = []
    used = 0
    for character in text:
        step = cell_width(character)
        if used + step > width - 1:
            break
        kept.append(character)
        used += step
    return "".join(kept) + "…"


def pad(text: str, width: int) -> str:
    text = crop(text, width)
    return text + " " * max(0, width - cell_width(text))


def _paint(text: str, code: str, color: bool) -> str:
    if not color or not text.strip():
        return text
    return f"{code}{text}{ANSI['reset']}"


@dataclass(frozen=True, slots=True)
class _Columns:
    agent: int
    surface: int
    repo: int
    issue: int
    pr: int
    status: int

    @property
    def show_surface(self) -> bool:
        return self.surface > 0

    @property
    def show_issue(self) -> bool:
        return self.issue > 0

    @property
    def show_pr(self) -> bool:
        return self.pr > 0


def _columns(rows: Sequence[BoardRow], width: int, group: str) -> _Columns:
    """Fit the columns to the pane, giving up PR, then ISSUE, then SURFACE.

    The row never exceeds ``width``: after the optional columns are gone the
    repository column takes whatever is left, and in a pane too narrow even
    for that the status column shrinks last, so the state of each session is
    the final thing to go rather than the first.
    """
    gap = 2
    agent = max([len("AGENT"), *(cell_width(row.agent_name) for row in rows)])
    agent = min(agent, 10)
    status = max([len("STATUS"), *(cell_width(status_cell(row)) for row in rows)])
    pr = max([len("PR"), *(cell_width(pr_cell(row.github)) for row in rows)])
    pr = min(pr, 22)
    issue = max([len("ISSUE"), *(cell_width(issue_cell(row)) for row in rows)])
    issue = min(issue, 24)
    surface = (
        0
        if group == "surface"
        else min(max([len("SURFACE"), *(cell_width(row.surface) for row in rows)]), 30)
    )
    repo_min = 12

    def remaining(surface_width: int, issue_width: int, pr_width: int) -> int:
        used = agent + gap + status
        used += surface_width + gap if surface_width else 0
        used += issue_width + gap if issue_width else 0
        used += pr_width + gap if pr_width else 0
        return width - used - gap

    if remaining(surface, issue, pr) < repo_min:
        pr = 0
    if remaining(surface, issue, pr) < repo_min:
        issue = 0
    if remaining(surface, issue, pr) < repo_min:
        surface = 0
    repo = remaining(surface, issue, pr)
    if repo < 4:
        # Too narrow for a readable repository next to the status: drop the
        # repository, shorten the agent name, and give status what is left.
        repo = 0
        agent = min(agent, 6)
        status = max(1, width - agent - gap)
    return _Columns(
        agent=agent, surface=surface, repo=repo, issue=issue, pr=pr, status=status
    )


def _line(cells: Sequence[tuple[str, int, str]], color: bool) -> str:
    """Join padded cells; ``cells`` are (text, width, ansi code) triples."""
    parts = []
    for text, width, code in cells:
        if width <= 0:
            continue
        parts.append(_paint(pad(text, width), code, color))
    return "  ".join(parts)


SELECTION_MARK = "▸ "
STRIP_MARK = "⚠ "


def render_board(
    rows: Sequence[BoardRow],
    width: int,
    height: int,
    color: bool,
    *,
    clock: str = "",
    group: str = "none",
    hints: str | None = None,
    discovering: bool = False,
    selected: str | None = None,
    warnings: Sequence[str] = (),
    detail: Sequence[str] | None = None,
    detail_heading: str = "",
) -> str:
    """Draw the roster as a fixed-width frame, one line per string row.

    ``group`` is ``none`` for the flat table, ``surface`` to put a header
    over each surface and drop the redundant column, or ``repo`` to do the
    same for repositories with the branch alone on the row. ``selected``
    names the row carrying the selection mark; ``warnings`` is the conflict
    strip; ``detail`` is the selected session's recent timeline, already
    rendered by the caller, shown under its ``detail_heading``. The detail
    pane takes at most a third of the height so the roster stays the point.
    """
    width = max(20, width)
    height = max(4, height)
    rows = sort_rows(rows, group)
    lines: list[str] = []
    gutter = len(SELECTION_MARK) if selected is not None and rows else 0
    selection = selected_index(rows, selected) if gutter else None

    repositories = {row.repository_id for row in rows if row.repository}
    repository_labels = _repository_labels(rows)
    summary = f"{len(rows)} session{'s' if len(rows) != 1 else ''}"
    if repositories:
        summary += f" · {len(repositories)} repo{'s' if len(repositories) != 1 else ''}"
    if discovering:
        summary += " · discovering"
    heading = f"SIDE DOG board · {summary}"
    gap = width - cell_width(heading) - cell_width(clock)
    if gap < 1:
        heading = crop(heading, max(1, width - cell_width(clock) - 1))
        gap = max(1, width - cell_width(heading) - cell_width(clock))
    masthead = heading + " " * gap + clock
    if color:
        name = "SIDE DOG"
        masthead = (
            f"{ANSI['magenta']}{ANSI['bold']}{name}{ANSI['reset']}"
            f"{ANSI['bold']}{ANSI['blue']}{masthead[len(name):]}{ANSI['reset']}"
        )
    lines.append(masthead)

    if not rows:
        lines.append("")
        for text in (
            "No coding-agent sessions found.",
            "Sessions appear here as Claude Code, Codex, and the other"
            " supported agents start working.",
        ):
            lines.append(_paint(crop(text, width), ANSI["dim"], color))
    else:
        columns = _columns(rows, width - gutter, group)
        header_cells = [
            ("AGENT", columns.agent, ANSI["dim"]),
            ("SURFACE", columns.surface, ANSI["dim"]),
            ("BRANCH" if group == "repo" else "REPO / BRANCH", columns.repo, ANSI["dim"]),
            ("ISSUE", columns.issue, ANSI["dim"]),
            ("PR", columns.pr, ANSI["dim"]),
            ("STATUS", columns.status, ANSI["dim"]),
        ]
        lines.append(" " * gutter + _line(header_cells, color))
        body: list[str] = []
        body_is_row: list[bool] = []
        current_group: str | None = None
        for index, row in enumerate(rows):
            if group != "none":
                label = (
                    row.surface
                    if group == "surface"
                    else repository_labels.get(row.repository_id, "no repository")
                )
                if label != current_group:
                    current_group = label
                    body.append(
                        " " * gutter
                        + _paint(crop(label, width - gutter), ANSI["dim"] + ANSI["bold"], color)
                    )
                    body_is_row.append(False)
            chosen = gutter and index == selection
            mark = SELECTION_MARK if chosen else " " * gutter
            cells = _line(
                [
                    (row.agent_name, columns.agent, ANSI["magenta"]),
                    (row.surface, columns.surface, ""),
                    (repo_cell(row, group), columns.repo, ""),
                    (issue_cell(row), columns.issue, issue_color(row)),
                    (pr_cell(row.github), columns.pr, pr_color(row.github)),
                    (status_cell(row), columns.status, STATUS_COLORS[row.status]),
                ],
                color,
            )
            body.append(_paint(mark, ANSI["blue"] + ANSI["bold"], color and bool(chosen)) + cells)
            body_is_row.append(True)
        strip = [
            _paint(crop(f"{STRIP_MARK}{text}", width), ANSI["yellow"], color)
            for text in warnings[:MAX_CONFLICTS]
        ]
        pane: list[str] = []
        if detail is not None:
            pane_room = max(3, height // 3)
            heading = crop(detail_heading or "detail", width)
            pane.append(_paint(heading, ANSI["dim"] + ANSI["bold"], color))
            shown_detail = list(detail)[-(pane_room - 1) :] if pane_room > 1 else []
            if not shown_detail:
                shown_detail = [_paint("no recent events for this session", ANSI["dim"], color)]
            pane.extend(shown_detail)
        # The roster is the point: in a short pane the detail pane goes first,
        # then the conflict strip, then the hints, before a single row does.
        def room_left() -> int:
            return height - (len(lines) + (1 if hints else 0) + len(strip) + len(pane))

        if room_left() < 1 and pane:
            pane = []
        if room_left() < 1 and strip:
            strip = []
        if room_left() < 1 and hints:
            hints = None
        room = max(1, room_left())
        if len(body) > room:
            # Keep the selected row on screen: scroll the body so it is
            # visible, and when only one line fits, spend it on that row
            # rather than on the "more" marker.
            shown = room - 1 if room >= 2 else 1
            start = 0
            if selection is not None:
                row_positions = [i for i, is_row in enumerate(body_is_row) if is_row]
                if selection < len(row_positions):
                    position = row_positions[selection]
                    if position >= start + shown:
                        start = position - shown + 1
            hidden = len(body) - shown
            window = body[start : start + shown]
            if group != "none" and start > 0 and shown >= 2 and selection is not None:
                # A grouped row does not repeat its group, so when the
                # header it sits under has scrolled off, pin it on top.
                header = max(
                    (i for i in range(start) if not body_is_row[i]), default=None
                )
                if header is not None and all(body_is_row[start : start + shown]):
                    window = [body[header]] + body[start + 1 : start + shown]
            body = window
            if room >= 2:
                body.append(_paint(f"… {hidden} more", ANSI["dim"], color))
        lines.extend(body)
        lines.extend(strip)
        lines.extend(pane)

    if hints:
        while len(lines) < height - 1:
            lines.append("")
        lines.append(_paint(crop(hints, width), ANSI["dim"], color))
    return "\n".join(crop(line, width) if not color else line for line in lines[:height])


def _repository_labels(rows: Sequence[BoardRow]) -> dict[str, str]:
    """A header per repository, told apart by parent folder when names clash.

    ``/org-a/api`` and ``/org-b/api`` are both called ``api``; grouping by the
    Git common directory keeps them separate, and the header says which is
    which: ``api (org-a)`` and ``api (org-b)``.
    """
    by_name: dict[str, dict[str, str]] = {}
    for row in rows:
        if not row.repository:
            continue
        by_name.setdefault(row.repository, {}).setdefault(row.repository_id, row.root)
    labels: dict[str, str] = {}
    for name, roots in by_name.items():
        for repository_id, root in roots.items():
            if len(roots) == 1:
                labels[repository_id] = name
            else:
                parent = PurePath(root).parent.name
                labels[repository_id] = f"{name} ({parent})" if parent else name
    return labels


def next_group(group: str) -> str:
    index = GROUPS.index(group) if group in GROUPS else 0
    return GROUPS[(index + 1) % len(GROUPS)]
