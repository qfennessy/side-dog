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

from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any, Iterable, Mapping, Sequence

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
    not the folder's. ``activity`` is the newest event time per session id,
    in epoch milliseconds, read from the folder's own history tail.
    """

    root: str
    repository: str = ""
    branch: str = ""
    github: Mapping[str, Any] | None = None
    identities: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    branches: Mapping[str, str] = field(default_factory=dict)
    activity: Mapping[str, int] = field(default_factory=dict)


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
    label: str = ""
    model: str = ""
    session_id: str = ""
    pane_id: str = ""
    github: Mapping[str, Any] | None = None

    @property
    def agent_name(self) -> str:
        return agent_label(self.agent)


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
        epoch = source.activity.get(session_id) if session_id else None
        age = (now_ms - epoch) / 1000 if isinstance(epoch, int) and epoch else None
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
        rows.append(
            BoardRow(
                key=key,
                agent=normalize_provider(identity.get("agent")),
                surface=surface_label(identity),
                repository=source.repository,
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
            )
        )
    return rows


def sort_rows(rows: Iterable[BoardRow], group: str = "none") -> list[BoardRow]:
    """Working first, then blocked, idle, unknown, done; newest first within."""

    def rank(row: BoardRow) -> tuple[Any, ...]:
        head: tuple[Any, ...] = ()
        if group == "surface":
            head = (row.surface == UNKNOWN_SURFACE, row.surface.casefold())
        elif group == "repo":
            head = (not row.repository, row.repository.casefold())
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
    pr: int
    status: int

    @property
    def show_surface(self) -> bool:
        return self.surface > 0

    @property
    def show_pr(self) -> bool:
        return self.pr > 0


def _columns(rows: Sequence[BoardRow], width: int, group: str) -> _Columns:
    """Fit the columns to the pane, giving up PR then SURFACE when narrow.

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
    surface = (
        0
        if group == "surface"
        else min(max([len("SURFACE"), *(cell_width(row.surface) for row in rows)]), 30)
    )
    repo_min = 12

    def remaining(surface_width: int, pr_width: int) -> int:
        used = agent + gap + status
        used += surface_width + gap if surface_width else 0
        used += pr_width + gap if pr_width else 0
        return width - used - gap

    if remaining(surface, pr) < repo_min:
        pr = 0
    if remaining(surface, pr) < repo_min:
        surface = 0
    repo = remaining(surface, pr)
    if repo < 4:
        # Too narrow for a readable repository next to the status: drop the
        # repository, shorten the agent name, and give status what is left.
        repo = 0
        agent = min(agent, 6)
        status = max(1, width - agent - gap)
    return _Columns(agent=agent, surface=surface, repo=repo, pr=pr, status=status)


def _line(cells: Sequence[tuple[str, int, str]], color: bool) -> str:
    """Join padded cells; ``cells`` are (text, width, ansi code) triples."""
    parts = []
    for text, width, code in cells:
        if width <= 0:
            continue
        parts.append(_paint(pad(text, width), code, color))
    return "  ".join(parts)


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
) -> str:
    """Draw the roster as a fixed-width frame, one line per string row.

    ``group`` is ``none`` for the flat table, ``surface`` to put a header
    over each surface and drop the redundant column, or ``repo`` to do the
    same for repositories with the branch alone on the row.
    """
    width = max(20, width)
    height = max(4, height)
    rows = sort_rows(rows, group)
    lines: list[str] = []

    repositories = {row.repository for row in rows if row.repository}
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
        lines.append(_paint("No coding-agent sessions found.", ANSI["dim"], color))
        lines.append(
            _paint(
                "Sessions appear here as Claude Code, Codex, and the other"
                " supported agents start working.",
                ANSI["dim"],
                color,
            )
        )
    else:
        columns = _columns(rows, width, group)
        header_cells = [
            ("AGENT", columns.agent, ANSI["dim"]),
            ("SURFACE", columns.surface, ANSI["dim"]),
            ("BRANCH" if group == "repo" else "REPO / BRANCH", columns.repo, ANSI["dim"]),
            ("PR", columns.pr, ANSI["dim"]),
            ("STATUS", columns.status, ANSI["dim"]),
        ]
        lines.append(_line(header_cells, color))
        body: list[str] = []
        current_group: str | None = None
        for row in rows:
            if group != "none":
                label = row.surface if group == "surface" else (row.repository or "no repository")
                if label != current_group:
                    current_group = label
                    body.append(_paint(crop(label, width), ANSI["dim"] + ANSI["bold"], color))
            body.append(
                _line(
                    [
                        (row.agent_name, columns.agent, ANSI["magenta"]),
                        (row.surface, columns.surface, ""),
                        (repo_cell(row, group), columns.repo, ""),
                        (pr_cell(row.github), columns.pr, pr_color(row.github)),
                        (status_cell(row), columns.status, STATUS_COLORS[row.status]),
                    ],
                    color,
                )
            )
        reserved = len(lines) + (1 if hints else 0)
        room = height - reserved
        if len(body) > room:
            shown = max(0, room - 1)
            hidden = len(body) - shown
            body = body[:shown] + [
                _paint(f"… {hidden} more", ANSI["dim"], color)
            ]
        lines.extend(body)

    if hints:
        while len(lines) < height - 1:
            lines.append("")
        lines.append(_paint(crop(hints, width), ANSI["dim"], color))
    return "\n".join(crop(line, width) if not color else line for line in lines[:height])


def next_group(group: str) -> str:
    index = GROUPS.index(group) if group in GROUPS else 0
    return GROUPS[(index + 1) % len(GROUPS)]
