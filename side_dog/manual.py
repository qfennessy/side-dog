"""Deterministic offline manuals generated from the installed CLI parser."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import textwrap


def _roff(text: str) -> str:
    return "\n".join(
        "\\&" + line.replace("\\", "\\e").replace("-", "\\-")
        for line in text.splitlines()
    )


def manual_pages(parser: argparse.ArgumentParser) -> dict[str, str]:
    parsers = {"side-dog": parser}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            parsers.update(
                {f"side-dog-{name}": child for name, child in action.choices.items()}
            )
    pages = {}
    for name, child in parsers.items():
        child.formatter_class = lambda prog: argparse.HelpFormatter(prog, width=80)
        pages[name + ".1"] = (
            f'.TH "{name.upper()}" "1" "September 8, 2026" "Side Dog" "User Commands"\n'
            ".SH NAME\n" + _roff(name + " - coding-agent activity viewer") + "\n"
            ".SH SYNOPSIS AND OPTIONS\n.nf\n" + _roff(child.format_help()) + "\n.fi\n"
            ".SH FIRST RUN\n.nf\n"
            + _roff(
                "side-dog --version\nside-dog demo --watch\nside-dog doctor .\nside-dog watch ."
            )
            + "\n.fi\n"
            ".SH VIEWS\n"
            "Watch is chronological. Press b for Board contributions, a for its live roster, and w to return.\n"
            "Board --activity groups recorded work by repository, issue/PR, session and event-time model over the last 24 hours.\n"
            "Use j/k to select work and o to open its link. Completed contributors remain in recorded history.\n"
            "Unknown attribution is explicit. GitHub polling is an observation, never model effort.\n"
            "Only unambiguous recorded turn references link work; ambiguous actions remain unlinked.\n"
            ".SH FILES\n"
            "~/.config/side-dog/config.toml: hand-authored configuration.\n.br\n"
            "~/.config/side-dog/spaces.toml: saved spaces, rewritten by watch --save.\n.br\n"
            "~/.local/state/side-dog/: disposable validated activity and display state.\n"
            ".SH ENVIRONMENT\n"
            "XDG_CONFIG_HOME overrides the configuration parent. SIDE_DOG_STATE_DIR overrides runtime state.\n"
            ".SH PRIVACY\n"
            "Only validated metadata is persisted or exposed; never prompts, responses, full commands, output, diffs or file contents.\n"
            "Usage estimates are not allocated to PRs.\n"
            ".SH SEE ALSO\n"
            "side-dog(1), side-dog-watch(1), side-dog-board(1), side-dog-doctor(1)\n.br\n"
            "https://qfennessy.github.io/side-dog/\n"
        )
    for name, text in pages.items():
        pages[name] = (
            "\n".join(
                line if line.startswith((".", "\\")) else textwrap.fill(line, width=78)
                for line in text.splitlines()
            )
            + "\n"
        )
    return pages


def show_manual(command: str | None, path_only: bool = False) -> int:
    path = Path(__file__).with_name("man") / (
        "side-dog" + ("-" + command if command else "") + ".1"
    )
    if path_only:
        print(path)
        return 0
    if shutil.which("man"):
        return subprocess.call(["man", str(path)])
    print(
        "No man viewer found. Use side-dog help"
        + (" " + command if command else "")
        + ", or install man/man-db."
    )
    return 1
