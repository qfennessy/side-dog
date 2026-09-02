from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from side_dog.doctor import (
    Readiness,
    _claude_hooks_installed,
    cline_probe,
    doctor,
    github_probe,
)
from side_dog.cli import command_for_hook, desired_hooks


class DoctorTests(unittest.TestCase):
    def run_doctor(self, *checks: Readiness) -> tuple[int, str]:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("side_dog.doctor.git_probe", return_value=checks[0]),
                patch("side_dog.doctor.github_probe", return_value=checks[1]),
                patch("side_dog.doctor.codex_probe", return_value=checks[2]),
                patch(
                    "side_dog.doctor.cline_probe",
                    return_value=Readiness("Cline discovery", "ok", "ready"),
                ),
                patch("side_dog.doctor.antigravity_probe", return_value=checks[3]),
                patch("side_dog.doctor.claude_probe", return_value=checks[4]),
                patch("side_dog.doctor.herdr_probe", return_value=checks[5]),
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
            Readiness("Antigravity discovery", "ok", "ready"),
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
            Readiness("Antigravity discovery", "ok", "ready"),
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
            Readiness("Antigravity discovery", "info", "no sessions yet"),
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
            Readiness("Antigravity discovery", "ok", "ready"),
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
                patch("side_dog.doctor.cline_probe", return_value=ready),
                patch("side_dog.doctor.antigravity_probe", return_value=ready),
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

    def test_cline_probe_honors_data_directory_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            sessions = data / "sessions"
            sessions.mkdir()

            result = cline_probe({"CLINE_DATA_DIR": str(data)})

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.name, "Cline discovery")

    def test_github_auth_is_scoped_to_selected_remote_host(self) -> None:
        repository = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": '{"url":"https://example.com/acme/project"}'},
        )()
        authenticated = type("Completed", (), {"returncode": 0, "stdout": ""})()
        with (
            patch("side_dog.doctor.shutil.which", return_value="/usr/bin/gh"),
            patch(
                "side_dog.doctor._completed",
                side_effect=[repository, authenticated],
            ) as run,
        ):
            result = github_probe(Path("/tmp/project"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["gh", "repo", "view", "--json", "url"],
                    cwd=Path("/tmp/project"),
                ),
                call(
                    [
                        "gh",
                        "auth",
                        "status",
                        "--hostname",
                        "example.com",
                        "--active",
                    ]
                ),
            ],
        )

    def test_github_readback_requires_an_addressable_repository(self) -> None:
        with (
            patch("side_dog.doctor.shutil.which", return_value="/usr/bin/gh"),
            patch(
                "side_dog.doctor._completed",
                return_value=type("Completed", (), {"returncode": 1, "stdout": ""})(),
            ) as run,
        ):
            result = github_probe(Path("/tmp/project"))

        self.assertEqual(result.status, "warn")
        self.assertIn("cannot be mapped", result.detail)
        run.assert_called_once_with(
            ["gh", "repo", "view", "--json", "url"], cwd=Path("/tmp/project")
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

    def test_claude_hook_requires_every_managed_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = root / "settings.local.json"
            command = command_for_hook(root)
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {"type": "command", "command": command}
                                    ]
                                }
                            ]
                        }
                    }
                )
            )

            self.assertFalse(_claude_hooks_installed(settings, root))

            settings.write_text(json.dumps({"hooks": desired_hooks(command)}))
            self.assertTrue(_claude_hooks_installed(settings, root))

            document = {"hooks": desired_hooks(command)}
            document["hooks"]["PreToolUse"][0]["matcher"] = "^Bash$"
            settings.write_text(json.dumps(document))
            self.assertFalse(_claude_hooks_installed(settings, root))

    def test_claude_hook_rejects_missing_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = root / "settings.local.json"
            command = f"/missing/side-dog hook --root {root}"
            settings.write_text(json.dumps({"hooks": desired_hooks(command)}))

            self.assertFalse(_claude_hooks_installed(settings, root))

    def test_claude_hook_rejects_unrelated_runnable_command_in_managed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = root / "settings.local.json"
            command = command_for_hook(root)
            document = {"hooks": desired_hooks(command)}
            for entries in document["hooks"].values():
                stale = entries[0]["hooks"][0]
                stale["command"] = "side-dog hook --root /old/project"
                entries[0]["hooks"].append(
                    {
                        "type": "command",
                        "command": f"true --root {root}",
                    }
                )
            settings.write_text(json.dumps(document))

            self.assertFalse(_claude_hooks_installed(settings, root))

    def test_claude_hook_ignores_non_object_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            settings = root / "settings.local.json"
            document = {"hooks": desired_hooks(command_for_hook(root))}
            document["hooks"]["Stop"].insert(0, None)
            settings.write_text(json.dumps(document))

            self.assertTrue(_claude_hooks_installed(settings, root))

    def test_antigravity_probe_detects_app_data_or_cli(self) -> None:
        from side_dog.doctor import antigravity_probe

        with tempfile.TemporaryDirectory() as directory:
            app_dir = Path(directory) / "antigravity-cli"
            brain = app_dir / "brain"
            brain.mkdir(parents=True)
            result = antigravity_probe({"ANTIGRAVITY_APP_DATA_DIR": str(app_dir)})
            self.assertEqual(result.status, "ok")
            self.assertIn("discovery is ready", result.detail)

    def test_antigravity_probe_reports_info_when_absent(self) -> None:
        from side_dog.doctor import antigravity_probe

        with tempfile.TemporaryDirectory() as directory:
            result = antigravity_probe(
                {
                    "ANTIGRAVITY_APP_DATA_DIR": str(
                        Path(directory) / "nonexistent"
                    )
                }
            )
            self.assertEqual(result.status, "info")
            self.assertIn("No local Antigravity", result.detail)


if __name__ == "__main__":
    unittest.main()
