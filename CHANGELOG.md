# Changelog

All notable Side Dog changes will be recorded here.

## [2.1.1] - Unreleased

- Fix the Watch-to-Board `b` shortcut crash and preserve the selected roots.
- Attribute recorded activity to models and reasoning effort consistently in
  Watch and Board, including narrow terminal layouts.
- Require verified evidence for Board issue attribution and preserve repository
  identity when interpreting issue references.
- Remove failed-test desktop popups while retaining terminal activity reporting.
- Improve installation and usage documentation, bundled manual discovery, and
  Board exit-key visibility in small panes.
- Add installed Linux runtime validation, including interactive terminal and
  browser checks, and record installed wheel provenance.

## [2.0.0] - 2026-09-07

- Keep the expanded header to at most forty percent of the pane so the
  timeline keeps the rest: watched folders are listed on one grouped line
  per parent ("~/src: a, b, c"), folders with the same refresh warning
  share one line, and per-session usage rows fold behind a new `u` key.
- Show the board in the browser panel at `/board`, linked from the timeline,
  updating live over its own event stream from the same sources as the
  terminal board and sending no folder paths. Add an optional `[board]`
  configuration table (`group`, `detail`) for the defaults both views start
  with; `--group` and `--no-detail` override it.
- Send a desktop notification from `side-dog board` when a pull request's
  checks pass or its review is approved while the session idles, when a
  session blocks with nothing else working in its repository, or when a
  new conflict appears in the strip. Each change notifies once until it
  lapses, at most one message per second, through the same `osascript` and
  `notify-send` path as test failures; `--no-notify` and `[notify]` turn it
  off.
- Make the board interactive: `j`/`k` select a session, enter or `d` shows
  its recent timeline in a detail pane, `o` and `i` open its pull request and
  issues, and a conflict strip warns when two live sessions share a worktree,
  a branch, or an issue.
- Add `side-dog board`, a machine-wide table with one row per live
  coding-agent session: agent, surface, repository and branch, pull request,
  and status, discovered without the watch folder cap.
- Link each board row to its issues: confirmed (`#139`) from the pull
  request's closing issues or a recent successful `gh issue view`/`develop`,
  inferred (`#139?`) from the branch name or PR title. Issue URLs from
  commands are rebuilt from validated parts and hosts gh knows, never copied.
- Name the terminal or app a bare-terminal Claude Code or Codex session runs
  in on the board (Ghostty, Herdr, iTerm2, kitty, WezTerm, VS Code) from its
  process ancestry. Only process names, parent ids, and start times are read,
  plus each Codex process's working directory and whether it holds a rollout
  file open, compared by file identity rather than by name; `unknown` is
  shown whenever more than one process or recent session could own a row.
- Render the watch screen promptly, then finish agent discovery and GitHub
  context in the background.
- Reuse Git worktree inventories and validated bounded history summaries to
  make warm startup substantially faster.
- Redesign the compact terminal header with aligned agent details, concise
  startup feedback, clearer working and idle states, and useful folder context.
- Turn the top line into a masthead: the Side Dog name in the identity color,
  version and scope beside it, and a striped fill running to the clock.
- Start every agent row and pull-request line with a status dot, `●` or `○`,
  painted in the state's color, and leave an unknown model or effort out
  instead of printing `?`.
- Quiet the timeline: folder colors become muted foreground tints on a thin
  left bar and on the badge instead of filled blocks, the badge is said once
  per run and never on a task card's child rows, unknown states show `·`
  rather than `?`, milestone lines bold only their title, and pull-request
  states read as words ("changes requested", not `CHANGES_REQUESTED`).
- Group coding-agent activity into tasks, reduce repeated read noise, and hide
  passive filesystem activity by default while keeping it available with `F`.
- Add machine-wide token usage and public-API cost estimates with clearer
  periods, scope, and explanations that estimates are not subscription bills.
- Improve Herdr and worktree scoping so watched folders and attached agents are
  attributed consistently across compact and expanded views.
- Make the macOS installer more reliable and document how to install, update,
  diagnose, and use Side Dog with or without Herdr.
- Add a shared framed View dialog with radio toggles for order, filter, detail,
  and multi-folder layout, plus the same frame for help and quit confirmation.

## [1.0.0] - 2026-09-03

- Establish Side Dog V1 as the package, CLI, doctor, and source metadata baseline.
- Add stable semantic-version validation and release-order checks.
