---
name: prepare-side-dog-release
description: Prepare a Side Dog patch, minor, or major release by updating its canonical SemVer and changelog, validating the package, and opening one release pull request. Use when asked to bump Side Dog's version or prepare a release; do not use merely because an ordinary change lands.
---

# Prepare a Side Dog release

Create one release-preparation pull request. Do not bump the version inside
each feature or fix pull request; parallel changes would conflict in the
canonical version and changelog.

## Choose the increment

Honor an increment explicitly named by the user. Otherwise inspect the
user-visible changes since the latest `vMAJOR.MINOR.PATCH` tag and choose the
largest applicable increment:

- `patch`: backward-compatible fixes or documentation corrections that should ship;
- `minor`: backward-compatible features or new coding-agent integrations;
- `major`: incompatible CLI, configuration, state, or integration-contract changes.

If patch and minor changes are both present, choose minor. Never infer or
silently apply a major increment; obtain explicit user direction first.

## Prepare the release

Work from an up-to-date isolated branch or worktree and preserve unrelated
changes. Fetch tags, inspect the current version and changelog, then preview
the selected increment:

```sh
git fetch origin --tags
git tag --list 'v*' --sort=-version:refname
uv run python -m side_dog.release --bump patch --dry-run
```

Replace `patch` with `minor` or an explicitly approved `major`. If the preview
is correct, run the same command without `--dry-run`. It updates
`side_dog.__version__` and the matching `CHANGELOG.md` heading together.

Edit the new `Unreleased` section so it contains concise, user-visible changes
since the preceding release. Keep `side_dog.__version__` as the only package
version source; do not add `project.version` to `pyproject.toml`.

## Validate and hand off

Run:

```sh
uv run python -m side_dog.release --require-advance
uv run python -m unittest discover -s tests -q
uv build
uvx twine check dist/*
```

Commit with `chore: prepare Side Dog X.Y.Z`, push, and open one non-draft
release pull request. Report the chosen increment, resulting version, tests,
build validation, and any uncertainty about the changelog boundary.

Changing the version is release preparation only. Do not create or move a Git
tag, GitHub release, or PyPI/TestPyPI publication unless the user separately
authorizes that external action.
