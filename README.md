# Side Dog

Side Dog is a narrow terminal timeline and local browser panel for watching
coding agents work. It currently targets Codex: Side Dog reads a
privacy-filtered view of Codex's local activity stream, while Herdr associates
the live Codex session with each watched root. The timeline shows:

- file and configuration writes;
- running, passed, and failed test commands;
- branch, worktree, commit, and push operations;
- pull request creation and merge operations;
- issue creation, closure, and reopening; and
- agent session and turn boundaries.

It is deliberately small: Python 3.11+, one lightweight terminal-width
dependency, an append-only JSONL activity feed, and an ANSI terminal UI that
uses the full pane width by default while remaining readable in a narrow split.
Use `--width 42` when an explicit cap is useful.

## Support status

- **Codex:** ready for local use and the installation path documented below.
- **Claude Code:** **not ready.** Experimental hook and parser code exists in
  this repository, but installation, compatibility, attribution, and event
  completeness are not yet supported. Do not run `side-dog init` or rely on
  Side Dog as a Claude activity record yet.

Side Dog is an activity visualization, not an audit or security boundary. It
stores short event metadata but never stores prompts, responses, file contents,
diffs, full shell commands, stdout, or stderr.

## Install

Prerequisites:

- Python 3.11 or newer;
- [uv](https://docs.astral.sh/uv/getting-started/installation/);
- Herdr, with the Codex session running inside a Herdr pane; and
- Git, plus an authenticated `gh` CLI if pull-request readback is wanted.

Clone and verify Side Dog from its checkout:

```sh
git clone https://github.com/qfennessy/side-dog.git
cd side-dog
uv sync
uv run side-dog help
```

To make `side-dog` available outside this checkout:

```sh
uv tool install .
side-dog help
```

After pulling a newer checkout, update that installation with
`uv tool install --force .`. Remove it with `uv tool uninstall side-dog`.

## First run with Codex

Codex requires no hook installation. Start Codex in a Herdr pane rooted in the
repository or worktree you want to observe. Split that pane to the right, open
a normal shell in the new pane, and run:

```sh
side-dog watch /absolute/path/to/project
```

Use an explicit project path so the watcher pane's own working directory cannot
accidentally select the wrong root. `watch` attaches to the active Codex session
that Herdr associates with that canonical root. To use Chrome instead, run:

```sh
side-dog panel /absolute/path/to/project
```

When working directly from the checkout without `uv tool install`, prefix those
commands with `uv run`, for example `uv run side-dog watch .`.

On a successful first run, the header names the watched root and shows the
matched Codex session, model, effort, and activity state. If it keeps saying it
is waiting for an agent, confirm that Codex is running in Herdr and that the
explicit path resolves to the same repository or worktree as the Codex pane.

Press `?` in the Side Dog pane for a quick guide. Press `e` to switch between
compact and expanded detail, `f` to cycle all/milestone/file views, and `p` to
pause or resume display updates without stopping collection. Press `r` to
toggle between the default newest-first timeline and an oldest-first feed with
new activity at the bottom. Press `?` again or `Esc` to return to the timeline.
After any display-changing key, Side Dog briefly shows a non-modal explanation
of the resulting view above the timeline. The notice disappears after two
seconds; another key immediately replaces it and restarts the timer. Collection
and polling continue while the notice is visible, including when the displayed
timeline is paused.

Watch several repositories or worktrees in one pane by passing each canonical
root explicitly:

```sh
side-dog watch \
  ~/src/project \
  ~/src/project-issue-42 \
  ~/src/another-project
```

Side Dog keeps an independent filesystem snapshot, JSONL cursor, Git state,
agent identity set, and GitHub status for every root. It merges only copied
display records, leaving every project's append-only JSONL feed unchanged.
Events are labeled with the root's PR number, branch, or folder name. Press
`a` to show all roots, `Tab` to cycle a focused root, or `1` through `9` to
jump directly to a root. Existing `r`, `e`, `f`, and `p` controls apply to the
consolidated timeline. In a color terminal, the header renders roots with a
working agent in bold, subtly dims roots whose agents are all idle or done, and
leaves roots with unknown activity neutral. Timeline event styling is unchanged.

In a wide terminal, the default `auto` layout gives every root its own column
when each column can remain at least 42 characters wide. Each column has its
own root, GitHub, agent, date, and timeline context; events never cross between
columns. Resize narrower to return automatically to the consolidated timeline,
or select a layout explicitly:

```sh
side-dog watch . ../worktree-a --layout columns
side-dog watch . ../worktree-a --layout timeline
```

Focusing a root with `Tab` or `1` through `9` uses the full pane for that root;
press `a` to restore all root columns. The temporary view notice says which root
is focused or whether all roots are shown as columns or one consolidated
timeline.

## Local web panel

Use the same activity model in a narrow browser window with `panel`:

```sh
side-dog panel .
side-dog panel ~/src/project ~/src/another-project
```

Side Dog binds only to `127.0.0.1` on a free port, adds a random unguessable
path to the URL, rejects non-local Host headers, and disables browser caching.
It serves event metadata only—never file contents or command bodies. Chrome and
Chromium open in a 360×1040 app window when available; otherwise Side Dog uses
the default browser. Pass `--no-open` to print the local URL without launching
a window, or `--port PORT` to select a specific local port.

The panel says `Watching:` before every repository name so the displayed Git
branch and commit cannot be mistaken for the running Side Dog version. Its
auto layout places watched roots side by side when each has at least 300 pixels,
then falls back to a stack as the window narrows. Select `columns` or `stack`
to request that layout; columns still fall back to a stack when roots would be
too narrow. The `e`, `f`, `p`, `r`, `a`, `Tab`, and `1`–`9` controls match the
terminal feed: expand details, filter, pause, reverse order, and focus roots.
Buttons and keyboard controls show the same two-second view explanation as the
terminal, and rapid changes replace the prior message instead of queuing it.
PR, issue, and commit events link to GitHub when an origin URL is available.

Press `h` (or click `highway`) for the live pulse view. Each root keeps its own
four lanes—files, tests, Git, and GitHub—with the NOW line at the top. Completed
events age downward at a constant rate; `s` cycles `0.5×`, `1×`, and `2×` speed.
Running operations stay on NOW with a hold tail proportional to elapsed time,
then resolve to `PASS`, `MISS`, or neutral when their matching completion
arrives. Success increases the combo, failure resets it, and unknown status is
neutral: it neither increments nor resets the combo. `p` and the operating
system's reduced-motion preference show the identical static pulse strip and
schedule no animation frames. Press `h` again to return to the default row
timeline.

Each watched root receives a stable muted identity color based on its
command-line position. That background color is attached directly to the root
name in the summary or column header and to the matching `[root]` label on its
agents and events; Side Dog does not use a detached strip that could be mistaken
for progress. PR/CI text colors are a separate system: blue means open, yellow
means pending, green means clean or merged, and red means failed. Text labels
are always retained. The 12-color root palette cycles predictably for larger
root sets; `--no-color` and redirected output omit every ANSI accent while
keeping the same root labels and layout.

All agent, filesystem, Git, test, and GitHub events appear in one newest-first
timeline. The display fills the available pane height with retained semantic
events and reports how many continue below the viewport. Codex-originated
events carry a compact `Codex` label; experimental Claude records may carry a
`Claude` label but are not supported yet.
Reversing the display changes complete semantic-unit order only: compact groups,
expanded detail, filters, and paused snapshots retain their normal behavior and
the append-only JSONL order and contents are unchanged.
Filter the timeline by Herdr pane ID, task title, or agent session-ID prefix:

```sh
side-dog watch . --session wB:p1
```

To see the display before starting an agent, run this in one terminal:

```sh
side-dog watch .
```

and this in another:

```sh
side-dog demo .
```

## Command reference

Every command accepts `-h` or `--help`. `side-dog help` is the same as
`side-dog --help`, and `side-dog help watch` is the same as
`side-dog watch --help`. Missing and unknown commands print the complete command
list plus those recovery hints. Paths default to the current directory, and
multiple watch or panel roots must be listed explicitly.

### `init`

`side-dog init [PROJECT] [--print]` installs the machine-local Claude Code
hooks for `PROJECT`. **Claude support is not ready, so this command is currently
for development only.** Codex does not use it: `watch` and `panel` read its
activity stream directly.

| Argument | Default | Meaning |
| --- | --- | --- |
| `PROJECT` | `.` | Project whose `.claude/settings.local.json` is merged. |
| `--print` | off | Print the merged settings without writing them. |

### `hook`

`side-dog hook [--root ROOT]` is the experimental internal Claude hook receiver
installed by `init`; users should not invoke it. Claude support is not ready.

### `watch`

`side-dog watch [ROOT ...] [OPTIONS]` renders the live terminal feed.

| Argument or option | Default | Meaning |
| --- | --- | --- |
| `ROOT ...` | `.` | One or more canonical project or worktree roots to consolidate. |
| `--width WIDTH` | `0` | Maximum render width; `0` uses the full terminal pane. |
| `--poll SECONDS` | `0.75` | Filesystem scan interval. |
| `--session VALUE` | unset | Filter by Herdr pane, task title, or session-ID prefix. |
| `--github-poll SECONDS` | `15.0` | Verified PR refresh interval; `0` disables GitHub readback. |
| `--layout auto\|timeline\|columns` | `auto` | Multi-root layout; columns fall back when roots are too narrow. |
| `--once` | off | Print one frame and exit instead of watching. |
| `--no-color` | off | Omit ANSI color and root accents. |

Terminal controls:

| Key | Result |
| --- | --- |
| `?` | Open or close the in-pane help. |
| `Esc` | Close help and return to the timeline. |
| `e` | Toggle compact grouped history and expanded event detail. |
| `f` | Cycle `all` → `milestones` → `files` → `all`. |
| `p` | Pause or resume display updates; collection continues while paused. |
| `r` | Toggle newest-first and oldest-first ordering. |
| `a` | Show all watched roots. |
| `Tab` | Focus the first root or cycle the focused root. |
| `1`–`9` | Focus that root by command-line position. |
| `Ctrl-C` | Quit the watcher. |

The `e`, `f`, `p`, `r`, `a`, `Tab`, and number controls briefly explain the
resulting view. `a`, `Tab`, and `1`–`9` matter only when multiple roots are
watched.

Outcome markers are `✓` success, `×` failure, `…` running, and `?` unknown or
unconfirmed. An unknown result means Side Dog observed the action but could not
safely determine its final outcome.

### `panel`

`side-dog panel [ROOT ...] [OPTIONS]` streams the same display model to a local
browser panel.

| Argument or option | Default | Meaning |
| --- | --- | --- |
| `ROOT ...` | `.` | One or more canonical project or worktree roots to display. |
| `--port PORT` | `0` | Loopback port; `0` selects a free port. |
| `--poll SECONDS` | `0.75` | JSONL polling interval. |
| `--no-open` | off | Print the private local URL without opening a browser window. |

Panel buttons select `auto`, `columns`, or `stack` layout and expose `h`, `s`,
`e`, `f`, `p`, `r`, and `a`. `h` toggles the live four-lane pulse view and `s`
cycles its scroll speed; the default remains the row timeline. The same letter
keys work from the keyboard; `Tab` cycles a focused root and `1`–`9` jumps to
one. Auto layout uses columns while every visible root has at least 300 pixels,
otherwise it stacks them. Explicit columns also fall back when too narrow;
focusing a root gives it the full panel. Every display control shows the same
replacing two-second explanation as the terminal.

### `tmux`

`side-dog tmux [PROJECT] [--width WIDTH]` opens `watch` in a right-side tmux
split. `PROJECT` defaults to `.` and `--width` defaults to `42` columns. This
command requires an existing tmux session; Herdr users should create a normal
right-side shell pane and run `side-dog watch` there.

### `demo`

`side-dog demo [PROJECT]` appends representative file, test, Git, PR, config,
and issue activity to `PROJECT`'s feed. `PROJECT` defaults to `.`. Run it beside
`watch` or `panel` to preview the display before an agent starts.

## How collection works

For Codex, both `watch` and `panel` use Herdr only to discover the active session
ID and watched root, then tail Codex's own append-only session stream. Side Dog
accepts normalized command, file-change, and subagent lifecycle records and
sends them through a privacy-filtered event normalizer. A terminal watcher and
browser panel may run together: stable source IDs prevent duplicate JSONL
events, while persistent cursors recover earlier activity once and resume from
the last processed transcript position.

### Experimental Claude internals

**Claude support is not ready. The implementation below is present for
development and should not be treated as an installable or complete feature.**

`side-dog init` merges observational hooks into
`.claude/settings.local.json`. It does not touch the shareable
`.claude/settings.json`. Existing hook entries are preserved, while a previous
Side Dog entry is replaced so re-running initialization is safe.

The synchronous `PreToolUse` hook records that an operation is starting. It
never claims a write succeeded and never blocks Claude. `PostToolUse` confirms
success; `PostToolUseFailure` closes the same timeline item as failed. Direct
edit coverage includes `Write`, `Edit`, and `NotebookEdit`.

Each hook records Claude's native session and turn plus any Herdr
workspace/tab/pane environment. The watcher reconciles those fields with
Herdr's live session snapshot, so resumed Claude sessions keep the correct pane
and visible task-title label. Hook commands pin the initialized project root;
changing Claude's child-process working directory does not split the feed.

Side Dog intentionally does not register Claude's `WorktreeCreate` hook:
Claude delegates worktree creation to that hook, so treating it as a passive
notification could break isolation. Worktrees created with `git worktree`
through Bash are observed normally.

The visible watcher also scans the project for changed files. This is the
fallback for shell redirects, generators, formatters, and other writes that do
not use a direct edit tool. Common dependency, VCS, cache, and build directories
are excluded. This is a lightweight visualization, not a complete audit log or
filesystem enforcement boundary.

Events are stored independently per canonical project root under
`~/.local/state/side-dog/projects/` with owner-only permissions. Set
`SIDE_DOG_STATE_DIR` to choose another private location. Side Dog stores only
short activity metadata; it does not store source contents, shell output, full
shell commands, prompts, or transcripts.

Native Codex ingestion follows the same boundary. Side Dog never persists
Codex stdout, stderr, diffs, prompts, responses, or arbitrary command text. It
records only relative file paths and a conservative allowlist of test, Git,
pull-request, and issue operation summaries. Unrecognized commands are ignored.

Related edits, tests, commits, pushes, PRs, and merges from one agent turn render
as one Agent task sequence with elapsed time:

```text
12:13  ┌ Codex · Agent task · 2m03s
       Edit ×19 → Tests ✓ → Commit 200d661 → Push ✓ → PR #3
```

Passive filesystem activity collapses into time-bounded bursts with change and
path totals plus the busiest paths. Expanded detail restores individual
rows. This is presentation-only: the append-only JSONL keeps every raw event.
Atomic events use one display line, including their cropped target or title.
Only compressed filesystem bursts and Agent task sequences use continuation
lines, so long PR and commit histories use the pane height efficiently.
Each displayed local date group has a full-width marker, with the current day
labeled `Today`; compacted file activity never crosses a local midnight. This
is derived during rendering, so filtering, reconnecting, and expanding history
cannot add records to or modify the raw JSONL feed.
GitHub refreshes that do not change the PR's visible title, lifecycle, CI,
review, or mergeability state are suppressed; real status transitions remain.

Common labels are compacted in the display (`File changed` becomes `changed`,
for example) so paths and operation targets receive the remaining width.
Issue and pull-request creation events safely retain their `--title` value, but
not their body or full shell command. Verified PR banners and lifecycle events
also use the title returned by GitHub.
Conventional prefixes such as `feat:`, `fix(sidebar):`, and `chore!:` are
removed from displayed commit subjects and pull-request titles to preserve
space for useful content. Their original values remain unchanged in JSONL and
GitHub status data.

A Git status line near the top always shows the watched worktree's current
branch and HEAD commit. Side Dog also polls Git directly and emits a commit or
branch event when that state changes, including changes made outside the direct
agent activity stream.

Herdr's active agent snapshot identifies whether a running pane belongs to
Codex or Claude and associates its native session with a watched root. Side Dog
reads only the latest local session's `model` and `effort` metadata for either
agent and displays them with the Herdr task label and running status; it does
not copy prompts, responses, or transcript content into the activity feed.
Claude identification remains experimental and unsupported.

After a PR command, Side Dog polls `gh pr view` for the PR attached to the
current branch. The sticky banner and versioned lifecycle event distinguish a
successful command from a confirmed PR and show CI, review, mergeability, and
open/closed/merged state. Blue means open or draft, yellow means pending or
partially observed, green means clean or merged, and red is reserved for a
failure, conflict, or changes-requested state. The default poll interval is 15
seconds:

```sh
side-dog watch . --github-poll 30
side-dog watch . --github-poll 0  # disable GitHub readback
```

## Current limits

- Shell activity is recognized conservatively. Unrecognized commands are not
  logged, avoiding accidental persistence of secrets in command arguments. If
  a compound command makes one shell exit code ambiguous, the recognized
  operation is shown as finished with an unknown outcome rather than passed or
  failed.
- GitHub readback requires an authenticated `gh` CLI and follows only the PR
  attached to the watched worktree's current branch. A definitive no-PR result
  clears old branch context; transient failures preserve it as visibly
  `PARTIAL` rather than claiming clean coverage.
- Issue activity is immediate when Codex invokes a recognized `gh` command;
  issues are not yet reconciled from GitHub after the command.
- The filesystem fallback uses polling while the pane is open. Very large
  repositories may want a native watcher later.
- Codex events depend on the current local session-stream record shapes. The
  Side Dog event schema is versioned as `side-dog-activity-v1` so collectors can
  evolve independently of the display.
- Codex model and effort discovery depends on the current local session-log
  shapes. If unavailable, the pane reports `model ?` or `effort ?` rather than
  guessing. Claude hook and metadata compatibility remains unfinished.

The design borrows several lessons from Quodet: a versioned append-only event
boundary, machine-local hook configuration, canonical-root/session scoping,
explicit direct-edit coverage, and honest separation between attempted edits,
confirmed writes, and after-the-fact filesystem observation.
