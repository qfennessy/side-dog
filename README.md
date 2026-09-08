# Side Dog

<p align="center">
  <img src="https://raw.githubusercontent.com/qfennessy/side-dog/main/docs/side-dog-logo.png" alt="A golden retriever watching an event timeline" width="360">
</p>

Side Dog is a narrow terminal timeline and local browser panel for watching
coding agents work. It shows edits, tests, Git activity, pull requests, issues,
and agent turns as they happen.

[Read the Side Dog documentation](https://qfennessy.github.io/side-dog/).

Side Dog was inspired by [Sundai Hack 138](https://sundai.club). Sundai Club is
a community for building and launching AI prototypes every Sunday.

## Install

Install a released package with `uv tool install side-dog` on macOS or Linux.
Follow the [platform installation guide](docs/install.md) for prerequisites,
PATH, upgrades, Git snapshots, uninstall, and offline man pages.
These guides describe current source. Until the next release, use the Git
snapshot for contributions, man pages and the repaired macOS demo.

## Try it

```sh
side-dog --version
side-dog demo --watch
side-dog doctor ~/src/my-project
side-dog watch ~/src/my-project
```

Replace the path with a real project. Use `side-dog demo --panel` for the browser tour.
Claude Code requires optional project hooks for attributed tool activity; the
other registered agents read local metadata without Side Dog hooks.

## Coding agent support

See the [current integration table and setup instructions](docs/integrations.md).
It distinguishes session discovery from live activity, including Cursor/Grok
through T3 Code, and lists custom data-location variables.

## With or without Herdr

Herdr is optional. `side-dog watch .` pins an explicit folder in a standalone
terminal. Bare `watch` discovers agent folders; inside Herdr it follows Herdr
context. See [discovery and everyday use](docs/everyday-use.md).

## What Side Dog shows

Watch is chronological activity; Board presents current sessions and recorded
contributions by model/session. GitHub polling is labeled as observation and
unknown attribution stays unknown. Read [Which model worked on this PR?](docs/contributions.md).

Only validated metadata enters Side Dog history or its local browser panel.
See [privacy, configuration and disposable state](docs/configuration.md).
[Tokens and estimated cost](docs/usage.md) are separate from PR contribution counts.

## Terminal and panel controls

Press `?` for help, `v` for Watch settings, `b` for Board, and `w` to return.
In Board, `a` toggles contributions and the live roster. The browser panel's
`/board` page shows the live roster.
See [view-specific controls and workflows](docs/everyday-use.md).

## Configuration

Optional settings live in `~/.config/side-dog/config.toml`. Saved spaces, worktree
discovery, display preferences and notifications are explained in the
[configuration guide](docs/configuration.md).

## Other commands

`side-dog help` lists commands. `side-dog man` opens the bundled offline manual;
`side-dog man board` opens a command-specific page.

- [Troubleshooting](docs/troubleshooting.md)
- [Documentation validation and platform coverage](docs/validation.md)
- [Maintainer release guide](docs/releasing.md)
- [Dependency maintenance](docs/dependency-maintenance.md)
- [Security policy](SECURITY.md)

## Develop from a checkout

```sh
uv sync --locked
uv run python -m unittest discover -s tests -q
uv run python scripts/build_man_pages.py --check
python scripts/build_docs_site.py
python scripts/check_docs_links.py
```

Release preparation follows the maintainer guide; merging does not publish a package.

For private vulnerability reporting, use the
[security policy](https://github.com/qfennessy/side-dog/security/policy).
Do not put security details in public issues. Side Dog uses the [MIT License](LICENSE).
