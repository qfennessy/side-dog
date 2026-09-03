"""Validate Side Dog's release version without publishing anything."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SEMVER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


@dataclass(frozen=True, order=True, slots=True)
class SemVer:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> SemVer:
        if SEMVER_PATTERN.fullmatch(value) is None:
            raise ValueError(
                f"{value!r} is not stable SemVer MAJOR.MINOR.PATCH; "
                "prerelease and build identifiers are not supported"
            )
        return cls(*(int(component) for component in value.split(".")))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def latest_release_version(tags: Iterable[str]) -> SemVer | None:
    versions: list[SemVer] = []
    for tag in tags:
        if not tag.startswith("v"):
            continue
        try:
            versions.append(SemVer.parse(tag[1:]))
        except ValueError as error:
            raise ValueError(
                f"release tag {tag!r} must be vMAJOR.MINOR.PATCH"
            ) from error
    return max(versions, default=None)


def require_advance(current: SemVer, tags: Iterable[str]) -> None:
    latest = latest_release_version(tags)
    if latest is not None and current <= latest:
        raise ValueError(
            f"release version {current} must be greater than latest tag v{latest}"
        )


def _canonical_version(source: str, *, location: str) -> str:
    try:
        module = ast.parse(source, filename=location)
    except SyntaxError as error:
        raise ValueError(f"cannot parse canonical version source {location}") from error
    values: list[str] = []
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if isinstance(target, ast.Name) and target.id == "__version__":
            if not isinstance(statement.value, ast.Constant) or not isinstance(
                statement.value.value, str
            ):
                raise ValueError(f"{location} must assign __version__ a string literal")
            values.append(statement.value.value)
    if len(values) != 1:
        raise ValueError(f"{location} must define __version__ exactly once")
    return values[0]


def read_canonical_version(root: Path) -> SemVer:
    path = root / "side_dog" / "__init__.py"
    return SemVer.parse(
        _canonical_version(path.read_text(encoding="utf-8"), location=str(path))
    )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "git command failed"
        raise ValueError(detail)
    return completed.stdout


def release_tags(root: Path) -> list[str]:
    return _git(root, "tag", "--list", "v*").splitlines()


def version_at_ref(root: Path, reference: str) -> SemVer | None:
    if not reference or set(reference) == {"0"}:
        return None
    source = _git(root, "show", f"{reference}:side_dog/__init__.py")
    return SemVer.parse(
        _canonical_version(source, location=f"{reference}:side_dog/__init__.py")
    )


def validate_project(
    root: Path,
    *,
    base_ref: str | None = None,
    always_require_advance: bool = False,
    tags: Iterable[str] | None = None,
) -> SemVer:
    current = read_canonical_version(root)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    metadata = project.get("project", {})
    if "version" in metadata:
        raise ValueError(
            "pyproject.toml must not duplicate the canonical version with project.version"
        )
    if metadata.get("dynamic") != ["version"]:
        raise ValueError('pyproject.toml must declare dynamic = ["version"]')
    dynamic = project.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    if dynamic.get("version") != {"attr": "side_dog.__version__"}:
        raise ValueError(
            "setuptools must derive package metadata from side_dog.__version__"
        )

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if (
        re.search(
            rf"^## \[{re.escape(str(current))}\](?:\s|$)", changelog, re.MULTILINE
        )
        is None
    ):
        raise ValueError(f"CHANGELOG.md needs a ## [{current}] release heading")

    available_tags = list(tags) if tags is not None else release_tags(root)
    base_version = version_at_ref(root, base_ref) if base_ref else None
    if always_require_advance or (base_version is not None and base_version != current):
        require_advance(current, available_tags)
    return current


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Side Dog's canonical stable SemVer release version."
    )
    parser.add_argument(
        "--base-ref",
        default=None,
        help="require advancement when the canonical version differs from this Git ref",
    )
    parser.add_argument(
        "--require-advance",
        action="store_true",
        help="require the current version to exceed the latest vMAJOR.MINOR.PATCH tag",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = Path.cwd()
    try:
        version = validate_project(
            root,
            base_ref=arguments.base_ref,
            always_require_advance=arguments.require_advance,
        )
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"release version check failed: {error}", file=sys.stderr)
        return 1
    print(f"release version {version} is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
