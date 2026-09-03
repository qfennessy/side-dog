from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class CiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    def test_runs_tests_on_supported_python_versions_and_platforms(self) -> None:
        self.assertIn("pull_request:", self.workflow)
        self.assertIn("ubuntu-latest", self.workflow)
        self.assertIn("macos-latest", self.workflow)
        for version in ("3.11", "3.12", "3.13"):
            self.assertIn(f'- "{version}"', self.workflow)
        self.assertIn("uv run python -m unittest discover -s tests -q", self.workflow)

    def test_builds_checks_and_runs_the_installed_wheel(self) -> None:
        self.assertIn("needs: test", self.workflow)
        self.assertIn("fetch-depth: 0", self.workflow)
        self.assertIn("python -m side_dog.release --base-ref", self.workflow)
        self.assertIn("uv build", self.workflow)
        self.assertIn("uvx twine check dist/*", self.workflow)
        self.assertIn('uv tool install "$wheel"', self.workflow)
        self.assertIn('side-dog" --version', self.workflow)
        self.assertIn('side-dog" doctor . --no-color', self.workflow)
        self.assertIn('"${UV_TOOL_BIN_DIR}/side-dog" help', self.workflow)

    def test_has_read_only_permissions_and_no_publishing(self) -> None:
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertNotIn("id-token: write", self.workflow)
        self.assertNotIn("pypa/gh-action-pypi-publish", self.workflow)


if __name__ == "__main__":
    unittest.main()
