# Changelog

All notable Side Dog changes will be recorded here.

## [1.1.0] - Unreleased

- Render the watch screen promptly, then finish agent discovery and GitHub
  context in the background.
- Reuse Git worktree inventories and validated bounded history summaries to
  make warm startup substantially faster.
- Redesign the compact terminal header with aligned agent details, concise
  startup feedback, clearer working and idle states, and useful folder context.
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
