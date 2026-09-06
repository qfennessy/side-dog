# Board: a machine-wide "who is working on what, where" view

## Status

Proposal. Nothing in this document is implemented yet.

## Problem

A working day now spreads coding agents across four surfaces at once:

- Claude Code in a Ghostty terminal, usually inside a Herdr pane
- Codex in a Ghostty terminal, usually inside a Herdr pane
- Codex Desktop, working in its own worktrees under `~/.codex/worktrees`
- Claude Code Desktop, working wherever it was pointed

`side-dog watch` answers "what is happening in this folder". It is a timeline,
one column per watched root, with agents seated under the folder they work in.
That is the wrong shape for the question that four surfaces raise several times
an hour: *which agent, on which surface, is on which issue and pull request
right now, and is it still moving?* Answering it today means alt-tabbing through
every window and reading each session's scrollback.

The board is a second view over the same collectors. Rows are sessions, not
folders. Each row says what surface the session lives on, which repository and
branch it is working in, the issue and pull request it is attached to, and
whether it is working, idle, blocked, or done.

## Goals

- One full-screen terminal view listing every live coding-agent session on the
  machine, regardless of which app launched it.
- Each row names the surface (Herdr pane, bare Ghostty window, Claude Desktop,
  Codex Desktop, VS Code) well enough to find that window.
- Each row links to a GitHub issue and pull request when one can be inferred
  from local evidence, with the PR's state, review, and CI at a glance.
- Two sessions on the same branch or worktree are flagged, because that is how
  an agent's push silently discards another agent's commit.
- Selecting a row shows that session's recent timeline using the existing
  renderer, so the board replaces alt-tab, not `watch`.
- Everything the board shows comes from files the collectors already read plus
  local Git and the existing `gh pr view` readback. No new network dependency.

## Non-goals

- Controlling agents. The board never sends keystrokes, signals, or prompts.
- Replacing `watch`. The board is a roster; `watch` stays the timeline.
- Perfect surface attribution. A best-effort label with an honest `unknown`
  beats a wrong guess.
- Reading prompts or transcripts for meaning. Issue linkage uses only numbers
  and URLs that the privacy layer already permits.

## What Side Dog already knows

The collectors already produce most of the row. This is why the board belongs
in Side Dog rather than in a new tool.

| Column | Source today | Where |
|---|---|---|
| Claude sessions, any surface | `~/.claude/sessions/*.json` with pid, session id, cwd; dead pids dropped | `claude_session_registry()` |
| Claude surface | registry `entrypoint`: `cli`, `claude-desktop`, `claude-vscode` | `CLAUDE_SURFACES` |
| Codex sessions, any surface | rollout headers with `cwd`, `id`, `originator` (Codex Desktop names itself) | `load_codex_session_identities()` |
| Herdr pane, tab, workspace | Herdr agent listing and `HERDR_*` environment | `load_herdr_identities()` |
| Working vs idle | transcript or rollout mtime within a working window | `claude_session_status()`, Codex `changed` |
| Model and effort | session metadata caches | `load_codex_metadata()`, Claude metadata cache |
| Branch per worktree | Git status per root | `WatchRootState.git_status` |
| PR number, state, review, CI, mergeability | `gh pr view --json` per root branch with quota backoff | `github_refresh_interval()`, `_SAFE_GITHUB_FIELDS` |
| Issue create/close | `gh issue` commands parsed to a number only | `_gh_issue_number()` |
| Machine-wide folder discovery | bare `side-dog watch` asks every integration for its working folders | `working_folders_loader` |
| Merge of every source into one identity per session | Herdr wins on pane data, files win on presence | `load_agent_identities()` |

The other seven integrations (Pi, OpenCode, Crush, Cursor, Grok, DeepSeek,
Cline, Antigravity) already return the same identity shape, so they appear on
the board for free.

## What is missing

Three gaps stand between the current identities and the board.

### 1. A session-centric roster

`load_agent_identities()` is called per root and its results are seated into
folder columns by `watch_root_column_identities()`. The board needs the inverse
join: discover every folder any agent is working in, load identities for each,
then flatten to one row per `SessionKey` carrying its root, working root, and
branch. `aggregate_watch_identities()` already does the first half for bare
`watch`; the flatten is new and pure.

### 2. Surface attribution beyond Herdr

Herdr names its panes. Claude's registry names the desktop app and VS Code. A
Claude Code or Codex session in a Ghostty window that Herdr did not open has no
label at all today, and a Codex CLI session has only its originator string.

Proposed resolution order, first hit wins:

1. Herdr identity present: `Herdr · <workspace> · pane <id>`.
2. Claude registry `entrypoint` is `claude-desktop` or `claude-vscode`: the
   mapped surface name.
3. Codex header `originator` starts with `Codex Desktop`: `Codex Desktop`.
4. Process ancestry. Claude's registry has the pid; for Codex, find the `codex`
   process whose cwd matches the rollout header (`lsof -a -d cwd -p` on macOS,
   `/proc/<pid>/cwd` on Linux). Walk parent pids with `ps -o ppid=,comm=`
   until the chain hits a known terminal or app: `ghostty`, `Herdr`, `Terminal`,
   `iTerm2`, `kitty`, `WezTerm`, `Code`, `Claude`, `Codex`. Label with the
   app name, plus the terminal tab title when the terminal exposes one.
5. `unknown`, shown literally.

Ancestry walks are cheap (a handful of `ps` calls per poll, cached per pid for
the life of the session) and read only public process metadata. They never read
command-line arguments, so no prompt text can leak into the label.

### 3. Issue linkage

Side Dog records `gh issue` commands but has no notion of "the issue this
session is on". Proposed sources, checked in order and shown with a confidence
marker (`#123` when confirmed, `#123?` when inferred):

1. Confirmed: the pull request's closing issues. Extend the existing
   `gh pr view --json` readback with `closingIssuesReferences`. Same call, same
   quota, one extra field.
2. Confirmed: a `gh issue develop <n>` or `gh issue view <n>` command event in
   this session within the last hour. Already parsed to a number.
3. Inferred: an issue number in the branch name: a leading number followed
   by a dash, `issue-` or `issue/` followed by a number, a trailing number after
   a dash or slash, or a `#`-prefixed number inside a path segment. Branch names
   are already recorded as safe events.
4. Inferred: `#(\d+)` or `/issues/(\d+)` in the PR title or body from the
   readback.
5. None: the column is blank.

Only integers and GitHub URLs cross the privacy boundary, matching how PR
numbers are handled today. Add `closing_issues` (a tuple of integers) to
`_SAFE_GITHUB_FIELDS` and review it there.

## The view

```
side-dog board                                        4 sessions · 2 repos · 14:32:07
─────────────────────────────────────────────────────────────────────────────────────
 AGENT   SURFACE                  REPO / BRANCH                ISSUE  PR             STATUS
 claude  Herdr · side-dog · p3    side-dog  feat/board          #142  #151 ✓ci ○rev  ● working 4s
 codex   Herdr · side-dog · p5    side-dog  fix/codex-cwd       #139  #150 ✗ci       ● working 12s
 codex   Codex Desktop            side-dog  codex/issue-139     #139? —              ○ idle 6m
 claude  Claude Desktop           herdr     main                —     —              ◌ blocked 2m
─────────────────────────────────────────────────────────────────────────────────────
 ⚠ two sessions on side-dog for #139: Herdr p5 (fix/codex-cwd) and Codex Desktop
─────────────────────────────────────────────────────────────────────────────────────
 codex · Herdr · side-dog · p5 · gpt-5-codex high                          #150 ✗ci
 14:31:52  test   failed   tests/test_cli.py::TestCodexCwd (2 failed)
 14:31:40  file   edited   side_dog/cli.py  +18 −4
 14:31:12  cmd    running  uv run python -m unittest tests.test_cli
 14:30:48  commit          fix codex cwd canonicalisation
─────────────────────────────────────────────────────────────────────────────────────
 j/k select  enter detail  g group by surface/repo  o open PR  i open issue  q quit
```

Row order: working first, then blocked, idle, done, newest activity first within
each. Done sessions age out after fifteen minutes, matching identity ageing
today. Sessions with no worktree (an agent started in `~`) still appear, with a
blank repo column, because a lost agent is exactly what the board is for.

Status glyphs reuse `AgentStatus`: working, blocked, idle, done, unknown. The
PR cell is compact: number, then CI (`✓` passed, `✗` failed, `…` pending),
review (`✓` approved, `○` waiting, `✗` changes requested), and `merged` or
`closed` when terminal.

### Grouping

`g` toggles between grouping by surface (the default, since the question is
"which window") and by repository (useful when two agents share a repo). A group
header is one dim line.

### Conflict strip

Between the roster and the detail pane, at most three warnings:

- two live sessions with the same worktree path
- two live sessions on the same branch of the same repository, different
  worktrees
- two live sessions attached to the same issue

Each names both surfaces so the person can decide which one to stop.

### Detail pane

The lower third shows the selected session's recent events using the existing
`render()` path with `--session` filtering, so the board inherits every event
kind, colour, and privacy rule. The pane is optional (`d` toggles) so the board
fits a short Herdr pane as a roster only.

### Actions

`o` and `i` open the PR or issue URL with the platform opener, the same way the
browser panel already does. There is no jump-to-window action in the first
version: Herdr may expose a focus command later and desktop apps do not, so the
surface label is the navigation aid.

## Interfaces

```
side-dog board [--poll SECONDS] [--github-poll SECONDS] [--once]
               [--group surface|repo] [--no-detail] [--no-color] [--width N]
```

`--once` prints one deterministic frame, as `watch --once` does, for tests and
for piping. Config gains an optional `[board]` table for the default grouping
and detail toggle, following the `[display]` pattern. The browser panel gets a
`/board` route rendering the same roster from the same SSE stream in a later
phase.

## Code shape

- `side_dog/board.py`, new. Pure functions only: `BoardRow` (frozen
  dataclass), `rows_from_identities()`, `surface_label()`, `linked_issue()`,
  `conflicts()`, `sort_rows()`, `render_board(rows, width, height, color)`.
  No I/O, so every table above is a unit test with a literal fixture.
- `side_dog/surfaces.py`, new. The process-ancestry probe, with the `ps` and
  `lsof` calls behind one function that tests patch. Cached per pid.
- `side_dog/cli.py`. The `board` subcommand, its argument parser, and the loop
  that reuses `aggregate_watch_identities()`, `PollCoordinator`, the Git status
  refresh, and the GitHub readback. The `gh pr view` field list grows by one.
- `side_dog/integrations.py`. `surface` joins `AgentIdentity` as a plain
  string field. `closing_issues` joins `_SAFE_GITHUB_FIELDS`. Both need the
  privacy review that the closed-set comment asks for.
- `side_dog/panel.py`, later phase. A `/board` route.

The board reads identities through the same `LazyCliCallable` registry entries
as `watch`, so a new integration appears on the board without board changes.

## Privacy

Nothing in a board row is new information, only a new arrangement, except for
two additions: the surface label and the linked issue number. The surface label
is derived from process names and Herdr ids, never from command lines or window
contents. Issue linkage yields integers and GitHub URLs, which the privacy layer
already admits for pull requests. Branch names already cross the boundary as
`branch` events. The conflict strip repeats surface labels and branch names
that are already on screen.

## Testing

- `tests/test_board.py`: row flattening from a fixture of mixed identities
  (Herdr Claude, Herdr Codex, Codex Desktop, Claude Desktop, one Pi session);
  surface resolution order; each issue-linkage source and its confidence
  marker; conflict detection for the three cases; sort order; `render_board()`
  with `color=False` at three widths, including the roster-only fit.
- `tests/test_surfaces.py`: ancestry walk against patched `ps` output for a
  Ghostty chain, a Herdr chain, an orphaned chain, and a dead pid.
- `tests/test_cli.py`: `side-dog board --once` on a temporary state directory
  produces a stable frame; `--group repo` reorders it.
- Registry tests: `test_integration_conformance.py` needs no change because the
  board adds no integration. `SAFE_EVENT_FIELDS` is untouched; the GitHub
  sub-mapping test gains `closing_issues`.

## Rollout

1. Roster only. `side-dog board --once` and the live loop from existing
   identities, Git branch, and the existing PR readback. Surface falls back to
   Herdr, Claude entrypoint, Codex originator, or `unknown`. No issue column.
   This alone answers "where is Codex Desktop working" today.
2. Issue linkage from the PR readback and branch names, with the confidence
   marker.
3. Process-ancestry surface attribution for bare terminals.
4. Detail pane, grouping toggle, conflict strip, `o` and `i` actions.
5. Browser panel `/board` route and a `[board]` config table.
6. Notifications keyed on board transitions: a PR went green while its session
   is idle, or a session went blocked with no one looking at it.

Each phase ships on its own and is useful without the next.

## Alternatives considered

**Extend `watch --layout columns`.** Columns are folders. Four surfaces on two
repositories give two columns with two agents each, and the surface question
still needs a header per agent. The row-per-session shape is the point.

**A separate tool.** The collectors, privacy layer, Herdr context, and GitHub
readback are the hard part and already exist here. A new tool would reimplement
nine session-file readers to draw one table.

**Read terminal window titles via accessibility APIs.** Accurate on macOS but
needs an accessibility permission prompt, does not exist on Linux, and reads
window contents that may include prompt text. Process ancestry gives the app
name without any of that.

## Open questions

- Should `board` be a mode of `watch` (`watch --board`) rather than a
  subcommand? A subcommand keeps the argument surface clean and matches
  `panel`, `usage`, and `tmux`.
- Codex Desktop worktrees all share a repository. Should the repo column show
  the canonical repository name and the worktree only in the detail pane?
- Does Herdr expose a focus-pane command? If so, `enter` on a Herdr row could
  jump to it, which would make the board the only window that needs a
  keyboard shortcut.
