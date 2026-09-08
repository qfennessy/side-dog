# Everyday use

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

### Startup progress

Interactive `side-dog watch` shows one transient status row before the normal
feed when startup takes more than a short moment. It names only the current
stage, includes elapsed time for a slow stage, and ends with the total startup
duration:

```text
Starting Side Dog...
Finding projects... 0.2s
Finding coding agents... 0.4s
Finding projects and worktrees... 1.1s
Loading recent activity... 1.8s
Refreshing optional context... 2.3s
Ready in 2.6s
```

Fast starts do not flash every stage. Optional context is labeled as optional
so a slow or unavailable usage/GitHub enrichment does not look like a failure.
The `--once` form remains synchronous and prints exactly one complete frame; it
does not emit the interactive progress row.

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

Report suspected vulnerabilities privately by following the
[security policy](https://github.com/qfennessy/side-dog/security/policy). Do not put security details or sensitive local
activity in a public issue.

When an observation fails the privacy policy, Side Dog keeps only a fixed
diagnostic. Repeated hook reports for one tool call are counted once beside
the matching session rather than becoming timeline rows. Compound commands
that finish with a recognized `gh` action use the command's reported exit
status; commits with the same message and author in separate worktrees of one
repository are folded into one display row.

### Status and color

Side Dog uses the same small visual vocabulary in the terminal and browser
panel. Blue marks navigation and selection, purple identifies an agent or
source, green means completed, amber means running or warning, red means
failed, and neutral text means idle or unknown. Each watched folder keeps one
muted color of its own, used as a thin bar at the left edge of its roster and
timeline lines, on its `[folder]` badge, and on its column title. The color is
a tint on plain text, never a filled block, so you can follow the folder
without mistaking it for status. A badge appears on the first line of a run
from one folder and again after a day divider; the rows inside a task card
never repeat it, and a title that already starts with the badge text drops it.

Color is never the only signal. Agent rows and pull-request lines start with
`●` (working, completed, failed, or an open PR) or `○` (idle, unknown, or
closed), and the state is also spelled out in a word. Timeline status uses
`✓` for completed, `…` for running, `!` for warning, `×` for failed, `○` for
idle, and a quiet `·` when Side Dog could not determine the state. These labels
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

## Watch and panel controls

The most useful controls are:

| Key | Action |
| --- | --- |
| `?` | Show or hide help |
| `/` | Filter visible activity |
| `v` | Open the View settings dialog |
| `E` | Show or hide folder, discovery-mode, and usage details |
| `e` | Switch between compact and expanded detail |
| `f` | Show all events, milestones, or files |
| `F` | Show or hide background activity, including files and lifecycle rows |
| `p` | Pause the display; collection continues |
| `i` | Show or fold idle agents |
| `u` | List or fold per-session usage rows under the expanded header |
| `r` | Reverse the timeline order |
| `b` | Switch from the terminal Watch view to Board |
| `w` | Switch from the terminal Board view to Watch |
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


## Board

Run `side-dog board` for the current session roster. Press `a` for recent contributions, `j`/`k` to select, `o` to open the selected work, and `w` to switch to Watch. In the roster, enter or `d` toggles detail, `i` opens linked issues, and `g` cycles grouping. `?` explains each view. Board quits directly with `q`; Watch uses a quit confirmation.

The panel at `/board` remains the live session roster. The terminal contributions view is described in [Which model worked on this PR?](contributions.md).
