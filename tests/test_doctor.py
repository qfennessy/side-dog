from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from side_dog.doctor import Readiness, _claude_hooks_installed, doctor, github_probe


class DoctorTests(unittest.TestCase):
    def run_doctor(self, *checks: Readiness) -> tuple[int, str]:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("side_dog.doctor.git_probe", return_value=checks[0]),
                patch("side_dog.doctor.github_probe", return_value=checks[1]),
                patch("side_dog.doctor.codex_probe", return_value=checks[2]),
                patch("side_dog.doctor.claude_probe", return_value=checks[3]),
                patch("side_dog.doctor.herdr_probe", return_value=checks[4]),
            ):
                code = doctor(
                    directory, no_color=True, environment={}, output=output
                )
        return code, output.getvalue()

    def test_healthy_environment_is_ready(self) -> None:
        code, text = self.run_doctor(
            Readiness("Git project", "ok", "repository", True),
            Readiness("GitHub readback", "ok", "ready"),
            Readiness("Codex discovery", "ok", "ready"),
            Readiness("Claude discovery", "ok", "ready"),
            Readiness("Herdr", "ok", "ready"),
        )
        self.assertEqual(code, 0)
        self.assertIn("[OK] Git project: repository", text)
        self.assertIn("Recommended: side-dog watch", text)
        self.assertNotIn("\x1b[", text)

    def test_optional_partial_environment_remains_ready(self) -> None:
        code, text = self.run_doctor(
            Readiness("Git project", "ok", "linked worktree", True),
            Readiness("GitHub readback", "warn", "not authenticated"),
            Readiness("Codex discovery", "ok", "ready"),
            Readiness("Claude discovery", "warn", "hooks absent"),
            Readiness("Herdr", "warn", "snapshot unavailable"),
        )
        self.assertEqual(code, 0)
        self.assertIn("[WARN optional] GitHub readback: not authenticated", text)
        self.assertIn("[WARN optional] Claude discovery: hooks absent", text)
        self.assertIn("[WARN optional] Herdr: snapshot unavailable", text)

    def test_unavailable_optional_integrations_are_informational(self) -> None:
        code, text = self.run_doctor(
            Readiness("Git project", "ok", "repository", True),
            Readiness("GitHub readback", "info", "PR readback unavailable"),
            Readiness("Codex discovery", "info", "no sessions yet"),
            Readiness("Claude discovery", "info", "Claude unavailable"),
            Readiness("Herdr", "info", "Herdr unavailable"),
        )
        self.assertEqual(code, 0)
        self.assertIn("[INFO optional] GitHub readback: PR readback unavailable", text)
        self.assertIn("[INFO optional] Claude discovery: Claude unavailable", text)
        self.assertIn("[INFO optional] Herdr: Herdr unavailable", text)

    def test_required_git_failure_sets_nonzero_exit(self) -> None:
        code, text = self.run_doctor(
            Readiness("Git", "fail", "not installed", True),
            Readiness("GitHub readback", "info", "optional"),
            Readiness("Codex discovery", "ok", "ready"),
            Readiness("Claude discovery", "info", "optional"),
            Readiness("Herdr", "info", "optional"),
        )
        self.assertEqual(code, 1)
        self.assertIn("[FAIL required] Git: not installed", text)

    def test_missing_project_fails_without_running_external_probes(self) -> None:
        output = io.StringIO()
        missing = Path(tempfile.gettempdir()) / "side-dog-doctor-missing"
        code = doctor(str(missing), no_color=True, environment={}, output=output)
        self.assertEqual(code, 1)
        self.assertIn("[FAIL required] Project folder", output.getvalue())

    def test_explicit_project_stays_in_recommendation_inside_herdr(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="side dog ") as directory:
            ready = Readiness("ready", "ok", "ready")
            with (
                patch("side_dog.doctor.git_probe", return_value=ready),
                patch("side_dog.doctor.github_probe", return_value=ready),
                patch("side_dog.doctor.codex_probe", return_value=ready),
                patch("side_dog.doctor.claude_probe", return_value=ready),
                patch(
                    "side_dog.doctor.herdr_probe",
                    return_value=Readiness("Herdr", "ok", "ready"),
                ),
            ):
                code = doctor(
                    directory,
                    no_color=True,
                    environment={"HERDR_ENV": "1"},
                    output=output,
                    project_explicit=True,
                )

        self.assertEqual(code, 0)
        self.assertIn("Mode: explicit folder", output.getvalue())
        self.assertIn(f"side-dog watch '{Path(directory).resolve()}'", output.getvalue())

    def test_github_auth_is_scoped_to_selected_remote_host(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": ""})()
        with (
            patch("side_dog.doctor.shutil.which", return_value="/usr/bin/gh"),
            patch("side_dog.doctor._git_remote_host", return_value="example.com"),
            patch("side_dog.doctor._completed", return_value=completed) as run,
        ):
            result = github_probe(Path("/tmp/project"))

        self.assertEqual(result.status, "ok")
        run.assert_called_once_with(
            ["gh", "auth", "status", "--hostname", "example.com"]
        )

    def test_claude_hook_must_target_the_selected_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = root / "settings.local.json"
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "side-dog hook --root /old/project",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                )
            )

            self.assertFalse(_claude_hooks_installed(settings, root))


if __name__ == "__main__":
    unittest.main()
