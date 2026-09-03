from __future__ import annotations

import tempfile
import unittest
from importlib.metadata import version as installed_version
from pathlib import Path

from side_dog import __version__
from side_dog.release import (
    SemVer,
    latest_release_version,
    require_advance,
    validate_project,
)

ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_current_source_and_installed_metadata_use_the_v1_baseline(self) -> None:
        self.assertEqual(__version__, "1.0.0")
        self.assertEqual(installed_version("side-dog"), __version__)
        self.assertEqual(validate_project(ROOT, tags=[]), SemVer(1, 0, 0))

    def test_accepts_stable_semver_and_compares_components_numerically(self) -> None:
        self.assertEqual(SemVer.parse("12.34.56"), SemVer(12, 34, 56))
        self.assertEqual(
            latest_release_version(["notes", "v1.9.9", "v1.10.0", "v2.0.0"]),
            SemVer(2, 0, 0),
        )

    def test_rejects_invalid_or_unsupported_prerelease_versions(self) -> None:
        for value in ("1", "1.0", "01.0.0", "1.0.0-rc.1", "1.0.0+build"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SemVer.parse(value)

    def test_requires_a_release_to_advance_beyond_the_latest_tag(self) -> None:
        require_advance(SemVer.parse("1.10.0"), ["v1.9.9"])
        for value in ("1.9.9", "1.8.20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                require_advance(SemVer.parse(value), ["v1.9.9"])

    def test_rejects_a_non_semver_release_tag(self) -> None:
        with self.assertRaisesRegex(ValueError, "vMAJOR.MINOR.PATCH"):
            latest_release_version(["v1.0"])

    def test_project_metadata_must_derive_from_the_canonical_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "side_dog"
            package.mkdir()
            (package / "__init__.py").write_text('__version__ = "1.0.0"\n')
            (root / "CHANGELOG.md").write_text("## [1.0.0] - Unreleased\n")
            (root / "pyproject.toml").write_text(
                '[project]\nname = "side-dog"\nversion = "0.9.0"\n'
            )

            with self.assertRaisesRegex(ValueError, "must not duplicate"):
                validate_project(root, tags=[])


if __name__ == "__main__":
    unittest.main()
