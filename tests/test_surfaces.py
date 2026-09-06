import os
import tempfile
from contextlib import contextmanager
from typing import Iterator
from unittest import TestCase, skipUnless
from unittest.mock import patch

from side_dog import surfaces
from side_dog.surfaces import (
    PS_COMMAND,
    CodexRequest,
    CodexResolution,
    ancestry_surface,
    app_for,
    parse_ps,
    reset_caches,
    resolve_codex_sessions,
    resolve_surface,
    walk_ancestry,
)

ROLLOUT = "/Users/q/.codex/sessions/2026/09/06/rollout-x1.jsonl"
ROLLOUT_2 = "/Users/q/.codex/sessions/2026/09/06/rollout-x2.jsonl"
ROLLOUT_3 = "/Users/q/.codex/sessions/2026/09/06/rollout-x3.jsonl"

# A macOS process table as ``ps -eo pid=,ppid=,lstart=,comm=`` prints it: a
# five-token start time, full paths for app bundles, login shells with a
# leading dash, and no arguments anywhere.
PS = """\
    1     0 Sat Sep  6 09:00:00 2026 /sbin/launchd
  400     1 Sat Sep  6 09:00:01 2026 /Applications/Ghostty.app/Contents/MacOS/ghostty
  410   400 Sat Sep  6 09:00:02 2026 login
  420   410 Sat Sep  6 09:00:02 2026 -zsh
  500   420 Sat Sep  6 09:00:05 2026 claude
  600     1 Sat Sep  6 09:01:00 2026 /Applications/Herdr.app/Contents/MacOS/Herdr
  610   600 Sat Sep  6 09:01:01 2026 zsh
  620   610 Sat Sep  6 09:01:05 2026 codex
  700     1 Sat Sep  6 09:02:00 2026 zsh
  710   700 Sat Sep  6 09:02:05 2026 claude
  800     1 Sat Sep  6 09:03:00 2026 tmux
  810   800 Sat Sep  6 09:03:01 2026 -zsh
  820   810 Sat Sep  6 09:03:05 2026 codex
  900     1 Sat Sep  6 09:04:00 2026 /Applications/Claude.app/Contents/MacOS/Claude
  910   900 Sat Sep  6 09:04:05 2026 claude
  950   420 Sat Sep  6 09:05:00 2026 claude
  960   950 Sat Sep  6 09:05:05 2026 claude
 1000     1 Sat Sep  6 09:06:00 2026 /Applications/Visual Studio Code.app/Contents/MacOS/Electron
 1010  1000 Sat Sep  6 09:06:01 2026 /Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin)
 1020  1010 Sat Sep  6 09:06:02 2026 zsh
 1030  1020 Sat Sep  6 09:06:05 2026 claude
"""

# Two Codex sessions in two terminals, the case the tie-to-process step is for.
PS_CODEX = """\
    1     0 Sat Sep  6 09:00:00 2026 /sbin/launchd
  400     1 Sat Sep  6 09:00:01 2026 /Applications/Ghostty.app/Contents/MacOS/ghostty
  420   400 Sat Sep  6 09:00:02 2026 zsh
  500   420 Sat Sep  6 09:00:05 2026 codex
  600     1 Sat Sep  6 09:01:00 2026 /Applications/Herdr.app/Contents/MacOS/Herdr
  610   600 Sat Sep  6 09:01:01 2026 zsh
  620   610 Sat Sep  6 09:01:05 2026 codex
  700     1 Sat Sep  6 09:02:00 2026 zsh
  710   700 Sat Sep  6 09:02:05 2026 python3
"""

NO_CODEX = """\
    1     0 Sat Sep  6 09:00:00 2026 /sbin/launchd
  400     1 Sat Sep  6 09:00:01 2026 ghostty
  420   400 Sat Sep  6 09:00:02 2026 zsh
  500   420 Sat Sep  6 09:00:05 2026 claude
"""


def fake_identity(path: str) -> tuple[int, int]:
    """A stand-in device and inode for a path that need not exist."""
    return (1, sum(ord(character) for character in path))


@contextmanager
def probe(
    ps: str,
    *,
    darwin: bool = False,
    holders: dict[int, list[str]] | None = None,
    cwds: dict[int, str] | None = None,
) -> Iterator[list[tuple[str, ...]]]:
    """Stand in for ``ps``, ``lsof``, and ``/proc`` and record every call.

    ``holders`` maps a pid to the rollout paths it has open and ``cwds`` a
    pid to its working directory; the fakes answer from them whichever
    platform is being pretended.
    """
    calls: list[tuple[str, ...]] = []
    holders = holders or {}
    cwds = cwds or {}

    def fake_run(args):
        calls.append(tuple(args))
        if args[0] == "ps":
            return ps
        if args[:3] == ("lsof", "-a", "-d"):
            pids = [int(part) for part in args[5].split(",")]
            return "".join(f"p{pid}\nn{cwds[pid]}\n" for pid in pids if pid in cwds)
        if args[:3] == ("lsof", "-a", "-p"):
            pids = [int(part) for part in args[3].split(",")]
            asked = set(args[5:])
            out = []
            for pid in pids:
                out.append(f"p{pid}\n")
                out.extend(f"n{path}\n" for path in holders.get(pid, []) if path in asked)
            return "".join(out)
        raise AssertionError(f"unexpected command {args!r}")

    def fake_fd_identities(pid):
        calls.append(("proc-fd", str(pid)))
        return frozenset(fake_identity(path) for path in holders.get(pid, []))

    def fake_link(pid, name):
        calls.append(("proc-link", str(pid), name))
        return cwds.get(pid)

    reset_caches()
    try:
        with patch.object(surfaces, "DARWIN", darwin), patch.object(
            surfaces, "_run", side_effect=fake_run
        ), patch.object(
            surfaces, "_proc_fd_identities", side_effect=fake_fd_identities
        ), patch.object(
            surfaces, "_file_identity", side_effect=fake_identity
        ), patch.object(surfaces, "_proc_link", side_effect=fake_link):
            yield calls
    finally:
        reset_caches()


def restarted(ps: str, pid: int, ppid: int, comm: str, start: str) -> str:
    """The table with ``pid`` recycled: a new parent and a new start time."""
    lines = []
    for line in ps.splitlines():
        if line.split()[:1] == [str(pid)]:
            line = f"{pid:>5} {ppid:>5} {start} {comm}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def resolve_one(path: str, cwd: str, *others: CodexRequest, **kwargs) -> CodexResolution:
    return resolve_codex_sessions([CodexRequest(path, cwd), *others], **kwargs)[path]


def codex_surface(surface: str, path: str, cwd: str, *others: CodexRequest) -> str:
    resolution = resolve_one(path, cwd, *others)
    return resolve_surface(surface, pid=resolution.pid, ambiguous=resolution.ambiguous)


class ParseTest(TestCase):
    def test_comm_keeps_paths_with_spaces(self) -> None:
        table = parse_ps(PS)
        self.assertEqual(table[1010].ppid, 1000)
        self.assertTrue(table[1010].comm.endswith("Code Helper (Plugin)"))
        self.assertEqual(table[420].comm, "-zsh")

    def test_start_time_is_kept_as_the_process_identity(self) -> None:
        # Whitespace is normalised: the token names a process, it is not shown.
        table = parse_ps(PS)
        self.assertEqual(table[500].start, "Sat Sep 6 09:00:05 2026")
        self.assertEqual(table[1].start, "Sat Sep 6 09:00:00 2026")
        self.assertNotEqual(table[500].start, table[620].start)

    def test_malformed_lines_are_skipped(self) -> None:
        self.assertEqual(
            parse_ps("garbage\n  x  y z\n  5 1\n  5 1 Sat Sep 6 zsh\n"), {}
        )

    def test_ps_reads_names_parents_and_start_times_only(self) -> None:
        self.assertEqual(PS_COMMAND[-1], "pid=,ppid=,lstart=,comm=")
        for forbidden in ("args", "command", "cmd"):
            self.assertNotIn(forbidden, " ".join(PS_COMMAND))


class AppNameTest(TestCase):
    def test_basename_matches_case_insensitively(self) -> None:
        self.assertEqual(app_for("/Applications/Ghostty.app/Contents/MacOS/ghostty"), "Ghostty")
        self.assertEqual(app_for("GHOSTTY"), "Ghostty")
        self.assertEqual(app_for("wezterm-gui"), "WezTerm")
        self.assertEqual(app_for("/usr/bin/kitty"), "kitty")

    def test_desktop_apps_match_exactly(self) -> None:
        self.assertEqual(app_for("/Applications/Claude.app/Contents/MacOS/Claude"), "Claude Desktop")
        self.assertEqual(app_for("Codex"), "Codex Desktop")
        self.assertIsNone(app_for("claude"))
        self.assertIsNone(app_for("codex"))

    def test_unfamiliar_names_are_not_guessed(self) -> None:
        self.assertIsNone(app_for("zsh"))
        self.assertIsNone(app_for("node"))
        self.assertIsNone(app_for(""))


class AncestryTest(TestCase):
    def setUp(self) -> None:
        reset_caches()
        self.table = parse_ps(PS)

    def test_ghostty_chain(self) -> None:
        self.assertEqual(walk_ancestry(500, self.table), "Ghostty")

    def test_herdr_chain(self) -> None:
        self.assertEqual(walk_ancestry(620, self.table), "Herdr")

    def test_orphaned_chain_finds_nothing(self) -> None:
        self.assertIsNone(walk_ancestry(710, self.table))

    def test_dead_pid_finds_nothing(self) -> None:
        self.assertIsNone(walk_ancestry(999, self.table))
        self.assertIsNone(ancestry_surface(999, self.table))

    def test_tmux_is_reported_as_far_as_the_walk_goes(self) -> None:
        self.assertEqual(walk_ancestry(820, self.table), "tmux")

    def test_the_sessions_own_process_is_skipped(self) -> None:
        # 960 is a claude under a claude wrapper under Ghostty: the wrapper is
        # not the desktop app, and the walk reaches the terminal.
        self.assertEqual(walk_ancestry(960, self.table), "Ghostty")
        self.assertEqual(walk_ancestry(910, self.table), "Claude Desktop")

    def test_vs_code_integrated_terminal(self) -> None:
        self.assertEqual(walk_ancestry(1030, self.table), "VS Code")

    def test_a_cycle_terminates(self) -> None:
        table = parse_ps(
            "  5  6 Sat Sep  6 09:00:00 2026 zsh\n  6  5 Sat Sep  6 09:00:00 2026 zsh\n"
        )
        self.assertIsNone(walk_ancestry(5, table))

    def test_answers_are_remembered_until_the_process_dies(self) -> None:
        self.assertEqual(ancestry_surface(500, self.table), "Ghostty")
        without = {pid: info for pid, info in self.table.items() if pid != 500}
        self.assertIsNone(ancestry_surface(500, without))
        reused = parse_ps(restarted(PS, 500, 610, "claude", "Sat Sep  6 12:00:00 2026"))
        self.assertEqual(ancestry_surface(500, reused), "Herdr")

    def test_the_same_process_keeps_its_answer(self) -> None:
        self.assertEqual(ancestry_surface(500, self.table), "Ghostty")
        # Same pid, same start time: the same process, so the remembered
        # answer stands even though this table would walk elsewhere.
        moved = parse_ps(restarted(PS, 500, 610, "claude", "Sat Sep  6 09:00:05 2026"))
        self.assertEqual(ancestry_surface(500, moved), "Ghostty")

    def test_a_recycled_pid_is_resolved_afresh(self) -> None:
        self.assertEqual(ancestry_surface(500, self.table), "Ghostty")
        # The pid never left the table between polls, but its start time
        # moved: the kernel handed it to a new process under Herdr.
        recycled = parse_ps(restarted(PS, 500, 610, "claude", "Sat Sep  6 12:00:00 2026"))
        self.assertEqual(ancestry_surface(500, recycled), "Herdr")

    def test_one_ps_snapshot_serves_a_poll(self) -> None:
        with probe(PS) as calls:
            self.assertEqual(resolve_surface("terminal", pid=500), "Ghostty")
            self.assertEqual(resolve_surface("terminal", pid=620), "Herdr")
            self.assertEqual([call[0] for call in calls], ["ps"])

    def test_bad_pids_are_refused(self) -> None:
        self.assertIsNone(ancestry_surface(0, self.table))
        self.assertIsNone(ancestry_surface(True, self.table))  # type: ignore[arg-type]


class ResolveTest(TestCase):
    def test_terminal_is_refined_and_kept_when_nothing_is_recognised(self) -> None:
        with probe(PS):
            self.assertEqual(resolve_surface("terminal", pid=500), "Ghostty")
            self.assertEqual(resolve_surface("terminal", pid=710), "terminal")
            self.assertEqual(resolve_surface("terminal", pid=999), "terminal")

    def test_nothing_known_is_unknown(self) -> None:
        with probe(PS):
            self.assertEqual(resolve_surface("", pid=710), "unknown")
            self.assertEqual(resolve_surface("", pid=None), "unknown")
            self.assertEqual(resolve_surface("unknown", pid=710), "unknown")
            self.assertEqual(resolve_surface("", pid=500), "Ghostty")

    def test_ambiguity_is_unknown_whatever_the_loader_said(self) -> None:
        with probe(PS) as calls:
            self.assertEqual(resolve_surface("terminal", pid=500, ambiguous=True), "unknown")
            self.assertEqual(resolve_surface("", ambiguous=True), "unknown")
            self.assertEqual(calls, [])

    def test_an_app_the_loader_named_is_not_probed(self) -> None:
        with probe(PS) as calls:
            self.assertEqual(resolve_surface("Claude Desktop", pid=500), "Claude Desktop")
            self.assertEqual(resolve_surface("Codex Desktop", ambiguous=True), "Codex Desktop")
            self.assertEqual(calls, [])

    def test_a_failing_tool_keeps_the_loaders_value(self) -> None:
        reset_caches()
        with patch.object(surfaces, "_run", side_effect=OSError("no ps")):
            self.assertEqual(resolve_surface("terminal", pid=500), "terminal")
        reset_caches()
        with patch.object(surfaces.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(resolve_surface("terminal", pid=500), "terminal")
        reset_caches()


class CodexProcessTest(TestCase):
    def test_an_open_rollout_file_wins(self) -> None:
        both = {500: "/work/side-dog", 620: "/work/side-dog"}
        with probe(PS_CODEX, holders={620: ["/dev/null", ROLLOUT]}, cwds=both):
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog").pid, 620)
            self.assertEqual(codex_surface("terminal", ROLLOUT, "/work/side-dog"), "Herdr")

    def test_two_holders_resolve_to_unknown(self) -> None:
        # Two processes with the file open: nothing says which one is the
        # session, so neither is picked.
        holders = {500: [ROLLOUT], 620: [ROLLOUT]}
        with probe(PS_CODEX, holders=holders, cwds={500: "/work/side-dog"}):
            resolution = resolve_one(ROLLOUT, "/work/side-dog")
            self.assertEqual((resolution.pid, resolution.ambiguous), (None, True))
            self.assertEqual(resolution.candidates, (500, 620))
            self.assertEqual(codex_surface("terminal", ROLLOUT, "/work/side-dog"), "unknown")

    def test_a_unique_cwd_match_is_accepted(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/other", 710: "/work/side-dog"}
        with probe(PS_CODEX, cwds=cwds):
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog").pid, 500)
            self.assertEqual(codex_surface("terminal", ROLLOUT, "/work/side-dog"), "Ghostty")

    def test_two_cwd_matches_resolve_to_unknown(self) -> None:
        both = {500: "/work/side-dog", 620: "/work/side-dog"}
        with probe(PS_CODEX, cwds=both):
            resolution = resolve_one(ROLLOUT, "/work/side-dog")
            self.assertEqual(resolution.candidates, (500, 620))
            self.assertTrue(resolution.ambiguous)
            self.assertEqual(codex_surface("terminal", ROLLOUT, "/work/side-dog"), "unknown")

    def test_two_recent_rollouts_in_one_cwd_make_the_fallback_ambiguous(self) -> None:
        # x1 ended minutes ago and is no longer held open; x2 is the new
        # session in the same folder. One codex process is there. Crediting
        # it to both rows would put the new window on the old session.
        cwds = {500: "/work/side-dog", 620: "/work/other"}
        with probe(PS_CODEX, cwds=cwds):
            results = resolve_codex_sessions(
                [CodexRequest(ROLLOUT, "/work/side-dog"), CodexRequest(ROLLOUT_2, "/work/side-dog")]
            )
            for path in (ROLLOUT, ROLLOUT_2):
                self.assertIsNone(results[path].pid)
                self.assertTrue(results[path].ambiguous)
                self.assertEqual(results[path].candidates, (500,))
            self.assertEqual(
                resolve_surface("terminal", pid=results[ROLLOUT].pid, ambiguous=results[ROLLOUT].ambiguous),
                "unknown",
            )

    def test_a_single_rollout_in_a_cwd_still_resolves_beside_others(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/other"}
        with probe(PS_CODEX, cwds=cwds):
            results = resolve_codex_sessions(
                [CodexRequest(ROLLOUT, "/work/side-dog"), CodexRequest(ROLLOUT_2, "/work/other")]
            )
            self.assertEqual(results[ROLLOUT].pid, 500)
            self.assertEqual(results[ROLLOUT_2].pid, 620)

    def test_an_open_file_settles_a_rollout_even_in_a_shared_cwd(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/side-dog"}
        with probe(PS_CODEX, holders={620: [ROLLOUT_2]}, cwds=cwds):
            results = resolve_codex_sessions(
                [CodexRequest(ROLLOUT, "/work/side-dog"), CodexRequest(ROLLOUT_2, "/work/side-dog")]
            )
            self.assertEqual(results[ROLLOUT_2].pid, 620)
            self.assertTrue(results[ROLLOUT].ambiguous)

    def test_no_candidate_keeps_the_loaders_value(self) -> None:
        with probe(PS_CODEX, cwds={500: "/elsewhere"}):
            resolution = resolve_one(ROLLOUT, "/work/side-dog")
            self.assertEqual(resolution, CodexResolution())
            self.assertEqual(codex_surface("terminal", ROLLOUT, "/work/side-dog"), "terminal")
            self.assertEqual(codex_surface("", ROLLOUT, ""), "unknown")

    def test_macos_asks_lsof_about_codex_processes_and_rollouts_only(self) -> None:
        with probe(PS_CODEX, darwin=True, holders={620: [ROLLOUT]}) as calls:
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog").pid, 620)
            self.assertIn(("lsof", "-a", "-p", "500,620", "-Fpn", ROLLOUT), calls)

    def test_no_codex_process_means_nothing_is_asked(self) -> None:
        with probe(NO_CODEX, darwin=True) as calls:
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog"), CodexResolution())
            self.assertEqual([call[0] for call in calls], ["ps"])

    def test_macos_uses_lsof_for_working_directories(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/other"}
        with probe(PS_CODEX, darwin=True, cwds=cwds) as calls:
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog").pid, 500)
            self.assertIn(("lsof", "-a", "-d", "cwd", "-p", "500,620", "-Fpn"), calls)

    def test_many_rollouts_cost_one_holders_probe_and_one_cwd_probe(self) -> None:
        requests = [
            CodexRequest(ROLLOUT, "/work/a"),
            CodexRequest(ROLLOUT_2, "/work/b"),
            CodexRequest(ROLLOUT_3, "/work/c"),
        ]
        cwds = {500: "/work/a", 620: "/work/b"}
        with probe(PS_CODEX, darwin=True, cwds=cwds) as calls:
            results = resolve_codex_sessions(requests)
            self.assertEqual(results[ROLLOUT].pid, 500)
            self.assertEqual(results[ROLLOUT_2].pid, 620)
            self.assertEqual(results[ROLLOUT_3], CodexResolution())
            lsof = [call for call in calls if call[0] == "lsof"]
            self.assertEqual(len(lsof), 2)
            holders_probe = [call for call in lsof if call[2] == "-p"]
            self.assertEqual(len(holders_probe), 1)
            self.assertEqual(set(holders_probe[0][5:]), {ROLLOUT, ROLLOUT_2, ROLLOUT_3})
        with probe(PS_CODEX, darwin=False, cwds=cwds) as calls:
            resolve_codex_sessions(requests)
            fd_reads = [call for call in calls if call[0] == "proc-fd"]
            self.assertEqual(sorted(fd_reads), [("proc-fd", "500"), ("proc-fd", "620")])
            links = [call for call in calls if call[0] == "proc-link"]
            self.assertEqual(sorted(links), [("proc-link", "500", "cwd"), ("proc-link", "620", "cwd")])

    def test_a_second_call_on_the_same_snapshot_asks_nothing_new(self) -> None:
        table = parse_ps(PS_CODEX)
        with probe(PS_CODEX, darwin=True, cwds={500: "/work/a"}) as calls:
            resolve_codex_sessions([CodexRequest(ROLLOUT, "/work/zzz")], table)
            asked = len(calls)
            # Same table object, a fresh rollout: the cwd probe is reused and
            # only the holders probe for the new path is made.
            resolve_codex_sessions([CodexRequest(ROLLOUT_2, "/work/zzz")], table)
            new = calls[asked:]
            self.assertEqual([call[:3] for call in new], [("lsof", "-a", "-p")])

    def test_a_settled_process_is_remembered_until_it_dies(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/other"}
        with probe(PS_CODEX, cwds=cwds) as calls:
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog").pid, 500)
            calls.clear()
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog").pid, 500)
            self.assertEqual(calls, [])
            gone = {pid: info for pid, info in parse_ps(PS_CODEX).items() if pid != 500}
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog", table=gone), CodexResolution())

    def test_a_settled_process_is_forgotten_when_its_pid_is_recycled(self) -> None:
        table = parse_ps(PS_CODEX)
        with probe(PS_CODEX, cwds={500: "/work/side-dog", 620: "/work/other"}):
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog", table=table).pid, 500)
        # Pid 500 is still a codex in the table, but a newer one: the old
        # answer is dropped and the new process, now working elsewhere, is
        # not this rollout's.
        recycled = parse_ps(restarted(PS_CODEX, 500, 420, "codex", "Sat Sep  6 12:00:00 2026"))
        with probe(PS_CODEX, cwds={500: "/work/other", 620: "/work/other"}):
            surfaces._CODEX_PIDS[ROLLOUT] = (500, table[500].start)
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog", table=recycled), CodexResolution())

    def test_an_unsettled_answer_is_not_asked_again_every_poll(self) -> None:
        both = {500: "/work/side-dog", 620: "/work/side-dog"}
        table = parse_ps(PS_CODEX)
        with probe(PS_CODEX, darwin=True, cwds=both) as calls:
            first = resolve_one(ROLLOUT, "/work/side-dog", table=table, now=0.0)
            self.assertTrue(first.ambiguous)
            asked = len(calls)
            self.assertGreater(asked, 0)
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog", table=table, now=2.0), first)
            self.assertEqual(len(calls), asked)
            surfaces._SNAPSHOT = None  # a later poll would bring a new table
            self.assertEqual(resolve_one(ROLLOUT, "/work/side-dog", table=table, now=31.0), first)
            self.assertGreater(len(calls), asked)


class ProcPrivacyTest(TestCase):
    def test_descriptor_names_are_never_read(self) -> None:
        with patch.object(surfaces.os, "readlink", side_effect=AssertionError("readlink")):
            surfaces._proc_fd_identities(os.getpid())

    @skipUnless(os.path.isdir("/proc"), "needs procfs")
    def test_open_files_are_found_by_identity(self) -> None:
        with tempfile.NamedTemporaryFile() as handle:
            identity = surfaces._file_identity(handle.name)
            self.assertIsNotNone(identity)
            with patch.object(surfaces.os, "readlink", side_effect=AssertionError("readlink")):
                held = surfaces._proc_fd_identities(os.getpid())
            self.assertIn(identity, held)
        self.assertNotIn(identity, surfaces._proc_fd_identities(os.getpid()))

    def test_a_missing_file_has_no_identity(self) -> None:
        self.assertIsNone(surfaces._file_identity("/nonexistent/rollout.jsonl"))
        self.assertEqual(surfaces._proc_fd_identities(2**22 + 12345), frozenset())
