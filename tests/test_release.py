from __future__ import annotations

import io
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from side_dog import __version__
from side_dog.cli import main


class ReleaseTests(unittest.TestCase):
    def test_build_reads_the_runtime_version_from_one_source(self) -> None:
        project = tomllib.loads(Path("pyproject.toml").read_text())
        self.assertNotIn("version", project["project"])
        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "side_dog.__version__"},
        )

    def test_cli_reports_the_release_version(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as stopped, redirect_stdout(output):
            main(["--version"])

        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"side-dog {__version__}")

    def test_release_workflow_is_tag_only_and_uses_short_lived_identity(self) -> None:
        workflow = Path(".github/workflows/release.yml").read_text()
        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("testpypi", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("pypa/gh-action-pypi-publish@v1.14.2", workflow)
        self.assertIn("GH_REPO: ${{ github.repository }}", workflow)
        self.assertNotIn("password:", workflow)
        self.assertNotIn("PYPI_API_TOKEN", workflow)

    def test_ci_builds_validates_and_installs_the_distribution(self) -> None:
        workflow = Path(".github/workflows/ci.yml").read_text()
        self.assertIn("uv build --clear", workflow)
        self.assertIn("uvx twine check dist/*", workflow)
        self.assertIn('uv tool install "$wheel"', workflow)
        self.assertIn('"$tool_root/bin/side-dog" --version', workflow)

    def test_release_docs_keep_publication_outside_the_preparation_pr(self) -> None:
        guide = Path("docs/releasing.md").read_text()
        self.assertIn("Merging the release workflow does not publish", guide)
        self.assertIn("not performed by this implementation PR", guide)


if __name__ == "__main__":
    unittest.main()
