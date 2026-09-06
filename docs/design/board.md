# Board: a machine-wide "who is working on what, where" view

## Status

Phases 1 through 4 of the rollout below are implemented (#177, #179, #178,
and the phase 4 pull request). Phases 5 and 6 are open as #173 and #174.
Where the implementation departed from this document, the note is inline.

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
then flatten to one row per session carrying its root, working root, and
branch. The row identity is the `SessionKey` when the session id is known and
otherwise the source key the loader already uses, such as `pane:<id>` for a
Herdr pane whose agent has not reported a session yet.
`load_agent_identities()` keeps those pane-keyed entries apart on purpose, and
`SessionKey` would fold every one of them into `<provider>:unknown`, hiding
all but one live pane. `aggregate_watch_identities()` already does the second step for bare
`watch`, but only over `WatchRootState` objects that already exist, and
`discovered_watch_roots()` truncates discovery to `WATCH_ROOT_LIMIT` (eight)
folders because the timeline has to fit them as columns. The board has no
columns, so it calls discovery with the cap disabled and builds a state per
discovered root before aggregating. `discovered_watch_roots()` already treats
`limit=None` as "read the config", so `None` cannot also mean unlimited; it
gains a keyword-only `uncapped: bool = False` that skips the truncation
entirely, and only the board passes it. A machine
with nine active folders otherwise loses every session in the ninth silently,
which is the opposite of what the board promises. The flatten is new and pure.

### 2. Surface attribution beyond Herdr

Herdr names its panes. Claude's registry names the desktop app and VS Code. A
Claude Code or Codex session in a Ghostty window that Herdr did not open has no
label at all today, and a Codex CLI session has only its originator string.

Proposed resolution order, first hit wins:

1. Herdr identity present: `Herdr · <workspace> · pane <id>`.
2. Claude registry `entrypoint` is `claude-desktop` or `claude-vscode`: the
   mapped surface name.
3. Codex header `originator` starts with `Codex Desktop`: `Codex Desktop`.
4. Process ancestry. Claude's registry has the pid. Codex's rollout header
   does not, so the session must be tied to a process first: prefer the
   process holding the rollout file open (`lsof -t <rollout path>` on macOS,
   `/proc/*/fd` on Linux), since Codex appends to that file for the life of
   the session; otherwise fall back to the `codex` process whose cwd matches
   the rollout's `cwd`, and only when exactly one does. Two Codex sessions in
   one worktree is the conflict case the board exists to show, and guessing
   between them would put a wrong window on the row, so an ambiguous match
   resolves to `unknown`. With a pid in hand, walk parent pids with
   `ps -o ppid=,comm=` until the chain hits a known terminal or app:
   `ghostty`, `Herdr`, `Terminal`, `iTerm2`, `kitty`, `WezTerm`, `Code`,
   `Claude`, `Codex`. Label with the app name, plus the terminal tab title
   when the terminal exposes one.
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
   this session within the last hour whose status is `success` and whose
   command was a single `gh` invocation. A failed command (a mistyped number,
   an expired login) still produces an event, and it must not outrank an
   inferred source. A compound command is refused outright: the classifier
   emits at most one event per rule, `_gh_issue_number()` returns the first
   operand, and `_compound_event_status()` resolves the outcome by kind, so
   `gh issue view 123 || gh issue view 456` would report a successful 123 when
   only 456 succeeded. The normalizer attaches issue metadata only when
   `shell_command_is_compound()` is false; per-stage operands and outcomes
   could relax that later, but not in phase 2. The command normalizer only recognises
   `gh issue create`, `close`, and `reopen` today, and
   `_gh_issue_stage_material()` ignores `view` and `develop`, so this source
   does not exist yet. Phase 2 extends both to `view` and `develop`, reducing
   the operand to a number exactly as `_gh_issue_number()` does for `close`.
   `_gh_issue_operand()` skips a fixed set of value-taking flags, and the new
   verbs bring their own: `develop` takes `--base`/`-b` and `--name`/`-n`,
   `view` takes `--jq`/`-q`, `--template`/`-t`, and `--json`. Without them
   `gh issue develop --base 123 456` would confirm issue 123. The flag set
   becomes per-verb, with tests that put a number in each flag's value.
3. Inferred: an issue number in the branch name: a leading number followed
   by a dash, `issue-` or `issue/` followed by a number, a trailing number after
   a dash or slash, or a `#`-prefixed number inside a path segment. Branch names
   are already recorded as safe events.
4. Inferred: `#(\d+)` or `/issues/(\d+)` in the PR title from the readback.
   The title is already fetched; the body is not, and `GITHUB_PR_FIELDS`
   deliberately leaves it out. Reading the body would mean fetching free text
   and reducing it to integers before it reaches the safe-event boundary, and
   the closing-issues field above already covers the case where the body
   names the issue, so the body stays out.
5. None: the column is blank.

A pull request can close several issues, so a row keeps every linked issue,
not one: `BoardRow.issues` is a tuple of `(repository, number, confirmed)`
entries sorted by number, confirmed sources first. The repository is the
`host/owner/name` the issue belongs to, because two repositories on one board
can both have an issue 139 and `i` needs a full URL to open. For closing
issues it comes from the PR URL. For `gh issue` commands it has to survive
the privacy boundary: `_gh_issue_stage_material()` does extract an explicit
`-R` repository or issue URL, but only into the HMAC material for
`task_stage_id`, and the persisted event keeps nothing but the rendered
number. So the normalizer also sets the event's already-approved `github`
sub-mapping with `number` and a `url`, but never the URL the command
contained. Command events cross the boundary while still `running`, before
`gh` has accepted or rejected anything, and `_safe_http_url()` checks only
scheme and hostname, so persisting a typed URL would record whatever host the
person typed, including a private one. Instead the normalizer takes the
repository apart and rebuilds it: host, owner, and name must each match
`[A-Za-z0-9_.-]+`, and the host must be `github.com` or a host listed in gh's
own `~/.config/gh/hosts.yml`, the file gh writes for every host it has
authenticated against. Only then is
`https://<host>/<owner>/<name>/issues/<number>` written to `github.url`. A
repository that fails either check yields no `github` metadata at all, and
the event does not confirm a link.

The repository comes from, in order: an explicit `-R`/`--repo` flag or issue
URL in the command; a `GH_REPO=<[host/]owner/name>` assignment preceding `gh`
on the same command line, which gh documents as overriding the local
repository; and otherwise the worktree's `origin` remote, which the existing
GitHub readback already resolves. A hostless value takes its host from a
`GH_HOST=` assignment on the same line, then from the single non-default host
in `hosts.yml` if there is exactly one, then `github.com`. `_gh_repository_scope()`
handles the flag forms today and gains the two environment assignments. The ISSUE cell shows the first number and a count
for the rest (`#139 +1`), with the repository shown only when it differs from
the row's own. The conflict check treats any overlap between two rows'
`(repository, number)` sets as a shared issue, `i` opens the first and
repeated presses cycle through the rest, and the detail pane header lists them
all with their repositories.

Only integers and GitHub URLs cross the privacy boundary, matching how PR
numbers are handled today. `closing_issues` joins `_SAFE_GITHUB_FIELDS` as a
tuple of integers, but listing the name is not enough:
`_safe_github_metadata()` accepts scalars only and rejects any collection as
an unsupported value, so a `SafeEvent` carrying a tuple would fail to build.
The readback normalizer reduces each `closingIssuesReferences` object to its
`number` before the boundary, and `_safe_github_metadata()` gains an explicit
branch for this one key: a tuple of at most sixteen positive integers within
`MAX_SAFE_INTEGER`, anything else rejected.

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

`g` cycles between the flat table, grouping by surface, and grouping by
repository. The flat table is the default, as the mockup above shows; grouping
puts a dim header over each group and drops the grouped column, since the
header already says it. (This document originally made grouping by surface the
default; the implementation kept the flat table.)

### Conflict strip

Between the roster and the detail pane, at most three warnings:

- two live sessions with the same worktree path
- two live sessions on the same branch of the same repository, different
  worktrees
- two live sessions attached to the same issue

Each names both surfaces so the person can decide which one to stop.

### Detail pane

The lower third shows the selected session's recent events using the existing
`render()` path, so the board inherits every event kind, colour, and privacy
rule. It does not reuse the `--session` filter: `matches_session_filter()` is
a case-insensitive substring match over session id, pane id, and label, so
pane `w1:p1` would also pull in `w1:p10`, and a pane source key matches
nothing. `board.py` supplies an exact predicate instead, true only when the
event's `SessionKey` equals the row's, or, for a pane-keyed row, when the
identity the event was seated under carries that pane id. The pane is optional (`d` toggles) so the board
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
for piping. Config has an optional `[board]` table for the default grouping
and detail toggle, following the `[display]` pattern:

```toml
[board]
group = "none"     # none, surface, or repo
detail = "shown"   # shown or hidden
```

Absent or misspelled values fall back to the defaults and never stop the
board; `--group` and `--no-detail` win over the file.

The browser panel serves the same roster at `/board` (behind the same token
as the timeline), with `/board/data` returning the current message as JSON
and `/board/events` streaming it over SSE the way the timeline streams. The
page is built from the same `BoardRootState`s, `rows_from_sources()`, and
`detect_conflicts()` as the terminal. `board_rows_payload()` turns rows into
a frozen, self-validating `BoardMessage` (rows of `BoardRowWire`, each issue
a `BoardIssueWire`): display names, branch, surface, status, age and the
absolute last-activity time behind it, issue labels with URLs, and the pull
request reduced to the `_SAFE_GITHUB_FIELDS` closed set with an http(s)-only
URL. It becomes JSON only at the HTTP boundary, through `to_wire()`. No
filesystem path is in it: not the folder, the working root, the Git common
directory, nor the row key, which is replaced by a digest. The browser's
conflict strip comes from `browser_conflicts()`, which phrases a shared
worktree as "one worktree of <repository>" rather than naming the folder the
terminal shows. The roster is refreshed on its own thread, only while a
browser has the page open or asked for its JSON recently, so the timeline is
never held back by it. `?group=` on the page overrides the configured
default; `g` and `d` cycle grouping and the detail line once it is open, and
`side-dog panel --no-board` (what `side-dog demo` passes) leaves the page
empty.

## Code shape

- `side_dog/board.py`, new. Pure functions only: `BoardRow` (frozen
  dataclass), `rows_from_identities()`, `surface_label()`, `linked_issues()`,
  `conflicts()`, `sort_rows()`, `render_board(rows, width, height, color)`.
  No I/O, so every table above is a unit test with a literal fixture.
- `side_dog/surfaces.py`, new. The process-ancestry probe, with the `ps` and
  `lsof` calls behind one function that tests patch. Cached per pid.
- `side_dog/cli.py`. The `board` subcommand, its argument parser, and the loop
  that reuses `aggregate_watch_identities()`, `PollCoordinator`, the Git status
  refresh, and the GitHub readback. The `gh pr view` field list grows by one.
- `side_dog/integrations.py`. `surface` joins `AgentIdentity` as a plain
  string field. `closing_issues` joins `_SAFE_GITHUB_FIELDS`, and
  `_safe_github_metadata()` gains the bounded-tuple validation described
  above. Both need the privacy review that the closed-set comment asks for.
- `side_dog/cli.py`, phase 2. The `gh issue` command patterns and
  `_gh_issue_stage_material()` learn `view` and `develop`.
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
  marker; a failed `gh issue view` does not confirm a link;
  `gh issue develop --base 123 456` links issue 456; `gh issue view -R
  org/other 12` links `org/other` and not the worktree's origin;
  `GH_REPO=org/other gh issue view 12` does the same; `gh issue view 123 ||
  gh issue view 456` confirms nothing; a command naming an issue URL on a host
  absent from `hosts.yml` persists no `github` metadata; the detail
  predicate selects `w1:p1` and not `w1:p10`; a PR closing
  three issues renders as `#n +2`, conflicts on any of the three, and cycles
  through them on `i`; the same issue number in two repositories is not a
  conflict; two Herdr panes without session ids stay two rows; conflict
  detection for the three cases; sort order; `render_board()` with `color=False` at three widths,
  including the roster-only fit.
- `tests/test_cli.py`: board discovery with nine active folders yields nine
  roots where `watch` discovery yields eight, and `watch` discovery is
  unchanged when `uncapped` is left at its default.
- `tests/test_surfaces.py`: ancestry walk against patched `ps` output for a
  Ghostty chain, a Herdr chain, an orphaned chain, and a dead pid.
- `tests/test_cli.py`: `side-dog board --once` on a temporary state directory
  produces a stable frame; `--group repo` reorders it.
- `tests/test_surfaces.py` also covers the Codex session-to-process step: an
  open rollout file wins, a unique cwd match is accepted, two cwd matches
  resolve to `unknown`.
- Privacy tests: `closing_issues` accepted as a tuple of integers, rejected
  when it holds a string, a negative number, a nested collection, or more
  than sixteen entries. `gh issue view` and `gh issue develop` reduce to a
  number the way `close` does.
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
