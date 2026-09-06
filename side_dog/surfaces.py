"""Name the terminal or app a session runs in from its process ancestry.

Herdr names its panes and the Claude registry names the desktop app and VS
Code, but a Claude Code or Codex session in a Ghostty window that Herdr did
not open has nothing to say about where it lives. Its process tree does: the
session's process hangs off a shell that hangs off the terminal that drew it.
This module walks that tree and, when it reaches something it recognises,
says so; otherwise it says nothing and the board keeps its honest ``unknown``.

Everything that touches ``ps``, ``lsof``, or ``/proc`` sits behind the small
functions at the top so tests can replace them with fixtures. What is read is
deliberately narrow: process ids, parent ids, start times, and executable
names from ``ps``; each Codex process's working directory; and whether a
Codex process holds a given rollout file open, decided by comparing file
identity (device and inode) rather than by reading the names of its open
descriptors. Command-line arguments are never requested, so no prompt text
can reach a label.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

UNKNOWN = "unknown"
TERMINAL = "terminal"

# The only ``ps`` invocation this module makes. POSIX ``-e`` and ``-o`` work on
# macOS and Linux alike, ``lstart=`` is the start time that tells a recycled
# pid from the process that had it before, and ``comm=`` is the executable
# name, never its arguments. Do not add ``args``, ``command``, or ``cmd`` here.
PS_COMMAND = ("ps", "-eo", "pid=,ppid=,lstart=,comm=")
# ``lstart`` prints like ``Sat Sep  6 21:07:13 2026``: five tokens on both
# platforms, so the columns are pid, ppid, five date tokens, then comm.
LSTART_TOKENS = 5
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

# How long an unsettled Codex answer stands before the processes are asked
# again. A rollout nobody holds open belongs to a session that has finished
# or is between turns; looking every poll would cost an ``lsof`` each time.
CODEX_RETRY_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    comm: str
    # The start time as ``ps`` printed it. Together with the pid it names one
    # process for life: a pid the kernel hands out again starts at a new time.
    start: str = ""


@dataclass(frozen=True, slots=True)
class CodexRequest:
    """One recent Codex rollout: the file and the ``cwd`` its header names."""

    rollout_path: str
    cwd: str = ""


@dataclass(frozen=True, slots=True)
class CodexResolution:
    """Which process a rollout belongs to, or why that cannot be said.

    ``pid`` is set only when exactly one process can own the rollout.
    ``ambiguous`` is true when several processes could, or when the only
    evidence is a working directory that several recent rollouts share.
    ``candidates`` lists the processes considered, for the record.
    """

    pid: int | None = None
    ambiguous: bool = False
    candidates: tuple[int, ...] = ()


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
    """The target of ``/proc/<pid>/<name>``. Used for ``cwd`` only."""
    try:
        return os.readlink(f"/proc/{pid}/{name}")
    except (OSError, ValueError):
        return None


def _file_identity(path: str) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of a file: the same across every name it has."""
    try:
        status = os.stat(path)
    except (OSError, ValueError):
        return None
    return (status.st_dev, status.st_ino)


def _proc_fd_identities(pid: int) -> frozenset[tuple[int, int]]:
    """The identities of every file ``pid`` holds open, via ``/proc``.

    Each descriptor is ``stat``-ed, which follows the link to the file it
    names without ever producing the name. The names of a process's other
    open files are nobody's business here.
    """
    directory = f"/proc/{pid}/fd"
    try:
        names = os.listdir(directory)
    except OSError:
        return frozenset()
    identities: set[tuple[int, int]] = set()
    for name in names:
        try:
            status = os.stat(os.path.join(directory, name))
        except OSError:
            continue
        identities.add((status.st_dev, status.st_ino))
    return frozenset(identities)


# ---------------------------------------------------------------------------
# Process table.

_LOCK = threading.Lock()
_TABLE: tuple[float, dict[int, ProcessInfo]] | None = None
# Cached answers carry the start token of the process they were computed
# for, and are dropped the moment the table shows that pid with another.
_ANCESTRY: dict[int, tuple[str, str | None]] = {}
_CODEX_PIDS: dict[str, tuple[int, str]] = {}
_CODEX_UNSETTLED: dict[str, tuple[float, CodexResolution]] = {}


@dataclass
class _Snapshot:
    """What has been asked about the Codex processes of one table.

    Every probe here is made at most once per process-table snapshot,
    however many rollouts ask, so a board with many Codex sessions costs
    one ``lsof`` for open files and one for working directories per poll.
    """

    table: Mapping[int, ProcessInfo]
    cwds: dict[int, str] | None = None
    fd_identities: dict[int, frozenset[tuple[int, int]]] = field(default_factory=dict)
    holders: dict[str, tuple[int, ...]] = field(default_factory=dict)


_SNAPSHOT: _Snapshot | None = None


def reset_caches() -> None:
    """Forget every snapshot and resolution. Tests call this between cases."""
    global _TABLE, _SNAPSHOT
    with _LOCK:
        _TABLE = None
        _SNAPSHOT = None
        _ANCESTRY.clear()
        _CODEX_PIDS.clear()
        _CODEX_UNSETTLED.clear()


def parse_ps(text: str) -> dict[int, ProcessInfo]:
    """Rows of ``ps -eo pid=,ppid=,lstart=,comm=`` keyed by pid.

    ``comm`` is everything after the start time, because a macOS ``comm``
    is a full path and application bundles have spaces in their names.
    """
    table: dict[int, ProcessInfo] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 2 + LSTART_TOKENS)
        if len(parts) < 3 + LSTART_TOKENS:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if pid <= 0:
            continue
        start = " ".join(parts[2 : 2 + LSTART_TOKENS])
        table[pid] = ProcessInfo(
            pid=pid, ppid=ppid, comm=parts[2 + LSTART_TOKENS].strip(), start=start
        )
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


def _snapshot_for(table: Mapping[int, ProcessInfo]) -> _Snapshot:
    global _SNAPSHOT
    with _LOCK:
        if _SNAPSHOT is None or _SNAPSHOT.table is not table:
            _SNAPSHOT = _Snapshot(table=table)
        return _SNAPSHOT


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
    answer is dropped and nothing is returned. A pid that is back with a
    different start time is a different process and starts over.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if table is None:
        table = process_table()
    info = table.get(pid)
    with _LOCK:
        if info is None:
            _ANCESTRY.pop(pid, None)
            return None
        cached = _ANCESTRY.get(pid)
        if cached is not None and cached[0] == info.start:
            return cached[1]
    found = walk_ancestry(pid, table)
    with _LOCK:
        _ANCESTRY[pid] = (info.start, found)
    return found


# ---------------------------------------------------------------------------
# Codex: tie rollout files to the processes writing them.


def _normal_path(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _same_path(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left == right or _normal_path(left) == _normal_path(right)


def _codex_pids(table: Mapping[int, ProcessInfo]) -> list[int]:
    return sorted(
        pid
        for pid, info in table.items()
        if comm_name(info.comm).casefold().startswith(CODEX_PROCESS_PREFIX)
    )


def _parse_lsof_fields(text: str) -> dict[int, list[str]]:
    """``lsof -Fpn`` output: a ``p<pid>`` line, then ``n<name>`` lines for it."""
    names: dict[int, list[str]] = {}
    pid: int | None = None
    for line in text.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
            else:
                names.setdefault(pid, [])
        elif line.startswith("n") and pid is not None:
            names[pid].append(line[1:])
    return names


def _codex_cwds(snapshot: _Snapshot, codex: Sequence[int]) -> dict[int, str]:
    """Each Codex process's working directory, asked once per snapshot."""
    if snapshot.cwds is None:
        cwds: dict[int, str] = {}
        if codex:
            if DARWIN:
                joined = ",".join(str(pid) for pid in codex)
                listed = _parse_lsof_fields(
                    _run(("lsof", "-a", "-d", "cwd", "-p", joined, "-Fpn"))
                )
                cwds = {pid: names[0] for pid, names in listed.items() if names}
            else:
                for pid in codex:
                    target = _proc_link(pid, "cwd")
                    if target:
                        cwds[pid] = target
        snapshot.cwds = cwds
    return snapshot.cwds


def _codex_holders(
    snapshot: _Snapshot, codex: Sequence[int], paths: Sequence[str]
) -> dict[str, tuple[int, ...]]:
    """Which Codex processes hold each rollout open, one probe per snapshot.

    macOS asks ``lsof`` about the Codex pids and the requested paths in one
    call. Linux reads each Codex process's descriptor identities once and
    compares them with each rollout's own device and inode.
    """
    missing = [path for path in paths if path not in snapshot.holders]
    if missing and codex:
        if DARWIN:
            joined = ",".join(str(pid) for pid in codex)
            listed = _parse_lsof_fields(
                _run(("lsof", "-a", "-p", joined, "-Fpn", *missing))
            )
            for path in missing:
                snapshot.holders[path] = tuple(
                    pid
                    for pid in codex
                    if any(_same_path(name, path) for name in listed.get(pid, ()))
                )
        else:
            for pid in codex:
                if pid not in snapshot.fd_identities:
                    snapshot.fd_identities[pid] = _proc_fd_identities(pid)
            for path in missing:
                identity = _file_identity(path)
                snapshot.holders[path] = tuple(
                    pid
                    for pid in codex
                    if identity is not None and identity in snapshot.fd_identities[pid]
                )
    else:
        for path in missing:
            snapshot.holders[path] = ()
    return {path: snapshot.holders.get(path, ()) for path in paths}


def resolve_codex_sessions(
    requests: Iterable[CodexRequest],
    table: Mapping[int, ProcessInfo] | None = None,
    now: float | None = None,
) -> dict[str, CodexResolution]:
    """Tie every recent rollout to a process, all in one pass.

    Pass every recent rollout, not only the ones the board is asking about:
    the working-directory fallback is only trusted when a single recent
    rollout names that directory. A rollout that ended minutes ago is no
    longer held open, and a new session started in the same folder would
    otherwise be the sole process there and be credited to both.

    The processes holding a rollout open are the answer whenever exactly one
    does. When none does, the Codex processes working in the rollout's
    ``cwd`` are the candidates. Several processes, or several rollouts for
    the one ``cwd``, are ambiguous rather than a pick. A settled answer is
    remembered until that process leaves the table or its pid comes back
    with another start time; an unsettled one is kept for
    :data:`CODEX_RETRY_SECONDS` so an idle session does not cost a probe
    every poll.
    """
    requests = list(requests)
    if table is None:
        table = process_table()
    moment = time.monotonic() if now is None else now
    results: dict[str, CodexResolution] = {}
    pending: list[CodexRequest] = []
    with _LOCK:
        for request in requests:
            path = request.rollout_path
            remembered = _CODEX_PIDS.get(path)
            if remembered is not None:
                info = table.get(remembered[0])
                if info is not None and info.start == remembered[1]:
                    results[path] = CodexResolution(
                        pid=remembered[0], candidates=(remembered[0],)
                    )
                    continue
                del _CODEX_PIDS[path]
            unsettled = _CODEX_UNSETTLED.get(path)
            if unsettled is not None and 0 <= moment - unsettled[0] < CODEX_RETRY_SECONDS:
                results[path] = unsettled[1]
                continue
            pending.append(request)
    if not pending:
        return results

    codex = _codex_pids(table)
    snapshot = _snapshot_for(table)
    rollouts_per_cwd = Counter(
        _normal_path(request.cwd) for request in requests if request.cwd
    )
    holders = _codex_holders(snapshot, codex, [request.rollout_path for request in pending])
    cwds: dict[int, str] = {}
    if any(not holders.get(request.rollout_path) for request in pending):
        cwds = _codex_cwds(snapshot, codex)

    for request in pending:
        path = request.rollout_path
        candidates = holders.get(path, ())
        shared = False
        if not candidates and request.cwd:
            candidates = tuple(
                pid for pid in codex if _same_path(cwds.get(pid, ""), request.cwd)
            )
            shared = rollouts_per_cwd[_normal_path(request.cwd)] > 1
        if len(candidates) == 1 and not shared:
            resolution = CodexResolution(pid=candidates[0], candidates=candidates)
            with _LOCK:
                _CODEX_PIDS[path] = (candidates[0], table[candidates[0]].start)
                _CODEX_UNSETTLED.pop(path, None)
        else:
            resolution = CodexResolution(ambiguous=bool(candidates), candidates=candidates)
            with _LOCK:
                _CODEX_UNSETTLED[path] = (moment, resolution)
        results[path] = resolution
    return results


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
    ambiguous: bool = False,
) -> str:
    """Refine a loader's surface from the process tree, never guessing.

    An app or editor the loader already named wins outright. ``terminal``
    and an empty surface are exactly what ancestry can improve: the terminal
    or app found above ``pid`` replaces them, an unrecognised chain keeps
    ``terminal``, and when nothing at all is known the answer is ``unknown``.
    An ``ambiguous`` session is ``unknown`` too: two sessions in one worktree
    is the case the board exists to show, and guessing between them would
    put a wrong window on the row.
    """
    current = (surface or "").strip()
    if names_an_app(current):
        return current
    if ambiguous:
        return UNKNOWN
    try:
        if pid is not None:
            found = ancestry_surface(pid)
            if found:
                return found
    except Exception:
        pass
    return current or UNKNOWN
