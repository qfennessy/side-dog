# Releasing Side Dog

Side Dog releases are built by GitHub Actions and published with short-lived
OIDC credentials. Do not create or store a PyPI API token in this repository.

Merging the release workflow does not publish a package. Publication starts
only when a maintainer deliberately pushes a `vX.Y.Z` tag after completing the
external setup below.

## One-time external setup

Before the first tag:

1. Create separate PyPI and TestPyPI accounts and enable two-factor
   authentication on both.
2. Register a pending trusted publisher for project `side-dog` on PyPI:
   owner `qfennessy`, repository `side-dog`, workflow `release.yml`, environment
   `pypi`.
3. Register the equivalent TestPyPI publisher with environment `testpypi`.
4. Create GitHub environments named `testpypi` and `pypi`. Restrict both to
   release tags; add a required reviewer to `pypi` so TestPyPI can be checked
   before production publication.

The environment and workflow names must exactly match the trusted-publisher
registrations. These account and repository-setting changes are intentionally
not performed by this implementation PR.

## Prepare a release

1. Update `side_dog.__version__` to the new PEP 440 version.
2. Add a matching `## [X.Y.Z]` entry to `CHANGELOG.md` and replace
   `Unreleased` with the release date.
3. Open and merge a normal pull request. CI must pass on every supported Python
   and operating-system combination, and the package job must validate and
   install the built wheel.
4. From the updated `main`, create and push an annotated `vX.Y.Z` tag.

The release workflow rejects a tag that does not match `side_dog.__version__`
or lacks a matching changelog heading. It builds once, publishes the same
artifacts to TestPyPI and PyPI through their protected environments, then
attaches those artifacts to a GitHub release.

PyPI versions are immutable. If an upload fails after a version is accepted,
prepare a new patch version rather than moving or reusing the tag.

## Local preflight

Run the same package checks without publishing:

```sh
uv run python -m unittest discover -s tests -q
uv build --clear
uvx twine check dist/*
uv tool run --isolated --from dist/side_dog-*.whl side-dog --version
uv tool run --isolated --from dist/side_dog-*.whl side-dog help
```

Do not push a release tag until the trusted publishers and both GitHub
environments have been configured and verified.
