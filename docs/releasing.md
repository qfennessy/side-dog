# Releasing Side Dog

Side Dog uses stable semantic versions in the form `MAJOR.MINOR.PATCH`. The
single source of truth is `side_dog.__version__`; package metadata, the CLI,
and `side-dog doctor` derive their displayed versions from it. Prerelease and
build identifiers are not supported.

Choose the next version according to the user-visible impact:

- increment **MAJOR** for incompatible CLI, configuration, state, or integration
  contract changes;
- increment **MINOR** for backward-compatible features and integrations;
- increment **PATCH** for backward-compatible fixes and documentation-only
  corrections that should ship independently.

## Prepare the next version

Start from an up-to-date branch. Preview and prepare the appropriate increment;
the command updates the canonical version and matching `Unreleased` changelog
heading together:

```sh
git fetch origin --tags
git tag --list 'v*' --sort=-version:refname
uv run python -m side_dog.release --bump patch --dry-run
uv run python -m side_dog.release --bump patch
$EDITOR CHANGELOG.md
uv run python -m side_dog.release --require-advance
uv run python -m unittest discover -s tests -q
uv build
uvx twine check dist/*
```

Use `--bump minor` for a backward-compatible feature and reserve
`--bump major` for an explicitly approved incompatible change. The dry run
does not modify either file. After the bump, replace or add concise changelog
bullets describing the user-visible changes.

Use a `## [MAJOR.MINOR.PATCH] - Unreleased` changelog heading while the release
PR is under review. Replace `Unreleased` with the release date before tagging.
CI rejects a malformed version, duplicated package version, missing matching
changelog heading, or a changed version that does not exceed the latest release
tag. Numeric components are compared as integers, so `1.10.0` correctly follows
`1.9.9`.

After the preparation PR is reviewed and merged, a maintainer may create a
matching annotated tag:

```sh
version="$(uv run python -c 'from side_dog import __version__; print(__version__)')"
git tag -a "v${version}" -m "Side Dog ${version}"
git push origin "v${version}"
```

Changing the version does not create a tag, GitHub release, or package upload.
Publishing remains a separate, deliberate workflow tracked in issue #46. Never
reuse or move a released version tag; prepare a higher patch version instead.
