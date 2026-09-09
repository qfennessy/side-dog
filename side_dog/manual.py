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


def _parser_reference(parser: argparse.ArgumentParser) -> str:
    """Render parser metadata without version-specific argparse line wrapping."""
    lines = [parser.prog + " [options]", parser.description or "", ""]
    for action in parser._actions:
        if action.help == argparse.SUPPRESS:
            continue
        if isinstance(action, argparse._SubParsersAction):
            lines.append("COMMANDS")
            lines.extend(
                f"{choice.dest}: {choice.help}" for choice in action._choices_actions
            )
            continue
        label = ", ".join(action.option_strings) or str(action.metavar or action.dest)
        if action.nargs != 0:
            value = (
                "{" + ",".join(str(choice) for choice in action.choices) + "}"
                if action.choices is not None
                else str(action.metavar or action.dest.upper())
            )
            if action.nargs == "?":
                value = "[" + value + "]"
            elif action.nargs == "*":
                value = "[" + value + " ...]"
            elif action.nargs == "+":
                value += " ..."
            label += " " + value if action.option_strings else " (" + value + ")"
        lines.extend([label, "  " + str(action.help or ""), ""])
    return "\n".join(
        textwrap.fill(line, width=76, subsequent_indent="  ") if line else ""
        for line in lines
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
        pages[name + ".1"] = (
            f'.TH "{name.upper()}" "1" "September 8, 2026" "Side Dog" "User Commands"\n'
            ".SH NAME\n" + _roff(name + " - coding-agent activity viewer") + "\n"
            ".SH SYNOPSIS AND OPTIONS\n.nf\n"
            + _roff(_parser_reference(child))
            + "\n.fi\n"
            ".SH FIRST RUN\n.nf\n"
            + _roff(
                "side-dog --version\nside-dog demo --watch\nside-dog doctor .\nside-dog watch ."
            )
            + "\n.fi\n"
            ".SH VIEWS\n"
            "Watch is chronological. Press b for the live Board roster and w to return.\n"
            "Board lists live sessions by repository, issue/PR, model and event-time activity.\n"
            "Use j/k to select a session and o to open its linked pull request.\n"
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
