"""Render privacy-safe documentation recordings with the production renderers.

No agent discovery, GitHub requests, local history, or private sessions are read.
Run from the repository root: python scripts/terminal_demo.py watch|board.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from side_dog import __version__
from side_dog.board import BoardRow, LinkedIssue, board_summary, render_board
from side_dog.cli import board_hints, demo_tour_samples, render, status_bar, style_status_bar
from side_dog.integrations import AgentStatus
from side_dog.privacy import safe_event

WIDTH, HEIGHT = 120, 18
START = datetime(2026, 9, 8, 14, 32, tzinfo=timezone.utc)
ROOT = Path("/demo/atlas")


def watch_frame(stage: int, root: Path = ROOT) -> str:
    identity = dict(agent="codex", session_id="demo-codex", root=str(root),
                    working_root=str(root), model="gpt-5.6-luna", effort="high",
                    status="working" if stage < 4 else "completed", label="Fix search results")
    samples = [event for root_index, event in demo_tour_samples() if root_index == 0]
    records = []
    for index, sample in enumerate(samples[:stage + 2]):
        instant = START + timedelta(seconds=index * 12)
        records.append(safe_event(root, dict(
            **{**sample, "agent": "codex", "session_id": "demo-codex",
               "model": "gpt-5.6-luna", "effort": "high"},
            timestamp=instant.isoformat(), epoch_ms=int(instant.timestamp() * 1000),
        )).to_wire())
    with (
        patch("side_dog.cli.time.time", return_value=(START + timedelta(seconds=90)).timestamp()),
        patch("side_dog.cli.display_root", return_value="/demo/atlas"),
    ):
        return render(records, root, WIDTH, HEIGHT, True,
                      identities={"demo-codex": identity}, expanded_header=True,
                      expanded_history=True,
                      git_status={"branch": "fix/search"},
                      show_filesystem_activity=True)


def board_frame(stage: int) -> str:
    rows = []
    examples = [
        ("codex", "Codex Desktop", "atlas", "fix/search", "gpt-5.6-luna/high", AgentStatus.WORKING),
        ("claude-code", "Ghostty", "atlas", "docs/setup", "opus-5/xhigh", AgentStatus.WORKING),
        ("pi", "Terminal", "beacon", "fix/cache", "demo-model/high", AgentStatus.BLOCKED),
        ("opencode", "Terminal", "beacon", "test/api", "demo-model", AgentStatus.DONE),
    ]
    for index, (agent, surface, repo, branch, model, status) in enumerate(examples):
        rows.append(BoardRow(
            key=str(index), agent=agent, surface=surface, repository=repo, branch=branch,
            root=f"/demo/{repo}", working_root=f"/demo/{repo}/{branch}",
            status=status, age_seconds=stage * 3 + index * 18, model=model,
            issues=(LinkedIssue(f"github.com/demo/{repo}", 31 + index, True),),
            github={"number": 42 + index, "state": "OPEN", "draft": False,
                    "checks_total": 3, "checks_passed": 3 if stage > 1 else 1,
                    "checks_pending": 0 if stage > 1 else 2, "checks_failed": 0,
                    "review": "APPROVED" if stage > 2 else "REVIEW_REQUIRED"},
        ))
    if stage > 2:
        rows[0] = replace(rows[0], status=AgentStatus.DONE)
    clock = f"14:32:{stage * 3:02}"
    masthead = style_status_bar(status_bar(__version__, board_summary(rows),
        sum(row.status == AgentStatus.WORKING for row in rows), WIDTH, clock), True)
    return render_board(rows, WIDTH, HEIGHT, True, clock=clock,
                        selected=str(stage % len(rows)), hints=board_hints(False),
                        masthead=masthead,
                        detail=["Synthetic sessions only · Roster view", "Select a session to inspect its recent activity."],
                        detail_heading="Live session roster")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("view", choices=("watch", "board"))
    args = parser.parse_args()
    with TemporaryDirectory(prefix="side-dog-recording-") as directory:
        root = Path(directory) / "atlas"
        root.mkdir()
        try:
            sys.stdout.write("\x1b[?25l")
            for stage in range(5):
                screen = watch_frame(stage, root) if args.view == "watch" else board_frame(stage)
                sys.stdout.write("\x1b[H\x1b[2J" + screen)
                sys.stdout.flush()
                time.sleep(2)
        finally:
            sys.stdout.write("\x1b[?25h")


if __name__ == "__main__":
    main()
