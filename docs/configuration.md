# Configuration and private state

## Configuration

Configuration is optional. Side Dog reads
`~/.config/side-dog/config.toml`, or
`$XDG_CONFIG_HOME/side-dog/config.toml` when `XDG_CONFIG_HOME` is set.

```toml
pin = ["~/src/side-dog"]
ignore = ["~/.codex/worktrees/*", "~/Documents/Codex/*"]

[display]
order = "newest"       # newest or oldest
detail = "compact"     # compact or expanded
filter = "all"         # all, milestones, or files
layout = "auto"         # auto, columns, or timeline
show_filesystem_activity = false  # background files and lifecycle rows are hidden by default
limit = 8

[board]
group = "repo"         # repo (default), surface, or none
detail = "shown"       # shown or hidden

[notify]
enabled = false         # default; true opts in when Side Dog starts

[spaces]
review = ["~/src/project", "~/src/project-issue-42"]

[usage]
enabled = true
command = ["ccusage"]
agent = "claude-code"
offline = false
block_refresh_seconds = 10
session_refresh_seconds = 180
```

- `pin` keeps folders visible even when they are quiet.
- `ignore` hides automatically discovered folders. A folder named directly on
  the command line still wins.
- `[display]` sets the initial view. Interactive changes are remembered. Press
  `v` for a radio-toggle dialog covering order, filter, detail, and layout.
- `[board]` sets how `side-dog board` and the panel's `/board` page start.
  Sessions are grouped by repository by default; `group` can instead group
  them by surface or show one flat list, and `detail` shows
  or hides the detail pane. `--group` and `--no-detail` on the command line
  win over the file, as does `?group=` on the page. A misspelled value falls
  back to the default and never stops the board.
- Desktop alerts are off by default. Press uppercase `P` in Watch or Board to
  enable or disable them for the current terminal session. Set
  `[notify] enabled = true` to start with alerts enabled, including in the
  browser panel. Watch and the browser panel alert when a
  test command fails. The terminal Board also alerts when an idle or completed
  pull request becomes green or approved, an agent is blocked with nobody else
  working in that repository, or two sessions begin sharing a folder, branch,
  or issue. Failed tests and coding-agent conflicts stay visible for 30 seconds
  or until dismissed; the other Board alerts use the operating system's normal
  notification duration. `--no-notify` locks alerts off for that run.
- `show_filesystem_activity` changes visibility only. Background file and
  lifecycle activity is still collected and retained, and agent-attributed
  file/configuration events remain visible.
- `[spaces]` defines named folder groups such as `@review`.
- `[usage]` configures the optional ccusage executable and live refresh. The
  command is an argument array and is never interpreted by a shell. `agent`
  identifies untagged rows; current ccusage versions can report Claude Code,
  Codex, OpenCode, and Pi. Online pricing is the default; set `offline = true`
  to require cached pricing. Slow session scans are always separated by at
  least one minute even when a legacy `refresh_seconds` value is configured.

Activity is stored per project under
`~/.local/state/side-dog/projects/`. Set `SIDE_DOG_STATE_DIR` to use a different
private location. The append-only `events.jsonl` remains the authoritative
history. Beside it, Side Dog atomically maintains a versioned
`startup-summary.json` containing a validated 500-event tail plus the small
amount of GitHub, delivery, cursor, and up to 4,096 most-recent usage-session
keys needed at startup.
An unchanged history reuses that bounded summary; appended bytes are validated
from the saved offset. A missing, damaged, replaced, truncated, moved, or
version-incompatible summary is rebuilt from the JSONL history. The summary is
subject to the same privacy policy and never contains prompts, responses, raw
commands, output, diffs, or file contents.


## Saved spaces and privacy

`watch --save NAME` rewrites `~/.config/side-dog/spaces.toml`. Keep hand-authored spaces in `[spaces]` in `config.toml`. Do not store secrets in session titles or paths. Side Dog keeps validated metadata including model/session identifiers, relative paths and issue/PR links, never prompts, responses, full command bodies, stdout/stderr, diffs or file contents. The browser panel is local and binds to `127.0.0.1`.

State is disposable; deleting it removes recorded contribution history. Back up configuration separately. Malformed TOML falls back to defaults.
