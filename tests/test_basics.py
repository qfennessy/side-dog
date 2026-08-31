from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

from side_dog.cli import classify_commands, render


def activity(epoch_ms: int, detail: str) -> dict[str, object]:
    return {
        "epoch_ms": epoch_ms,
        "timestamp": datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).isoformat(),
        "kind": "file",
        "title": "File changed",
        "detail": detail,
        "agent": "filesystem",
        "status": "success",
    }


class SideDogBasicsTest(TestCase):
    def test_header_identifies_the_watched_folder(self) -> None:
        root = Path.home() / "src" / "side-dog"

        screen = render([], root, width=80, height=12, color=False)

        self.assertIn("Watching ~/src/side-dog", screen)
        self.assertIn("waiting for coding-agent activity", screen)

    def test_common_delivery_commands_are_recognized(self) -> None:
        self.assertEqual(
            classify_commands("python -m unittest"),
            [("test", "Running tests", "unittest")],
        )
        self.assertEqual(
            classify_commands("git commit -m 'test'"),
            [("commit", "Creating commit", "git commit")],
        )

    def test_timeline_order_can_be_reversed(self) -> None:
        records = [activity(1_000, "older.py"), activity(2_000, "newer.py")]

        newest_first = render(
            records,
            Path("/tmp/project"),
            width=80,
            height=16,
            color=False,
            expanded_history=True,
        )
        oldest_first = render(
            records,
            Path("/tmp/project"),
            width=80,
            height=16,
            color=False,
            expanded_history=True,
            newest_first=False,
        )

        self.assertLess(newest_first.index("newer.py"), newest_first.index("older.py"))
        self.assertLess(oldest_first.index("older.py"), oldest_first.index("newer.py"))

    def test_rendering_does_not_modify_raw_events(self) -> None:
        records = [activity(1_000, "app.py")]
        original = deepcopy(records)

        render(
            records,
            Path("/tmp/project"),
            width=80,
            height=12,
            color=False,
            newest_first=False,
        )

        self.assertEqual(records, original)
