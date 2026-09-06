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
PR is under review. Before tagging, replace `Unreleased` with the release date
and update `SECURITY.md` so the newly released `MAJOR.MINOR.x` line is marked
supported. CI rejects a malformed version, duplicated package version, missing
matching changelog heading, release/support mismatch, or a changed version that
does not exceed the latest release tag. Numeric components are compared as
integers, so `1.10.0` correctly follows `1.9.9`.

Changing the version does not create a tag, GitHub release, or package upload.
Publishing remains a separate, deliberate workflow tracked in issue #46. Never
reuse or move a released version tag; prepare a higher patch version instead.

## Configure trusted publishing once

The release workflow uses short-lived OpenID Connect credentials. Do not create
or store a PyPI API token in this repository.

Before the first release:

1. Create separate PyPI and TestPyPI accounts and enable two-factor
   authentication on both.
2. Register a pending trusted publisher for project `side-dog` on PyPI with
   owner `qfennessy`, repository `side-dog`, workflow `release.yml`, and
   environment `pypi`.
3. Register the equivalent TestPyPI publisher with environment `testpypi`.
4. Create GitHub environments named `testpypi` and `pypi`. Restrict deployment
   to release tags. Add a required reviewer to `pypi` if production publication
   should pause after the TestPyPI rehearsal.

The workflow and environment names must exactly match the trusted-publisher
registrations. Merging the workflow does not perform any of this setup and does
not publish anything.

## Publish a prepared version

Only after the release PR is merged, its changelog heading is dated, and the
trusted publishers and environments are ready:

```sh
version="$(uv run python -c 'from side_dog import __version__; print(__version__)')"
git switch main
git pull --ff-only
git tag -a "v${version}" -m "Side Dog ${version}"
git push origin "v${version}"
```

The tag starts `.github/workflows/release.yml`. It:

1. requires the tag, canonical package version, and dated changelog heading to
   agree;
2. runs the full tests, builds the wheel and source distribution once, validates
   their metadata, and smoke-tests the installed wheel;
3. publishes those verified files to TestPyPI, then PyPI, using separate
   protected environments and short-lived trusted-publishing credentials; and
4. creates a GitHub release containing the same files only after PyPI accepts
   them.

TestPyPI tolerates an already-uploaded rehearsal artifact so a safely paused
production deployment can be retried. PyPI does not: duplicate production
versions fail loudly. If PyPI has accepted any file for a version, prepare a new
patch version instead of moving or reusing the tag.

The publisher action also creates PyPI attestations by default. GitHub build
provenance and an SPDX software bill of materials remain tracked separately in
issue #151.
