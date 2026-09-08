# Install and first run

Side Dog requires Git, Python 3.11+, and macOS or Linux. Windows native terminals are not supported.
[PyPI](https://pypi.org/project/side-dog/) published wheel and source distributions for 2.0.0 on September 8, 2026 (verified against PyPI metadata). A merged PR does not update an installed release.

The contributions view, bundled man pages and macOS demo repair described here
are newer than PyPI 2.0.0. Use the [Git snapshot](#git-snapshots) for those features
until a release includes them. Released 2.0.0 still supports explicit-folder
Watch and doctor; its macOS demo can show an empty feed on aliased temporary paths.

## macOS

Install [Homebrew](https://brew.sh/) if needed, then:

```sh
brew install uv git
uv tool install side-dog
uv tool update-shell
```

Open a new terminal after the PATH update. `uv` supplies a compatible Python when needed.

## Linux

Install Git with your distribution's package manager. On Debian/Ubuntu:

```sh
sudo apt-get update
sudo apt-get install git ca-certificates curl man-db
```

Install uv using its [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/). For its standalone installer:

```sh
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/install-uv.sh
sh /tmp/install-uv.sh
```

You can inspect the downloaded installer before running it. Open a new terminal, then:

```sh
uv tool install side-dog
uv tool update-shell
```

Open another terminal if PATH changed. Linux package-manager and desktop instructions await hands-on validation in issue #194; Ubuntu CI validates Python runtime tests and wheel installation.

## First activity

```sh
command -v side-dog
side-dog --version
side-dog demo --watch
side-dog doctor ~/src/my-project
side-dog watch ~/src/my-project
```

Replace the project path with your real folder. The demo is synthetic and needs no agent or GitHub authentication. Start an agent in that same folder to see real activity. Claude Code needs `side-dog setup ~/src/my-project --claude` followed by a Claude restart for attributed tool events; see [integrations](integrations.md). Press `?` for controls.

`doctor` is read-only. Optional integrations can report unavailable without preventing local activity. For PR checks and review state, install GitHub CLI using its [official instructions](https://cli.github.com/) and run `gh auth login`, then `gh auth status`.

## Update or remove

```sh
uv tool upgrade side-dog
side-dog --version
uv tool uninstall side-dog
```

Uninstall removes the executable environment; it preserves your configuration and runtime history. Optional Claude hooks are project-local: inspect `.claude/settings.local.json` and remove only Side Dog hook entries if you no longer use them. Keep unrelated hooks.

## Git snapshots

Use this for changes merged since the latest release:

```sh
uv tool install --force 'side-dog @ git+https://github.com/qfennessy/side-dog.git'
side-dog --version
```

A Git installation is a snapshot. It does not keep following `main`. Refresh it explicitly:

```sh
uv tool install --force --refresh 'side-dog @ git+https://github.com/qfennessy/side-dog.git'
side-dog --version
```

The checkout helper `./scripts/install.sh` performs that Git refresh and executable check. To return to released packages, use `uv tool install --force side-dog`. A source change can retain the release version; use `uv tool list` and installation provenance when comparing snapshots.

## Offline manuals

```sh
side-dog man
side-dog man watch
side-dog man board
man "$(side-dog man board --path)"
```

Pages ship in the wheel and source distribution; no administrator install is required. `side-dog man --path` prints the exact bundled file location. If `man` is missing, use `side-dog help COMMAND` or install your platform's man viewer. To use traditional `man side-dog`, copy the bundled `.1` files into a directory such as `~/.local/share/man/man1` and add its parent to your manual search path. Recopy after upgrades; the bundled path always follows the installed version.
