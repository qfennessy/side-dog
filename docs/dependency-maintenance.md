# Dependency maintenance

Side Dog uses GitHub's native Dependabot support for the `uv` ecosystem and
GitHub Actions. Maintainers own the generated pull requests and should review
release notes, the dependency diff, and normal CI results before merging them.

## Cadence and pull-request limits

- Python and `uv.lock` updates run at 09:00 America/New_York each Monday. Minor
  and patch updates are grouped; major updates remain separate for deliberate
  review. At most two Python version-update pull requests may be open.
- GitHub Actions updates run at 09:00 America/New_York each Wednesday and are
  grouped into one pull request. At most one Actions version-update pull request
  may be open.
- Both ecosystems wait seven days after a release before proposing a routine
  version update. Dependabot security updates are not delayed by this cooldown
  or counted against these version-update limits.

The Python configuration uses `package-ecosystem: "uv"`. GitHub documents uv
and `uv.lock` as natively supported, so Side Dog does not run a separate
scheduled `uv lock --upgrade` workflow. Dependabot may update only `uv.lock`
when the existing requirement already allows the new release; otherwise it can
also widen the requirement. The normal `uv sync --locked` CI step rejects an
inconsistent manifest and lockfile.

## Pull-request dependency review

Every pull request to `main` runs the official dependency review action with
read-only repository permission. It reports all detected dependency changes
and fails when a newly introduced dependency has a high or critical known
vulnerability. Moderate and low advisories remain visible for review without
blocking unrelated work.

## Operational check

After changing `.github/dependabot.yml`, open **Insights > Dependency graph >
Dependabot**, run **Check for updates** for both `uv` and `github-actions`, and
confirm that each ecosystem records a successful last-checked time. For a
generated uv pull request, verify that any `pyproject.toml` and `uv.lock`
changes remain consistent by checking that normal CI passes. Dependabot's
configuration is activated from the default branch, so this service-side check
cannot be completed from an unmerged configuration pull request.

Current references:

- [Dependabot supported ecosystems and repositories](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories)
- [Dependabot options reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference)
- [Dependency review action configuration](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/manage-your-dependency-security/configure-dependency-review-action)
