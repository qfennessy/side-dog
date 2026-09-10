# Side Dog

<p align="center">
  <img src="https://raw.githubusercontent.com/qfennessy/side-dog/main/docs/side-dog-logo.png" alt="Side Dog logo: a dog beside a coding-agent activity panel" width="360">
</p>

Side Dog helps you follow coding agents across projects: **Watch** shows their
activity over time, while **Board** shows the live session roster. See edits,
tests, commits, pull requests, issues, and agent turns
in your terminal or a local browser panel.

[Read the Side Dog documentation](https://qfennessy.github.io/side-dog/).

Side Dog was inspired by [Sundai Hack 138](https://sundai.club). Sundai Club is
a community for building and launching AI prototypes every Sunday.

## Install

Requires Python 3.11+, Git, and macOS or Linux. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv tool install side-dog
side-dog --version
```

Upgrade an existing installation with `uv tool upgrade side-dog`. If the command
is not found, run `uv tool update-shell` and open a new terminal.
Follow the [platform installation guide](https://github.com/qfennessy/side-dog/blob/main/docs/install.md) for prerequisites,
PATH, upgrades, Git snapshots, uninstall, and offline man pages.

## Two terminal views

### Watch — follow the work

Watch shows a chronological feed with agent/model attribution, edits, test
results, commits, pushes, and PR observations. Press `e` to expand activity
and `b` to switch to Board.

![Side Dog Watch showing synthetic agent edits, tests, commits, and PR activity](https://raw.githubusercontent.com/qfennessy/side-dog/main/docs/side-dog-watch.gif)

### Board — see the live roster

Run `side-dog board` for the live session roster: agent, terminal or app,
repository and branch, linked issues, PR checks and review state, and status.
Use `j`/`k` to select a session, Enter for detail, and `g` to group rows.
Conflicts appear briefly when they are first detected; press `c` to show the
complete list again. Press `A` for Attention mode, which keeps only the
sessions that need a person (conflicts, blocked sessions, failed tests,
failing checks, requested changes, or stalled work) and says why on each row;
press it again to return to the full roster. The detail pane opens with a
short brief: status, confirmed issue and PR, the latest milestone, event
counts, and a rule-based cue such as "review PR" or "investigate failed
tests". Press `w` to switch to Watch.

![Side Dog Board live session roster with four synthetic agents, repositories, issues, PRs, and changing status](https://raw.githubusercontent.com/qfennessy/side-dog/main/docs/side-dog-board.gif)

Both recordings use synthetic data rendered by the current terminal UI;
they do not expose real agent sessions. See the
[recording instructions](https://github.com/qfennessy/side-dog/blob/main/docs/terminal-recordings.md).

## Try it

```sh
side-dog --version
side-dog demo --watch
side-dog doctor ~/src/my-project
side-dog watch ~/src/my-project
side-dog board
```

Replace the path with a real project. Use `side-dog demo --panel` for the browser tour.
For a real project, `side-dog panel ~/src/my-project` opens the local browser
timeline; its `/board` page shows the live roster. The server binds to loopback.
Claude Code requires optional project hooks for attributed tool activity; the
other registered agents read local metadata without Side Dog hooks.

## Coding agent support

Supports Codex, Claude Code, Pi, Oh My Pi, OpenCode, Crush, Cursor Agent, Grok
Build, DeepSeek Harness, Cline, Antigravity CLI, and Muse Code. Herdr is
optional, not an agent.
See the [current integration table and setup instructions](https://github.com/qfennessy/side-dog/blob/main/docs/integrations.md).
It distinguishes session discovery from live activity, including Cursor/Grok
through T3 Code, and lists custom data-location variables.

## With or without Herdr

Herdr is optional. `side-dog watch .` pins an explicit folder in a standalone
terminal. Bare `watch` discovers agent folders; inside Herdr it follows Herdr
context. See [discovery and everyday use](https://github.com/qfennessy/side-dog/blob/main/docs/everyday-use.md).

## What Side Dog shows

Watch is chronological activity; Board presents current sessions. GitHub
polling adds state to matching work but never becomes credited agent effort.

Only validated metadata enters Side Dog history or its local browser panel.
See [privacy, configuration and disposable state](https://github.com/qfennessy/side-dog/blob/main/docs/configuration.md).
[Tokens and estimated cost](https://github.com/qfennessy/side-dog/blob/main/docs/usage.md) are separate from PR contribution counts.

## Terminal and panel controls

Press `?` for help, `v` for Watch settings, `b` for Board, and `w` to return.
In Board, `A` toggles Attention mode and `c` reopens the temporary conflict
notice. The browser panel's `/board` page shows the live roster with the same
session brief under each row.
See [view-specific controls and workflows](https://github.com/qfennessy/side-dog/blob/main/docs/everyday-use.md).

## Configuration

Optional settings live in `~/.config/side-dog/config.toml`. Saved spaces, worktree
discovery, display preferences and notifications are explained in the
[configuration guide](https://github.com/qfennessy/side-dog/blob/main/docs/configuration.md).

## Other commands

`side-dog help` lists commands. `side-dog man` opens the bundled offline manual;
`side-dog man board` opens a command-specific page.

- [Troubleshooting](https://github.com/qfennessy/side-dog/blob/main/docs/troubleshooting.md)
- [Documentation validation and platform coverage](https://github.com/qfennessy/side-dog/blob/main/docs/validation.md)
- [Maintainer release guide](https://github.com/qfennessy/side-dog/blob/main/docs/releasing.md)
- [Dependency maintenance](https://github.com/qfennessy/side-dog/blob/main/docs/dependency-maintenance.md)
- [Security policy](https://github.com/qfennessy/side-dog/security/policy)

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
Do not put security details in public issues. Side Dog uses the [MIT License](https://github.com/qfennessy/side-dog/blob/main/LICENSE).
