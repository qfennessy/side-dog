# Side Dog

<p align="center">
  <img src="https://raw.githubusercontent.com/qfennessy/side-dog/main/docs/side-dog-logo.png" alt="A golden retriever watching an event timeline" width="360">
</p>

Side Dog is a narrow terminal timeline and local browser panel for watching
coding agents work. It shows edits, tests, Git activity, pull requests, issues,
and agent turns as they happen.

Side Dog was inspired by [Sundai Hack 138](https://sundai.club). Sundai Club is
a community for building and launching AI prototypes every Sunday.

![Side Dog watching an agent edit, test, commit, push, and open a pull request](https://raw.githubusercontent.com/qfennessy/side-dog/main/docs/side-dog-demo.gif)

## Install

Side Dog needs Git, Python 3.11 or newer, and a Unix-like system such as macOS
or Linux. On macOS, the easiest installation uses
[`uv`](https://docs.astral.sh/uv/getting-started/installation/):

```sh
brew install uv
uv tool install 'side-dog @ git+https://github.com/qfennessy/side-dog.git'
side-dog --version
```

On Linux, install `uv` using its official instructions, then run the same
`uv tool install` command.

If your shell cannot find `side-dog`, run `uv tool update-shell` and open a new
terminal.

A Git installation is a snapshot of the commit that uv installed. It does not
keep following `main`. Update your installed snapshot when you want newer Side
Dog changes:

```sh
uv tool upgrade side-dog
side-dog --version
```

If the update still runs an older commit, force uv to refresh its Git cache and
replace the installed tool:

```sh
uv tool install --force --refresh 'side-dog @ git+https://github.com/qfennessy/side-dog.git'
side-dog --version
```

From a Side Dog checkout, the repository helper performs that forced refresh,
checks Git and uv first, verifies the installed executable, and explains how to
put uv's tool directory on `PATH` when needed:

```sh
./scripts/install.sh
```

The helper installs the latest `main`. It does not modify your shell files or
run project setup.

If `side-dog --version` says `unknown command`, that installed copy predates
the version flag. Run the forced refresh above, then try the version command
again.

To remove Side Dog:

```sh
uv tool uninstall side-dog
```

Side Dog is not yet published on PyPI. Install it from GitHub as shown above.

## Try it

See a complete example without setting up a repository or starting an agent:

```sh
side-dog demo --panel
```

Use `side-dog demo --watch` to run the same tour in the terminal.

For a real project:

```sh
cd ~/src/my-project
side-dog doctor .
side-dog watch .
```

Use `side-dog panel .` for the browser panel. It opens a private local URL and
binds only to `127.0.0.1`.

`doctor` only checks your setup. It does not change any files. Run the guided
setup when you want Claude Code activity or optional Herdr details. Codex, Pi,
OpenCode, Crush, Cursor, Grok, DeepSeek Harness, Cline, and Antigravity CLI need
no Side Dog hooks:

```sh
side-dog setup .
```

## Coding agent support

| Agent | Finds and names sessions | Collects live activity | Setup |
| --- | --- | --- | --- |
| **Codex** | Yes, including terminal and Codex Desktop sessions | Yes, from Codex's local session stream | None |
| **Claude Code** | Yes, including terminal, desktop, and editor sessions | Yes, after project hooks are installed | Optional project hooks: run `side-dog setup . --claude`, then restart Claude Code |
| **Pi** | Yes | Yes, from Pi's local session files | None |
| **OpenCode** | Yes | Yes, from OpenCode's local SQLite store | None |
| **Crush** | Yes | Yes, from Crush's local SQLite stores | None |
| **Cursor Agent** | Yes, when launched through T3 Code | Yes, from T3 Code's projected activity store | None |
| **Grok Build** | Yes, when launched through T3 Code | Yes, from T3 Code's projected activity store | None |
| **DeepSeek Harness** | Yes | Yes, from Harness session logs | None |
| **Cline** | Yes, across CLI, editor, desktop, and background sessions | Yes, from Cline's local session store | None |
| **Antigravity CLI** | Yes | Yes, from Antigravity's local history and transcripts | None |

### Optional context

Herdr and T3 Code are not coding agents. They add context to the agents above.

| Context provider | What it adds | How Side Dog uses it | Setup |
| --- | --- | --- | --- |
| **Herdr** | Adds pane, tab, workspace, and terminal-title details | Routes activity to the right terminal context | Optional |
| **T3 Code** | Adds thread title, provider, status, and worktree details | Supplies projected activity for Cursor and Grok Build | Optional; no Side Dog hooks |

Most people do not need to set data-location variables. If an agent stores its
data somewhere custom, Side Dog honours `CODEX_HOME` for Codex,
`PI_CODING_AGENT_DIR` for Pi, `XDG_DATA_HOME` for OpenCode,
`CRUSH_GLOBAL_DATA` for Crush, `T3CODE_HOME` for T3 Code, `DSH_HOME` for
DeepSeek Harness, `CLINE_DIR`, `CLINE_DATA_DIR`,
`CLINE_DB_DATA_DIR`, and `CLINE_SESSION_DATA_DIR` for Cline, and
`ANTIGRAVITY_APP_DATA_DIR` or `GEMINI_HOME` for Antigravity CLI.

### Codex

Codex needs no hooks. Side Dog reads a privacy-filtered view of Codex's local
activity stream and identifies recent sessions by repository. This works for
Codex in a terminal, editor, or Codex Desktop. If Codex uses a custom data
folder, set `CODEX_HOME` to that folder.

### Antigravity CLI

Antigravity needs no hooks. Side Dog joins
`~/.gemini/antigravity-cli/history.jsonl` to each recent
`brain/<conversation-id>/.system_generated/logs/transcript.jsonl`, so sessions
are associated with the correct workspace and their turns, edits, commands,
tests, Git operations, and subagents appear as they happen. Set
`ANTIGRAVITY_APP_DATA_DIR` if Antigravity stores its application data
elsewhere. If that is not set, Side Dog also honours `GEMINI_HOME` as the
parent of Antigravity's data folders.

The collector uses stable per-step IDs and persistent cursors, so terminal and
browser views can run together without duplicating activity. A pending call is
replayed after restart until its result arrives. Command output is inspected
only for an exit code and is then discarded; prompts, responses, file content,
full commands, stdout, and stderr are never copied into Side Dog's feed.

### Claude Code

Side Dog can identify a live Claude Code session without setup. To see Claude's
tool activity, install Side Dog's hooks in each project:

```sh
cd ~/src/my-project
side-dog setup . --claude
```

Restart Claude Code after setup. Side Dog writes only to the machine-local
`.claude/settings.local.json` file. It preserves other hooks and does not change
the shared `.claude/settings.json` file.

Without these hooks, Claude's file changes still appear, but they are shown as
unattributed filesystem activity.

### Pi

Pi needs no hooks. Side Dog reads Pi's local session files to find the session,
model, reasoning level, and live activity. It honours `PI_CODING_AGENT_DIR` when
Pi stores its files somewhere other than the default location.

### OpenCode

OpenCode needs no hooks. Side Dog reads its local SQLite store to find the
session, model, reasoning variant, title, activity, and subagents. It shows
edits, tests, Git operations, and small markers for context tools such as read,
search, web fetch, and todo updates. Set `XDG_DATA_HOME` if OpenCode stores its
data under a custom data-directory parent.

### Crush

Crush needs no hooks. Side Dog reads Crush's machine-wide `projects.json`
index, then opens each indexed project's `crush.db` read-only. It uses the
indexed `data_dir` exactly as Crush recorded it, including configured absolute
locations, and attributes child-agent sessions to their top-level session.

Streaming tool rows are reread with a bounded overlap and stable event IDs, so
a call that finishes after Side Dog restarts converges without replaying old
activity. Side Dog selects only session metadata and relevant tool lifecycle
scalars; prompts, responses, reasoning, command output, result payloads, diffs,
and file snapshots are not copied into its state or panel feed. Set
`CRUSH_GLOBAL_DATA` to Crush's global data directory when Crush stores its
project index somewhere other than `~/.local/share/crush`.

### T3 Code, Cursor, and Grok

T3 Code is optional context, not an agent name. When T3 Code launches Codex,
Claude Code, or OpenCode, Side Dog uses the T3 thread title and worktree while
keeping that agent's native model, reasoning level, and activity reader.

Cursor Agent and Grok Build are supported when they run through T3 Code. Side
Dog reads only narrowly selected fields from T3 Code's local projected activity
store; it does not read messages, raw orchestration events, command output, or
provider logs. No hooks are needed. T3 Code normally stores its data under
`~/.t3`; set `T3CODE_HOME` only when T3 Code uses a different base directory.

### DeepSeek Harness

DeepSeek Harness needs no hooks. Side Dog reads event-sourced sessions from
`~/.dsh/sessions`, or `$DSH_HOME/sessions` when configured. Both Harness's
default Zstandard-compressed logs and diagnostic plain JSONL are supported.
Top-level sessions show their model, reasoning effort, status, edits, tests,
Git commands, subagents, and turn completion without storing prompts,
responses, command output, diffs, or file contents.

### Cline

Cline needs no hooks. Side Dog reads its shared SQLite session database and
structured message artifacts under `~/.cline/data`, or Cline's file-backed
session manifests when SQLite is unavailable. It honours `CLINE_DIR`,
`CLINE_DATA_DIR`, `CLINE_DB_DATA_DIR`, and `CLINE_SESSION_DATA_DIR`.

Side Dog names Cline sessions with their model, task title, and status, and
shows editor, patch, command, test, Git, and subagent activity. Child-session
activity is attributed to its top-level session. Prompts, responses, tool
output, patch contents, full shell commands, and file contents are not copied
into Side Dog's event log.

### With or without Herdr

[Herdr](https://herdr.dev) is optional. Side Dog works without it by reading
each coding agent's local session data.

Herdr adds information that agent files do not have: the terminal pane, tab,
workspace, and terminal title. When Herdr and an agent file describe the same
session, Side Dog keeps Herdr's terminal details and adds the model and
reasoning information from the agent.

Inside Herdr, a bare command follows every live Herdr agent folder across all
workspaces:

```sh
side-dog watch
```

Outside Herdr, the same command discovers active agent folders on the machine.
To require Herdr discovery, use `--herdr`. If Herdr is unavailable, Side Dog
stops and explains the problem instead of silently watching the wrong folders.
To restrict the watch to the Herdr workspace containing the current pane, use
`side-dog watch --workspace`.

## What Side Dog shows

The timeline reports:

- file and configuration writes, with lines added and removed since the last
  commit;
- tests starting, passing, and failing;
- branch, worktree, commit, and push operations;
- pull requests opening, checks running, checks passing or failing, and merges;
- issues being created, closed, or reopened;
- failed commands, identified only by program name; and
- agent sessions, turns, and subagent activity.

An authenticated `gh` CLI lets Side Dog confirm pull-request state, CI, reviews,
mergeability, and merges. Without it, local agent, file, test, and Git activity
still works.

GitHub readback is per watched folder, not per agent. Active pull requests use
a 60-second default interval. Branches without a pull request and partial/error
states back off to at least five minutes; closed and merged pull requests back
off to at least 15 minutes. A pull-request command or branch switch still
triggers an immediate readback. The browser panel always uses this schedule.
For terminal `side-dog watch`, use `--github-poll 0` to disable readback.

Side Dog is an activity display, not an audit log or a security boundary. It
stores short event metadata, but never stores prompts, responses, file
contents, diffs, full shell commands, stdout, or stderr.

When an observation fails the privacy policy, Side Dog keeps only a fixed
diagnostic. Repeated hook reports for one tool call are counted once beside
the matching session rather than becoming timeline rows. Compound commands
that finish with a recognized `gh` action use the command's reported exit
status; commits with the same message and author in separate worktrees of one
repository are folded into one display row.

### Token usage and estimated spend

Side Dog can optionally read [ccusage](https://ccusage.com/) JSON reports and
show a compact token and API-equivalent cost summary in `watch` and the browser
panel. The dependency stays optional: when `ccusage` is absent, normal activity
collection continues and `side-dog doctor` reports token usage as unavailable.

Install `ccusage` separately so its executable is on `PATH`, then inspect a
report without starting the live display:

```sh
side-dog usage daily
side-dog usage monthly --since 2026-01-01 --json
side-dog usage session --root .
```

Use `--agent`, `--since`, and `--until` to narrow a report, `--no-cost` when
only token counts should be shown, or `--cost-mode` to pass an explicit
ccusage cost mode. Cost is labelled as estimated, recorded, unpriced, omitted,
partial, unavailable, or stale rather than silently treated as exact. The
`API est` figure applies public API prices to token counts in local agent logs.
It is useful for comparing activity, but it is not a subscription bill and may
not match a provider invoice.

`--root` is supported only with the `session` view, where Side Dog can filter
to sessions it has associated with that folder. ccusage does not expose enough
project information to scope daily or monthly reports honestly, so those
combinations are rejected with an explanatory message.

The live header combines two independently captured views into one gauge:

```text
$23.00 this block ▰▰▰▰▱▱▱▱ 2h 26m left · $10.48/hr · today $88.95 · as of 10:33
```

The bar shows elapsed time in the active five-hour block. Its cost, pace, and
time left are machine-wide; today's figure is scoped to the shown folders.
The single `as of` time is the oldest capture used by the line. On narrow
terminals the line drops the capture time, today's total, half of the bar, and
then pace, in that order. If pricing is partial, the unpriced model and token
count replace the capture time so the gap stays visible.

When a figure is missing, the line says why rather than calling everything
unavailable. While the first reports are still arriving it reads `usage
loading`, and a figure that arrives first is shown beside `today loading`. A
missing `ccusage` reads `ccusage not installed`, a disabled reporter reads
`usage off in config`, and a quiet five-hour window reads `no active block`.
Any other failure reads `unavailable`: `ccusage` error text is never copied
into the pane. No bar is drawn without a block report.

Expanded usage details retain the three underlying views:

- **Today** totals provider-qualified ccusage sessions associated with the
  shown root or roots since the start of the current day.
- **Current 5-hour window** is machine-wide. It covers all local agent usage
  ccusage can identify and shows the API estimate per hour plus time left in
  the rolling window.
- **Tracked lifetime** totals the matched sessions Side Dog has seen for the
  shown root or roots. “Tracked” is deliberate: this is not an account-wide
  billing ledger.

The terminal status bar names Side Dog and its installed version, describes
the visible scope as a folder name, `all N folders`, or `N of M folders`, and
shows how many agents are working. The clock stays at the right edge. In a
narrow pane, the working count is removed first, then scope, then version;
the Side Dog name and clock remain for as long as the pane can fit them.

An all-folder view aggregates today's and tracked-lifetime associations across
its shown roots. The five-hour window remains machine-wide, regardless of
focus.

The terminal roster and the browser's expanded usage details show privacy-safe
Side Dog task labels and active/idle state. The terminal's expanded header
(`E`) reveals folder paths, discovery mode, usage contributions, lifetime
totals, and last activity. Neither view exposes raw session IDs. You do not
need to terminate an agent session to see its estimate: the active block is
refreshed about every 10 seconds, while the more expensive session scans are
staggered and refreshed every few minutes. Finished sessions stay in **Tracked
lifetime**.

Side Dog tries current online model prices first and falls back to ccusage's
cached price list. Each snapshot records the pricing source and capture age;
unknown models are named with their unpriced token count, and failed or old
refreshes retain the last good values marked stale. Usage snapshots remain in
memory, so pausing freezes the displayed values and resuming catches up. Raw
ccusage rows are never written to Side Dog's event history or sent to a panel.

### Status and color

Side Dog uses the same small visual vocabulary in the terminal and browser
panel. Blue marks navigation and selection, purple identifies an agent or
source, green means completed, amber means running or warning, red means
failed, and neutral text means idle or unknown. Each watched folder keeps one
stable color in the left gutter shared by its roster and timeline lines, so you
can follow the folder without mistaking its color for status.

Color is never the only signal: the roster uses `● working`; timeline status
uses `✓` for completed, `…` for running, `!` for warning, `×` for failed, `○`
for idle, and `?` when Side Dog could not determine the state. These labels
remain in plain and redirected output. Terminal colors use the terminal theme;
the browser panel provides matching light and dark themes.

## Choose what to watch

Watch one project and its active worktrees:

```sh
side-dog watch ~/src/my-project
```

Watch several folders together:

```sh
side-dog watch ~/src/project ~/src/project-issue-42 ~/src/another-project
```

Run `side-dog watch` with no folders to discover where agents are working. Run
`side-dog watch .` when you want to pin Side Dog to the current project.

By default, active worktrees join the display and finished ones leave it. Use
`--no-follow-worktrees` to watch only the folders you named.

Save a group of folders and open it later:

```sh
side-dog watch ~/src/project ~/src/project-issue-42 --save review
side-dog watch @review
```

Side Dog watches at most eight folders by default and gives space to the
busiest ones. Folders named on the command line or pinned in the configuration
are not removed.

The terminal roster uses one line for a folder with one active agent. When a
repository has multiple watched worktrees, it groups them under the repository
name and labels each row by branch or task purpose; directory hashes are never
used as names. Folder names are bold in color, while model/effort and age are
dimmed; status still has both a word and a glyph. Idle sessions fold into one
summary line by default. Lifecycle bookkeeping is collected but hidden with
the background activity toggle, and recent resumed/ended times remain on the
roster.

## Terminal and panel controls

The most useful controls are:

| Key | Action |
| --- | --- |
| `?` | Show or hide help |
| `/` | Filter visible activity |
| `E` | Show or hide folder, discovery-mode, and usage details |
| `e` | Switch between compact and expanded detail |
| `f` | Show all events, milestones, or files |
| `F` | Show or hide background activity, including files and lifecycle rows |
| `p` | Pause the display; collection continues |
| `i` | Show or fold idle agents |
| `r` | Reverse the timeline order |
| `h` | Switch the browser panel between timeline and highway views |
| `Tab`, `1`–`9` | Focus a watched folder |
| `a` | Show all watched folders |
| `C` | Open the browser panel from the terminal view |
| `q` | Open the quit confirmation (`No` is selected by default) |

The day divider repeats the active timeline controls as key hints: `r` for
order and `e` for detail. It adds `f` only when the event filter is narrower
than all events, and shows the off-screen activity count with its direction.

The first Ctrl-C opens the same confirmation. Press Ctrl-C again while it is
open to quit immediately.

Run `side-dog watch --help` or `side-dog panel --help` for every option.

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
show_filesystem_activity = false  # background files and lifecycle rows are hidden by default
limit = 8

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
- `[display]` sets the initial view. Interactive changes are remembered.
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
private location.

## Other commands

- `side-dog setup [PROJECT]` guides optional Claude and Herdr setup.
- `side-dog doctor [PROJECT]` checks readiness without changing files.
- `side-dog usage [daily|monthly|session]` reports local tokens and estimated
  API-equivalent cost through optional ccusage JSON output.
- `side-dog init [PROJECT]` directly installs Claude hooks; `setup` is preferred.
- `side-dog tmux [PROJECT]` opens the terminal view in a right-side tmux split.
- `side-dog demo --panel` and `side-dog demo --watch` run the synthetic tour.
- `side-dog help [COMMAND]` shows command help.

## Develop from a checkout

Release preparation uses one canonical stable SemVer version and never tags or
publishes merely because that version changes. Maintainers should follow
[the release guide](docs/releasing.md).

```sh
git clone https://github.com/qfennessy/side-dog.git
cd side-dog
uv sync
uv run side-dog demo --watch
uv run python -m unittest discover -s tests -q
```

Side Dog is licensed under the [MIT License](LICENSE).
