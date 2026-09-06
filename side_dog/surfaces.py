"""Name the terminal or app a session runs in from its process ancestry.

Herdr names its panes and the Claude registry names the desktop app and VS
Code, but a Claude Code or Codex session in a Ghostty window that Herdr did
not open has nothing to say about where it lives. Its process tree does: the
session's process hangs off a shell that hangs off the terminal that drew it.
This module walks that tree and, when it reaches something it recognises,
says so; otherwise it says nothing and the board keeps its honest ``unknown``.

Everything that touches ``ps``, ``lsof``, or ``/proc`` sits behind the small
functions at the top so tests can replace them with fixtures. Only process
ids, parent ids, and process names are ever read. Command-line arguments are
never requested, so no prompt text can reach a label.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

UNKNOWN = "unknown"
TERMINAL = "terminal"

# The only ``ps`` invocation this module makes. POSIX ``-e`` and ``-o`` work on
# macOS and Linux alike, and ``comm=`` is the executable name, never its
# arguments. Do not add ``args``, ``command``, or ``cmd`` here.
PS_COMMAND = ("ps", "-eo", "pid=,ppid=,comm=")
COMMAND_TIMEOUT_SECONDS = 2.0
# One snapshot of the process table serves every session on a poll.
TABLE_TTL_SECONDS = 2.0
# A shell inside a shell inside a terminal is deep enough; a cycle is not.
MAX_ANCESTRY_DEPTH = 32

DARWIN = sys.platform == "darwin"

# Executable basenames, casefolded, and the surface each names. The list is
# the terminals and apps that launch coding agents on this machine; anything
# else is passed over so an unfamiliar wrapper never becomes a wrong guess.
KNOWN_APPS: dict[str, str] = {
    "ghostty": "Ghostty",
    "herdr": "Herdr",
    "terminal": "Terminal",
    "iterm2": "iTerm2",
    "kitty": "kitty",
    "wezterm": "WezTerm",
    "wezterm-gui": "WezTerm",
    "code": "VS Code",
    "code helper (plugin)": "VS Code",
    "code helper (renderer)": "VS Code",
    # tmux's server is reparented to init the moment the client detaches, so
    # the walk can never reach the terminal behind it. Saying "tmux" is the
    # most that can honestly be said.
    "tmux": "tmux",
}

# The desktop apps share their command-line tools' names up to case: the CLI
# is ``claude`` or ``codex``, the app bundle's executable is ``Claude`` or
# ``Codex``. A case-insensitive match would label a CLI's own wrapper process
# as the desktop app, so these two match exactly.
DESKTOP_APPS: dict[str, str] = {
    "Claude": "Claude Desktop",
    "Codex": "Codex Desktop",
}

# Process names that identify a Codex process when tying a rollout file to
# one. Linux truncates ``comm`` to fifteen characters and the npm launcher
# runs a ``codex-aarch64-apple-darwin`` style binary, so match by prefix.
CODEX_PROCESS_PREFIX = "codex"


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    comm: str


# ---------------------------------------------------------------------------
# The only functions that touch the operating system. Tests patch these.


def _run(args: Sequence[str]) -> str:
    """Run one read-only tool and return its stdout, or nothing on any failure."""
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return completed.stdout or ""


def _proc_link(pid: int, name: str) -> str | None:
    """The target of ``/proc/<pid>/<name>`` (``cwd``, ``exe``), if readable."""
    try:
        return os.readlink(f"/proc/{pid}/{name}")
    except (OSError, ValueError):
        return None


def _proc_fd_targets(pid: int) -> list[str]:
    """Every file ``/proc/<pid>/fd`` says the process holds open."""
    directory = f"/proc/{pid}/fd"
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    targets: list[str] = []
    for name in names:
        try:
            targets.append(os.readlink(os.path.join(directory, name)))
        except OSError:
            continue
    return targets


# ---------------------------------------------------------------------------
# Process table.

# How long an unsettled Codex answer stands before the processes are asked
# again. A rollout nobody holds open belongs to a session that has finished
# or is between turns; looking every poll would cost an ``lsof`` each time.
CODEX_RETRY_SECONDS = 30.0

_LOCK = threading.Lock()
_TABLE: tuple[float, dict[int, ProcessInfo]] | None = None
_ANCESTRY: dict[int, str | None] = {}
_CODEX_PIDS: dict[str, int] = {}
_CODEX_UNSETTLED: dict[str, tuple[float, tuple[int, ...]]] = {}


def reset_caches() -> None:
    """Forget every snapshot and resolution. Tests call this between cases."""
    global _TABLE
    with _LOCK:
        _TABLE = None
        _ANCESTRY.clear()
        _CODEX_PIDS.clear()
        _CODEX_UNSETTLED.clear()


def parse_ps(text: str) -> dict[int, ProcessInfo]:
    """Rows of ``ps -eo pid=,ppid=,comm=`` keyed by pid.

    ``comm`` is everything after the second column, because a macOS ``comm``
    is a full path and application bundles have spaces in their names.
    """
    table: dict[int, ProcessInfo] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if pid <= 0:
            continue
        table[pid] = ProcessInfo(pid=pid, ppid=ppid, comm=parts[2].strip())
    return table


def process_table(now: float | None = None) -> dict[int, ProcessInfo]:
    """One ``ps`` snapshot, shared by every question asked within a poll."""
    global _TABLE
    moment = time.monotonic() if now is None else now
    with _LOCK:
        if _TABLE is not None and 0 <= moment - _TABLE[0] < TABLE_TTL_SECONDS:
            return _TABLE[1]
    table = parse_ps(_run(PS_COMMAND))
    with _LOCK:
        _TABLE = (moment, table)
    return table


def comm_name(comm: str) -> str:
    """The executable's basename: ``/Applications/Ghostty.app/.../ghostty`` is ``ghostty``."""
    text = comm.strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def app_for(comm: str) -> str | None:
    """The surface a process name identifies, or nothing when it is not one."""
    name = comm_name(comm)
    if not name:
        return None
    desktop = DESKTOP_APPS.get(name)
    if desktop:
        return desktop
    return KNOWN_APPS.get(name.casefold())


def walk_ancestry(pid: int, table: Mapping[int, ProcessInfo]) -> str | None:
    """Climb from ``pid``'s parent until a known terminal or app, or give up.

    The session's own process is skipped: it is the agent, not the window
    it runs in. Nothing is guessed from an unrecognised chain.
    """
    current = table.get(pid)
    if current is None:
        return None
    seen = {pid}
    for _ in range(MAX_ANCESTRY_DEPTH):
        parent = table.get(current.ppid)
        if parent is None or parent.pid in seen:
            return None
        seen.add(parent.pid)
        found = app_for(parent.comm)
        if found:
            return found
        current = parent
    return None


def ancestry_surface(
    pid: int, table: Mapping[int, ProcessInfo] | None = None
) -> str | None:
    """The terminal or app above ``pid``, remembered for as long as it lives.

    A process that is no longer in the table has died, so its remembered
    answer is dropped and nothing is returned; a reused pid starts over.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if table is None:
        table = process_table()
    with _LOCK:
        if pid not in table:
            _ANCESTRY.pop(pid, None)
            return None
        if pid in _ANCESTRY:
            return _ANCESTRY[pid]
    found = walk_ancestry(pid, table)
    with _LOCK:
        _ANCESTRY[pid] = found
    return found


# ---------------------------------------------------------------------------
# Codex: tie a rollout file to the process writing it.


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except (OSError, ValueError):
        return False


def _codex_pids(table: Mapping[int, ProcessInfo]) -> list[int]:
    return sorted(
        pid
        for pid, info in table.items()
        if comm_name(info.comm).casefold().startswith(CODEX_PROCESS_PREFIX)
    )


def _pids_from_lines(text: str) -> list[int]:
    pids: list[int] = []
    for token in text.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def processes_holding(path: str, table: Mapping[int, ProcessInfo]) -> list[int]:
    """Pids with ``path`` open: ``lsof`` on macOS, ``/proc/*/fd`` elsewhere.

    Only Codex-named processes are asked about: a rollout file is held by the
    Codex that writes it, and an unrestricted ``lsof`` walks every process's
    file table, which takes seconds on a busy Mac and, on Linux, is forbidden
    for other users' processes. With no Codex running nothing is asked at all.
    """
    candidates = _codex_pids(table)
    if not candidates:
        return []
    if DARWIN:
        joined = ",".join(str(pid) for pid in candidates)
        found = _pids_from_lines(_run(("lsof", "-t", "-a", "-p", joined, path)))
        return [pid for pid in found if pid in table]
    holders: list[int] = []
    for pid in candidates:
        if any(_same_path(target, path) for target in _proc_fd_targets(pid)):
            holders.append(pid)
    return holders


def _parse_lsof_cwds(text: str) -> dict[int, str]:
    """``lsof -Fpn`` output: a ``p<pid>`` line, then ``n<path>`` for its cwd."""
    cwds: dict[int, str] = {}
    pid: int | None = None
    for line in text.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None and pid not in cwds:
            cwds[pid] = line[1:]
    return cwds


def codex_processes_in(cwd: str, table: Mapping[int, ProcessInfo]) -> list[int]:
    """Codex processes whose working directory is ``cwd``."""
    candidates = _codex_pids(table)
    if not candidates or not cwd:
        return []
    cwds: dict[int, str] = {}
    if DARWIN:
        joined = ",".join(str(pid) for pid in candidates)
        cwds = _parse_lsof_cwds(
            _run(("lsof", "-a", "-d", "cwd", "-p", joined, "-Fpn"))
        )
    else:
        for pid in candidates:
            target = _proc_link(pid, "cwd")
            if target:
                cwds[pid] = target
    return [pid for pid in candidates if _same_path(cwds.get(pid, ""), cwd)]


def codex_session_pids(
    rollout_path: str,
    cwd: str = "",
    table: Mapping[int, ProcessInfo] | None = None,
    now: float | None = None,
) -> tuple[int, ...]:
    """The process a Codex rollout file belongs to, or the candidates for it.

    The process holding the file open is the answer whenever there is one:
    Codex appends to its rollout for the life of the session. Otherwise every
    Codex process working in the rollout's ``cwd`` is a candidate, and the
    caller treats more than one as ambiguous rather than picking. A settled
    answer is remembered until that process leaves the table; an unsettled
    one (nothing found, or two candidates) is kept for
    :data:`CODEX_RETRY_SECONDS` so an idle session does not cost an ``lsof``
    every poll.
    """
    if table is None:
        table = process_table()
    moment = time.monotonic() if now is None else now
    with _LOCK:
        remembered = _CODEX_PIDS.get(rollout_path)
        if remembered is not None:
            if remembered in table:
                return (remembered,)
            del _CODEX_PIDS[rollout_path]
        unsettled = _CODEX_UNSETTLED.get(rollout_path)
        if unsettled is not None and 0 <= moment - unsettled[0] < CODEX_RETRY_SECONDS:
            return unsettled[1]
    holders = processes_holding(rollout_path, table)
    if holders:
        chosen = min(holders)
        with _LOCK:
            _CODEX_PIDS[rollout_path] = chosen
            _CODEX_UNSETTLED.pop(rollout_path, None)
        return (chosen,)
    matches = tuple(codex_processes_in(cwd, table))
    with _LOCK:
        if len(matches) == 1:
            _CODEX_PIDS[rollout_path] = matches[0]
            _CODEX_UNSETTLED.pop(rollout_path, None)
        else:
            _CODEX_UNSETTLED[rollout_path] = (moment, matches)
    return matches


# ---------------------------------------------------------------------------
# Resolution.


def names_an_app(surface: str) -> bool:
    """True for a surface a loader already settled, like ``Claude Desktop``."""
    text = (surface or "").strip()
    return bool(text) and text.casefold() not in {TERMINAL, UNKNOWN}


def resolve_surface(
    surface: str,
    *,
    pid: int | None = None,
    rollout_path: str | None = None,
    cwd: str = "",
) -> str:
    """Refine a loader's surface from the process tree, never guessing.

    An app or editor the loader already named wins outright. ``terminal``
    and an empty surface are exactly what ancestry can improve: the terminal
    or app found above the process replaces them, an unrecognised chain keeps
    ``terminal``, and when nothing at all is known the answer is ``unknown``.
    A Codex rollout that could belong to two processes is ``unknown`` too:
    two sessions in one worktree is the case the board exists to show, and
    guessing between them would put a wrong window on the row.
    """
    current = (surface or "").strip()
    if names_an_app(current):
        return current
    try:
        if pid is None and rollout_path:
            candidates = codex_session_pids(rollout_path, cwd)
            if len(candidates) > 1:
                return UNKNOWN
            pid = candidates[0] if candidates else None
        if pid is not None:
            found = ancestry_surface(pid)
            if found:
                return found
    except Exception:
        pass
    return current or UNKNOWN
