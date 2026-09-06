from contextlib import contextmanager
from typing import Iterator
from unittest import TestCase
from unittest.mock import patch

from side_dog import surfaces
from side_dog.surfaces import (
    PS_COMMAND,
    ancestry_surface,
    app_for,
    codex_session_pids,
    parse_ps,
    reset_caches,
    resolve_surface,
    walk_ancestry,
)

ROLLOUT = "/Users/q/.codex/sessions/2026/09/06/rollout-x1.jsonl"

# A macOS process table as ``ps -eo pid=,ppid=,comm=`` prints it: full paths
# for app bundles, login shells with a leading dash, and no arguments anywhere.
PS = """\
    1     0 /sbin/launchd
  400     1 /Applications/Ghostty.app/Contents/MacOS/ghostty
  410   400 login
  420   410 -zsh
  500   420 claude
  600     1 /Applications/Herdr.app/Contents/MacOS/Herdr
  610   600 zsh
  620   610 codex
  700     1 zsh
  710   700 claude
  800     1 tmux
  810   800 -zsh
  820   810 codex
  900     1 /Applications/Claude.app/Contents/MacOS/Claude
  910   900 claude
  950   420 claude
  960   950 claude
 1000     1 /Applications/Visual Studio Code.app/Contents/MacOS/Electron
 1010  1000 /Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin)
 1020  1010 zsh
 1030  1020 claude
"""

# Two Codex sessions in two terminals, the case the tie-to-process step is for.
PS_CODEX = """\
    1     0 /sbin/launchd
  400     1 /Applications/Ghostty.app/Contents/MacOS/ghostty
  420   400 zsh
  500   420 codex
  600     1 /Applications/Herdr.app/Contents/MacOS/Herdr
  610   600 zsh
  620   610 codex
  700     1 zsh
  710   700 python3
"""


@contextmanager
def probe(
    ps: str,
    *,
    darwin: bool = False,
    holders: dict[int, list[str]] | None = None,
    cwds: dict[int, str] | None = None,
    lsof_t: str = "",
) -> Iterator[list[tuple[str, ...]]]:
    """Stand in for ``ps``, ``lsof``, and ``/proc`` and record every call."""
    calls: list[tuple[str, ...]] = []
    holders = holders or {}
    cwds = cwds or {}

    def fake_run(args):
        calls.append(tuple(args))
        if args[0] == "ps":
            return ps
        if args[0] == "lsof" and args[1] == "-t":
            return lsof_t
        if args[0] == "lsof" and args[-1] == "-Fpn":
            pids = [int(part) for part in args[5].split(",")]
            return "".join(
                f"p{pid}\nn{cwds[pid]}\n" for pid in pids if pid in cwds
            )
        raise AssertionError(f"unexpected command {args!r}")

    reset_caches()
    try:
        with patch.object(surfaces, "DARWIN", darwin), patch.object(
            surfaces, "_run", side_effect=fake_run
        ), patch.object(
            surfaces, "_proc_fd_targets", side_effect=lambda pid: holders.get(pid, [])
        ), patch.object(
            surfaces, "_proc_link", side_effect=lambda pid, name: cwds.get(pid)
        ):
            yield calls
    finally:
        reset_caches()


class ParseTest(TestCase):
    def test_comm_keeps_paths_with_spaces(self) -> None:
        table = parse_ps(PS)
        self.assertEqual(table[1010].ppid, 1000)
        self.assertTrue(table[1010].comm.endswith("Code Helper (Plugin)"))
        self.assertEqual(table[420].comm, "-zsh")

    def test_malformed_lines_are_skipped(self) -> None:
        self.assertEqual(parse_ps("garbage\n  x  y z\n  5 1\n"), {})

    def test_ps_reads_names_and_parents_only(self) -> None:
        self.assertEqual(PS_COMMAND[-1], "pid=,ppid=,comm=")
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
        table = parse_ps("  5  6 zsh\n  6  5 zsh\n")
        self.assertIsNone(walk_ancestry(5, table))

    def test_answers_are_remembered_until_the_process_dies(self) -> None:
        self.assertEqual(ancestry_surface(500, self.table), "Ghostty")
        without = {pid: info for pid, info in self.table.items() if pid != 500}
        self.assertIsNone(ancestry_surface(500, without))
        reused = parse_ps(PS.replace("  500   420 claude", "  500   610 claude"))
        self.assertEqual(ancestry_surface(500, reused), "Herdr")

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

    def test_an_app_the_loader_named_is_not_probed(self) -> None:
        with probe(PS) as calls:
            self.assertEqual(resolve_surface("Claude Desktop", pid=500), "Claude Desktop")
            self.assertEqual(resolve_surface("Codex Desktop", rollout_path=ROLLOUT), "Codex Desktop")
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
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), (620,))
            self.assertEqual(
                resolve_surface("terminal", rollout_path=ROLLOUT, cwd="/work/side-dog"),
                "Herdr",
            )

    def test_a_unique_cwd_match_is_accepted(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/other", 710: "/work/side-dog"}
        with probe(PS_CODEX, cwds=cwds):
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), (500,))
            self.assertEqual(
                resolve_surface("terminal", rollout_path=ROLLOUT, cwd="/work/side-dog"),
                "Ghostty",
            )

    def test_two_cwd_matches_resolve_to_unknown(self) -> None:
        both = {500: "/work/side-dog", 620: "/work/side-dog"}
        with probe(PS_CODEX, cwds=both):
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), (500, 620))
            self.assertEqual(
                resolve_surface("terminal", rollout_path=ROLLOUT, cwd="/work/side-dog"),
                "unknown",
            )

    def test_no_candidate_keeps_the_loaders_value(self) -> None:
        with probe(PS_CODEX, cwds={500: "/elsewhere"}):
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), ())
            self.assertEqual(
                resolve_surface("terminal", rollout_path=ROLLOUT, cwd="/work/side-dog"),
                "terminal",
            )
            self.assertEqual(resolve_surface("", rollout_path=ROLLOUT, cwd=""), "unknown")

    def test_macos_asks_lsof_about_codex_processes_only(self) -> None:
        with probe(PS_CODEX, darwin=True, lsof_t="620\n") as calls:
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), (620,))
            self.assertIn(("lsof", "-t", "-a", "-p", "500,620", ROLLOUT), calls)

    def test_no_codex_process_means_nothing_is_asked(self) -> None:
        no_codex = "    1     0 /sbin/launchd\n  400     1 ghostty\n  420   400 zsh\n  500   420 claude\n"
        with probe(no_codex, darwin=True) as calls:
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), ())
            self.assertEqual([call[0] for call in calls], ["ps"])

    def test_an_unsettled_answer_is_not_asked_again_every_poll(self) -> None:
        both = {500: "/work/side-dog", 620: "/work/side-dog"}
        table = parse_ps(PS_CODEX)
        with probe(PS_CODEX, darwin=True, cwds=both) as calls:
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog", table, now=0.0), (500, 620))
            asked = len(calls)
            self.assertGreater(asked, 0)
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog", table, now=2.0), (500, 620))
            self.assertEqual(len(calls), asked)
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog", table, now=31.0), (500, 620))
            self.assertGreater(len(calls), asked)

    def test_macos_uses_lsof_for_working_directories(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/other"}
        with probe(PS_CODEX, darwin=True, cwds=cwds) as calls:
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), (500,))
            self.assertIn(("lsof", "-a", "-d", "cwd", "-p", "500,620", "-Fpn"), calls)

    def test_a_settled_process_is_remembered_until_it_dies(self) -> None:
        cwds = {500: "/work/side-dog", 620: "/work/other"}
        with probe(PS_CODEX, cwds=cwds) as calls:
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), (500,))
            calls.clear()
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog"), (500,))
            self.assertEqual(calls, [])
            gone = {pid: info for pid, info in parse_ps(PS_CODEX).items() if pid != 500}
            self.assertEqual(codex_session_pids(ROLLOUT, "/work/side-dog", gone), ())
