"""Keep public recordings synthetic and tied to the production views."""
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts.terminal_demo import board_frame, watch_frame

ROOT = Path(__file__).resolve().parents[1]


class TerminalRecordingsTest(TestCase):
    def test_readme_has_exactly_watch_and_board_gifs(self):
        readme = (ROOT / "README.md").read_text()
        gifs = re.findall(r"https://[^)\s]+\.gif", readme)
        self.assertEqual([url.rsplit("/", 1)[1] for url in gifs],
                         ["side-dog-watch.gif", "side-dog-board.gif"])
        for url in gifs:
            self.assertTrue((ROOT / "docs" / url.rsplit("/", 1)[1]).is_file())
        self.assertNotIn("Until the next release", readme)

    def test_board_animation_stays_on_roster(self):
        for stage in range(5):
            with self.subTest(stage=stage), patch("side_dog.cli.discovered_watch_roots") as discovery:
                frame = board_frame(stage)
            discovery.assert_not_called()
            for text in ("AGENT", "SURFACE", "REPO / BRANCH", "ISSUE", "STATUS", "Live session roster"):
                self.assertIn(text, frame)
        self.assertNotEqual(board_frame(0), board_frame(4))

    def test_watch_accumulates_synthetic_activity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("side_dog.cli.discovered_watch_roots") as discovery:
                first, last = watch_frame(0, root), watch_frame(4, root)
            discovery.assert_not_called()
            self.assertIn("/demo/atlas", first)
            self.assertNotIn(directory, first + last)
            self.assertIn("Codex", last)
            self.assertIn("PR #47", last)
            self.assertNotEqual(first, last)
