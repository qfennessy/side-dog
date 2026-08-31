# Side Dog

Side Dog is a narrow terminal timeline for watching coding agents work. Claude
Code hooks provide direct tool events; Herdr supplies live Claude and Codex
identity. The timeline shows:

- file and configuration writes;
- running, passed, and failed test commands;
- branch, worktree, commit, and push operations;
- pull request creation and merge operations;
- issue creation, closure, and reopening; and
- agent session and turn boundaries.

It is deliberately small: Python 3.11+, no runtime dependencies, an append-only
JSONL activity feed, and an ANSI terminal UI that uses the full pane width by
default while remaining readable in a narrow split. Use `--width 42` when an
explicit cap is useful.

## Try it

From this checkout:

```sh
uv run side-dog init .
uv run side-dog watch .
```

Restart Claude Code after initialization. Keep `side-dog watch` in a narrow
terminal split to the right of Claude Code. In Herdr, split the Claude pane to
the right, resize it, and run the watcher in the new shell pane. Herdr session,
workspace, tab, and pane identity are detected automatically.

Press `?` in the Side Dog pane for a quick guide. Press `e` to switch between
compact and expanded detail, `f` to cycle all/milestone/file views, and `p` to pause or
resume display updates without stopping collection. Press `?` again or `Esc`
to return to the timeline.

All agent, filesystem, Git, test, and delivery events appear in one newest-first
timeline. The display fills the available pane height with retained semantic
events and reports how many continue below the viewport. Agent-originated
events carry a compact `Codex` or `Claude` label.
Filter the timeline by Herdr pane ID, task title, or agent session-ID prefix:

```sh
uv run side-dog watch . --session wB:p1
```

To see the display before starting an agent, run this in one terminal:

```sh
uv run side-dog watch .
```

and this in another:

```sh
uv run side-dog demo .
```

For a persistent command outside the checkout, use `uv tool install .` and run
the same commands without the `uv run` prefix.

## How collection works

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

Events are stored per canonical project root under
`~/.local/state/side-dog/projects/` with owner-only permissions. Set
`SIDE_DOG_STATE_DIR` to choose another private location. Side Dog stores only
short activity metadata; it does not store source contents, shell output, full
shell commands, prompts, or transcripts.

Related edits, tests, commits, pushes, PRs, and merges from one agent turn render
as one Delivery sequence with elapsed time:

```text
12:13  ┌ Codex · Delivery · 2m03s
       Edit ×19 → Tests ✓ → Commit 200d661 → Push ✓ → PR #3
```

Passive filesystem activity collapses into time-bounded bursts with change and
path totals plus the busiest paths. Expanded detail restores individual
rows. This is presentation-only: the append-only JSONL keeps every raw event.
Atomic events use one display line, including their cropped target or title.
Only compressed filesystem bursts and delivery sequences use continuation
lines, so long PR and commit histories use the pane height efficiently.
GitHub refreshes that do not change the PR's visible title, lifecycle, CI,
review, or mergeability state are suppressed; real status transitions remain.

Common labels are compacted in the display (`File changed` becomes `changed`,
for example) so paths and operation targets receive the remaining width.
Issue and pull-request creation events safely retain their `--title` value, but
not their body or full shell command. Verified PR banners and lifecycle events
also use the title returned by GitHub.

A Git status line near the top always shows the watched worktree's current
branch and HEAD commit. Side Dog also polls Git directly and emits a commit or
branch event when that state changes, including changes made outside Claude's
Bash hooks.

Herdr's active agent snapshot identifies whether a running pane belongs to
Codex or Claude. For Codex, Side Dog reads only the latest local session's
`model` and `effort` metadata and displays them with the running status; it does
not copy prompts, responses, or transcript content into the activity feed.

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
- Issue activity is immediate when Claude invokes `gh`; issues are not yet
  reconciled from GitHub after the command.
- The filesystem fallback uses polling while the pane is open. Very large
  repositories may want a native watcher later.
- Hook payloads and session IDs follow Claude Code's current documented hook
  contract. The event schema is versioned as `side-dog-activity-v1` so later
  collectors can evolve independently of the display.
- Codex model and effort discovery depends on the current local session-log
  shape. If unavailable, the pane reports `model ?` or `effort ?` rather than
  guessing.

The design borrows several lessons from Quodet: a versioned append-only event
boundary, machine-local hook configuration, canonical-root/session scoping,
explicit direct-edit coverage, and honest separation between attempted edits,
confirmed writes, and after-the-fact filesystem observation.
