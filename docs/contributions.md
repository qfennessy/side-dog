# Which model worked on this PR?

Run `side-dog watch /path/to/project`. Each attributed timeline action identifies
the provider, recorded model and abbreviated session ID. Missing metadata reads
`model unknown` or `unknown`; the current roster never supplies a model for old
work. A model switch therefore does not rename earlier contributions.

Press `b` to open Board's contributions view, or run:

```sh
side-dog board --activity /path/to/project
side-dog board --activity
```

The first command scopes recorded history to that folder; the second includes
all saved folders, including departed sessions and worktrees. Board shows the
last 24 hours of recorded work in a one-line table per contributor, grouped by
repository and ordered by contribution size. The table abbreviates the session
ID; `j`/`k` selects a row, `d` reveals its full session detail, and `o` opens
its recorded link.
Press `a` for the current session roster, and `w` to return to Watch.

## What the counts mean

Edits, commits, tests by outcome and PR actions count recorded operations.
A start followed by completion counts once. Failed and unknown outcomes are
labeled; a failed commit is not a successful commit. Different models and
sessions retain separate rows even when they work on the same PR.

Git, filesystem and polled GitHub activity are repository observations. A CI
poll is never model effort: when it matches a contributor's structured work it
updates that row's recorded state, and unmatched polling is folded into one
trailing observation notice rather than repeated placeholder rows.
A successful agent commit can also be linked when its full commit object ID
uniquely matches a recorded PR head in the same folder. This proves the work
link; its model still comes from the commit event. Shared heads stay ambiguous.
PR state/checks on an observation describe its most recent recorded readback,
not guaranteed live state. `PARTIAL` means incomplete information.

Only structured issue/PR metadata on an agent action establishes a contribution
link. An unambiguous link within the same recorded session turn can label that
turn's other actions. Polling observations never supply those links, even when
they retain a triggering session ID.
If a turn mentions several work items, unlinked actions stay unlinked. Branch
names, titles and the model currently active in a folder do not prove authorship.
Some historical commits therefore remain unlinked or unattributed.

No session-wide token estimate, elapsed session time or estimated cost is divided
among PRs. The contribution view currently reports counts and recency only.
The displayed `last success/failed/running/unknown` is the last recorded event
outcome, not proof that the session is still running. Session completion remains
visible as its recorded session event.

## Scope and limits

Watch collects activity and uses its own folder, focus and event filters. Board
contributions reads saved history; it does not start collectors. Run Watch or
the panel first if there is no recorded activity. Switching from Watch carries its currently selected folders (including
discovered worktrees and folder focus) into Board. Event filters and Watch's
bounded display tail can differ: Board's scope
line states its folders and fixed 24-hour window. Use bare `board --activity`
to include all saved worktrees. Returning to Watch restores its original options.

The live roster and browser `/board` page use current discovery and may omit old
completed sessions; `a` in terminal Board exposes their recorded contributions.
History reading is limited to 16 MiB per read and 20,000 retained events per
folder. The view says `PARTIAL history` when truncated. Deleting disposable
state deletes this history. Missing GitHub access never creates inferred authors.

See [terminal evidence and validation](validation.md).
