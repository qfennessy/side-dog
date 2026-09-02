from __future__ import annotations

import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from side_dog.cli import main


class SetupTests(unittest.TestCase):
    def run_setup(self, root: Path, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["setup", os.fspath(root), *arguments])
        return code, output.getvalue()

    def test_codex_only_setup_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with patch("side_dog.cli.shutil.which", return_value=None):
                code, output = self.run_setup(
                    root, "--no-claude", "--no-herdr"
                )

            self.assertEqual(code, 0)
            self.assertFalse((root / ".claude").exists())
            self.assertIn("Codex: ready without hooks", output)
            self.assertIn("Antigravity: ready without hooks", output)
            self.assertIn("no project files were changed", output)
            self.assertIn(f"side-dog watch {root}", output)
            self.assertIn(f"side-dog doctor {root}", output)

    def test_claude_setup_previews_then_writes_and_preserves_other_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = root / ".claude" / "settings.local.json"
            settings.parent.mkdir()
            settings.write_text(
                json.dumps(
                    {
                        "permissions": {"allow": ["Bash(git status)"]},
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "other-tool notify",
                                        }
                                    ]
                                }
                            ]
                        },
                    }
                )
            )

            with patch("side_dog.cli.shutil.which", return_value=None):
                code, output = self.run_setup(
                    root, "--claude", "--no-herdr"
                )

            self.assertEqual(code, 0)
            self.assertLess(
                output.index("preview of"), output.index("installed project-local")
            )
            self.assertIn("restart Claude Code", output)
            document = json.loads(settings.read_text())
            self.assertEqual(document["permissions"], {"allow": ["Bash(git status)"]})
            commands = [
                hook["command"]
                for entry in document["hooks"]["Stop"]
                for hook in entry["hooks"]
            ]
            self.assertIn("other-tool notify", commands)
            self.assertTrue(any("side-dog" in command for command in commands))

            first = settings.read_text()
            with patch("side_dog.cli.shutil.which", return_value=None):
                second_code, _second_output = self.run_setup(
                    root, "--claude", "--no-herdr"
                )
            self.assertEqual(second_code, 0)
            self.assertEqual(settings.read_text(), first)

    def test_herdr_flow_checks_health_and_prints_herdr_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch("side_dog.cli.shutil.which", return_value="/usr/bin/herdr"),
                patch(
                    "side_dog.cli.read_herdr_snapshot",
                    return_value=({"agents": []}, None),
                ) as health,
            ):
                code, output = self.run_setup(root, "--no-claude", "--herdr")

            self.assertEqual(code, 0)
            health.assert_called_once_with()
            self.assertIn("Herdr: selected and ready", output)
            self.assertIn(f"side-dog watch {root} --herdr", output)
            self.assertIn(f"side-dog panel {root} --herdr", output)

    def test_no_herdr_flow_never_probes_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch("side_dog.cli.shutil.which", return_value="/usr/bin/tool"),
                patch("side_dog.cli.read_herdr_snapshot") as health,
            ):
                code, output = self.run_setup(root, "--no-claude", "--no-herdr")

            self.assertEqual(code, 0)
            health.assert_not_called()
            self.assertIn("Herdr: detected but not selected", output)
            self.assertNotIn("--herdr", output)

    def test_failed_herdr_health_falls_back_to_project_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch("side_dog.cli.shutil.which", return_value="/usr/bin/herdr"),
                patch(
                    "side_dog.cli.read_herdr_snapshot",
                    return_value=({}, "Herdr service is unavailable"),
                ),
            ):
                code, output = self.run_setup(root, "--no-claude", "--herdr")

            self.assertEqual(code, 0)
            self.assertIn("health check failed", output)
            self.assertIn("without Herdr", output)
            self.assertNotIn("--herdr", output)

    def test_noninteractive_defaults_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with (
                patch("side_dog.cli.shutil.which", return_value="/usr/bin/tool"),
                patch("side_dog.cli.sys.stdin.isatty", return_value=False),
            ):
                code, output = self.run_setup(root)

            self.assertEqual(code, 0)
            self.assertFalse((root / ".claude").exists())
            self.assertIn("hooks were skipped", output)
            self.assertIn("detected but not selected", output)

    def test_recommended_commands_shell_quote_the_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="side dog ") as directory:
            root = Path(directory).resolve()
            with patch("side_dog.cli.shutil.which", return_value=None):
                code, output = self.run_setup(
                    root, "--no-claude", "--no-herdr"
                )

            self.assertEqual(code, 0)
            quoted = shlex.quote(os.fspath(root))
            self.assertIn(f"side-dog watch {quoted}", output)
            self.assertIn(f"side-dog panel {quoted}", output)
            self.assertIn(f"side-dog doctor {quoted}", output)

    def test_detected_integrations_are_offered_interactively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            detected = {
                "claude": "/usr/bin/claude",
                "herdr": "/usr/bin/herdr",
            }
            with (
                patch("side_dog.cli.shutil.which", side_effect=detected.get),
                patch("side_dog.cli.sys.stdin.isatty", return_value=True),
                patch("builtins.input", side_effect=["yes", "yes"]) as prompt,
                patch(
                    "side_dog.cli.read_herdr_snapshot",
                    return_value=({"agents": []}, None),
                ),
            ):
                code, output = self.run_setup(root)

            self.assertEqual(code, 0)
            self.assertEqual(prompt.call_count, 2)
            self.assertTrue((root / ".claude" / "settings.local.json").is_file())
            self.assertIn("Herdr: selected and ready", output)

    def test_claude_session_registry_is_detected_without_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            (home / ".claude" / "sessions").mkdir(parents=True)
            project = root / "project"
            project.mkdir()
            with (
                patch("side_dog.cli.shutil.which", return_value=None),
                patch("side_dog.cli.Path.home", return_value=home),
                patch("side_dog.cli.sys.stdin.isatty", return_value=True),
                patch("builtins.input", return_value="yes") as prompt,
            ):
                code, output = self.run_setup(project)

            self.assertEqual(code, 0)
            prompt.assert_called_once()
            self.assertTrue(
                (project / ".claude" / "settings.local.json").is_file()
            )
            self.assertIn("restart Claude Code", output)

    def test_init_remains_the_direct_preview_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["init", os.fspath(root), "--print"])

            self.assertEqual(code, 0)
            self.assertFalse((root / ".claude").exists())
            self.assertIn('"hooks"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
