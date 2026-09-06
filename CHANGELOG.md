# Changelog

All notable Side Dog changes will be recorded here.

## [1.1.0] - Unreleased

- Add `side-dog board`, a machine-wide table with one row per live
  coding-agent session: agent, surface, repository and branch, pull request,
  and status, discovered without the watch folder cap.
- Link each board row to its issues: confirmed (`#139`) from the pull
  request's closing issues or a recent successful `gh issue view`/`develop`,
  inferred (`#139?`) from the branch name or PR title. Issue URLs from
  commands are rebuilt from validated parts and hosts gh knows, never copied.
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
- Group coding-agent activity into tasks, reduce repeated read noise, and hide
  passive filesystem activity by default while keeping it available with `F`.
- Add machine-wide token usage and public-API cost estimates with clearer
  periods, scope, and explanations that estimates are not subscription bills.
- Improve Herdr and worktree scoping so watched folders and attached agents are
  attributed consistently across compact and expanded views.
- Make the macOS installer more reliable and document how to install, update,
  diagnose, and use Side Dog with or without Herdr.

## [1.0.0] - 2026-09-03

- Establish Side Dog V1 as the package, CLI, doctor, and source metadata baseline.
- Add stable semantic-version validation and release-order checks.
