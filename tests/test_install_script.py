from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
REQUIREMENT = "side-dog @ git+https://github.com/qfennessy/side-dog.git@main"


class InstallScriptTests(unittest.TestCase):
    def write_executable(self, path: Path, source: str) -> None:
        path.write_text(source, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def sandbox(
        self,
        *,
        include_git: bool = True,
        include_uv: bool = True,
        include_side_dog: bool = True,
        git_fails: bool = False,
        uv_version_fails: bool = False,
        uv_fails: bool = False,
        tool_bin_on_path: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command_bin = root / "commands"
            tool_bin = root / "tool bin"
            command_bin.mkdir()
            tool_bin.mkdir()
            log = root / "uv.log"

            if include_git:
                git_status = 19 if git_fails else 0
                self.write_executable(
                    command_bin / "git", f"#!/bin/sh\nexit {git_status}\n"
                )
            if include_uv:
                install_status = "exit 17" if uv_fails else "exit 0"
                version_status = "exit 18" if uv_version_fails else "exit 0"
                self.write_executable(
                    command_bin / "uv",
                    "#!/bin/sh\n"
                    "if [ \"$1\" = '--version' ]; then\n"
                    f"    {version_status}\n"
                    "fi\n"
                    "printf '%s\\n' \"$*\" >> \"$INSTALL_LOG\"\n"
                    "if [ \"$1 $2\" = 'tool install' ]; then\n"
                    f"    {install_status}\n"
                    "fi\n"
                    "if [ \"$1 $2 $3\" = 'tool dir --bin' ]; then\n"
                    "    printf '%s\\n' \"$TOOL_BIN_DIR\"\n"
                    "fi\n",
                )
            if include_side_dog:
                self.write_executable(
                    tool_bin / "side-dog",
                    "#!/bin/sh\nprintf '%s\\n' 'side-dog 1.0.0'\n",
                )

            path_parts = [os.fspath(command_bin)]
            if tool_bin_on_path:
                path_parts.append(os.fspath(tool_bin))
            environment = {
                **os.environ,
                "PATH": os.pathsep.join(path_parts),
                "INSTALL_LOG": os.fspath(log),
                "TOOL_BIN_DIR": os.fspath(tool_bin),
            }
            result = subprocess.run(
                ["/bin/sh", os.fspath(INSTALLER)],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            calls = (
                log.read_text(encoding="utf-8").splitlines() if log.exists() else []
            )
            return result, calls

    def test_force_refreshes_main_and_verifies_the_installed_executable(self) -> None:
        result, calls = self.sandbox(tool_bin_on_path=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                f"tool install --force --refresh {REQUIREMENT}",
                "tool dir --bin",
            ],
        )
        self.assertIn("Installed side-dog 1.0.0.", result.stdout)
        self.assertIn("side-dog doctor .", result.stdout)
        self.assertNotIn("uv tool update-shell", result.stdout)

    def test_explains_how_to_make_the_uv_tool_directory_visible(self) -> None:
        result, _calls = self.sandbox()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("which is not on PATH", result.stdout)
        self.assertIn("uv tool update-shell", result.stdout)

    def test_missing_prerequisites_fail_before_installing(self) -> None:
        missing_uv, missing_uv_calls = self.sandbox(include_uv=False)
        self.assertNotEqual(missing_uv.returncode, 0)
        self.assertIn("uv is required", missing_uv.stderr)
        self.assertIn("docs.astral.sh", missing_uv.stderr)
        self.assertEqual(missing_uv_calls, [])

        missing_git, missing_git_calls = self.sandbox(include_git=False)
        self.assertNotEqual(missing_git.returncode, 0)
        self.assertIn("Git is required", missing_git.stderr)
        self.assertEqual(missing_git_calls, [])

    def test_broken_prerequisites_are_reported_before_installing(self) -> None:
        broken_git, broken_git_calls = self.sandbox(git_fails=True)
        self.assertNotEqual(broken_git.returncode, 0)
        self.assertIn("Git was found but could not run", broken_git.stderr)
        self.assertEqual(broken_git_calls, [])

        broken_uv, broken_uv_calls = self.sandbox(uv_version_fails=True)
        self.assertNotEqual(broken_uv.returncode, 0)
        self.assertIn("uv was found but could not run", broken_uv.stderr)
        self.assertEqual(broken_uv_calls, [])

    def test_uv_failure_stops_before_post_install_verification(self) -> None:
        result, calls = self.sandbox(uv_fails=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv could not install Side Dog", result.stderr)
        self.assertEqual(calls, [f"tool install --force --refresh {REQUIREMENT}"])

    def test_missing_executable_is_reported_after_uv_succeeds(self) -> None:
        result, calls = self.sandbox(include_side_dog=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no executable was found", result.stderr)
        self.assertEqual(
            calls,
            [
                f"tool install --force --refresh {REQUIREMENT}",
                "tool dir --bin",
            ],
        )

    def test_rejects_arguments_instead_of_silently_ignoring_them(self) -> None:
        result = subprocess.run(
            ["/bin/sh", os.fspath(INSTALLER), "unexpected"],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not accept arguments", result.stderr)

    def test_script_is_executable_and_valid_posix_shell(self) -> None:
        self.assertTrue(INSTALLER.stat().st_mode & stat.S_IXUSR)
        result = subprocess.run(
            ["/bin/sh", "-n", os.fspath(INSTALLER)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
