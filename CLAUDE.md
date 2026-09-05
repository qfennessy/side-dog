# Claude Code instructions

Read and follow `AGENTS.md` for repository-wide development guidance.

## Required release skill

For every request to bump Side Dog's version or prepare a patch, minor, or
major release, you **must invoke** `/prepare-side-dog-release` before changing
`side_dog.__version__` or `CHANGELOG.md`. Follow that skill's increment
selection, dry-run, validation, and pull-request workflow. Do not substitute a
manual version edit.

Invoking the skill does not authorize a Git tag, GitHub release, TestPyPI
upload, or PyPI publication. Those external release actions always require
separate, explicit user authorization.
