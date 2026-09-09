# Troubleshooting

## Command missing or an old version still runs

Run `command -v side-dog`, `type -a side-dog`, `uv tool list` and
`side-dog --version`. Multiple installations can shadow one another. Run
`uv tool update-shell`, open a fresh terminal and check again. Use the
[update instructions](install.md#update-or-remove) for your distribution channel.
If `side-dog --version` says `unknown command`, that copy predates the flag;
refresh the Git snapshot or reinstall the published package.

## No agent or no activity

First run `side-dog demo --watch` to check terminal rendering, then
`side-dog doctor /path/to/project` and `side-dog watch /path/to/project`.
Run the agent in that exact folder. A discovered session is not necessarily a
ready activity integration: check [each integration's setup](integrations.md).
Claude needs project hooks and a restart; Cursor and Grok need T3 Code's
projected store. Custom agent data directories must be visible to the Side Dog
process through their documented environment variables.

Press `v` and choose all events, clear `/` search, and check folder focus.
Background file and lifecycle events can be hidden; `F` toggles them.
Use explicit paths to override automatic discovery. Herdr is optional;
`--herdr` explicitly requires it and fails when unavailable.

## Board misses a session

Board is a live roster. An explicit folder excludes sibling worktrees unless
you name them too. Run Watch or the panel to collect activity, and use Watch
to inspect completed work in its timeline. Unknown models cannot be recovered
from a current session after the agent has exited.

## No PR information, offline GitHub, or missing gh

Run `gh --version` and `gh auth status`. Install/authenticate GitHub CLI if you
want remote checks and review state. Verify the checkout's origin and branch.
Local activity works offline. Unknown or `PARTIAL` readbacks mean unavailable
information, not zero failures or confirmed success. Readbacks back off on
idle/closed branches and errors. `--github-poll 0` disables terminal readback.

## Notifications do not appear

Only terminal Board transitions send desktop alerts; failed tests remain in
the timeline without popups. Board alerts are opt-in: press uppercase `P` in Watch or Board, or set
`[notify] enabled = true` in config. `--no-notify` locks them off for that run.
On macOS, check notification permissions and Focus settings. Linux requires
`notify-send` and a working desktop notification service; a headless SSH session
may have neither. See the [Linux validation matrix](linux-validation.md) for tested service and
headless behavior and remaining desktop limitations.

## Configuration changes do not take effect

Confirm `XDG_CONFIG_HOME` and the TOML path. Malformed TOML silently uses
defaults. Interactive display state can override defaults; use `v` to change it.
Keep hand-authored spaces in `config.toml`; `watch --save` rewrites `spaces.toml`.
See [configuration and private state](configuration.md).
