from __future__ import annotations

import tempfile
import unittest
from importlib.metadata import version as installed_version
from pathlib import Path

from side_dog import __version__
from side_dog.release import (
    SemVer,
    bump_project,
    latest_release_version,
    require_advance,
    validate_project,
)

ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_codex_and_claude_release_skills_are_identical(self) -> None:
        codex = ROOT / ".agents/skills/prepare-side-dog-release/SKILL.md"
        claude = ROOT / ".claude/skills/prepare-side-dog-release/SKILL.md"

        self.assertEqual(codex.read_bytes(), claude.read_bytes())

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

    def test_bumps_each_semver_component(self) -> None:
        version = SemVer.parse("1.2.3")
        self.assertEqual(version.bump("patch"), SemVer(1, 2, 4))
        self.assertEqual(version.bump("minor"), SemVer(1, 3, 0))
        self.assertEqual(version.bump("major"), SemVer(2, 0, 0))
        with self.assertRaisesRegex(ValueError, "patch, minor, or major"):
            version.bump("feature")

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

    def test_bump_updates_the_canonical_version_and_unreleased_heading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.release_project(Path(directory), "1.2.3", unreleased=True)

            target = bump_project(root, "minor", tags=[])

            self.assertEqual(target, SemVer(1, 3, 0))
            self.assertIn(
                '__version__ = "1.3.0"',
                (root / "side_dog/__init__.py").read_text(),
            )
            changelog = (root / "CHANGELOG.md").read_text()
            self.assertIn("## [1.3.0] - Unreleased", changelog)
            self.assertNotIn("## [1.2.3]", changelog)

    def test_bump_adds_an_unreleased_heading_after_a_tagged_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.release_project(Path(directory), "1.2.3", unreleased=False)

            bump_project(root, "patch", tags=["v1.2.3"])

            changelog = (root / "CHANGELOG.md").read_text()
            self.assertLess(
                changelog.index("## [1.2.4]"), changelog.index("## [1.2.3]")
            )
            self.assertIn("## [1.2.3] - 2026-09-05", changelog)

    def test_dry_run_and_failed_advance_do_not_change_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.release_project(Path(directory), "1.2.3", unreleased=True)
            before = {
                path: path.read_text()
                for path in (root / "side_dog/__init__.py", root / "CHANGELOG.md")
            }

            self.assertEqual(
                bump_project(root, "patch", dry_run=True, tags=["v1.2.3"]),
                SemVer(1, 2, 4),
            )
            for path, contents in before.items():
                self.assertEqual(path.read_text(), contents)
            with self.assertRaisesRegex(ValueError, "must be greater"):
                bump_project(root, "patch", tags=["v2.0.0"])
            for path, contents in before.items():
                self.assertEqual(path.read_text(), contents)

    @staticmethod
    def release_project(root: Path, version: str, *, unreleased: bool) -> Path:
        package = root / "side_dog"
        package.mkdir()
        (package / "__init__.py").write_text(
            f'__version__ = "{version}"  # release source\n'
        )
        suffix = "Unreleased" if unreleased else "2026-09-05"
        (root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## [{version}] - {suffix}\n\n- Existing notes.\n"
        )
        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "side-dog"\n'
            'dynamic = ["version"]\n\n'
            "[tool.setuptools.dynamic]\n"
            'version = { attr = "side_dog.__version__" }\n'
        )
        return root


if __name__ == "__main__":
    unittest.main()
