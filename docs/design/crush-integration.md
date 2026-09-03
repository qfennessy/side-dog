# Crush integration

Status: implemented against Crush v0.92.0 and upstream commit
`e3c970336d7ca889b75dd9bf8c1c4ffd42d65396` (verified 2026-09-02).

## Source contract

Crush records known workspaces in `$CRUSH_GLOBAL_DATA/projects.json`. Without
that supported override, it uses `$XDG_DATA_HOME/crush/projects.json` or
`~/.local/share/crush/projects.json`. Each index entry supplies the project
`path` and its authoritative `data_dir`; Side Dog does not guess or replace the
indexed data directory. The project database is `<data_dir>/crush.db`.
Side Dog validates index entries, ranks the valid set by `last_accessed`, and
only then applies its project cap so malformed or stale entries cannot hide an
active project later in the file.

The integration requires the identity and timing columns in `sessions` plus
the role, model, provider, summary flag, timing, and JSON `parts` columns in
`messages`. Databases are opened with SQLite `mode=ro`, `query_only`, and a
short timeout. Required columns are checked before any session or activity
query. Missing, busy, unreadable, malformed, or migrated stores produce only a
fixed adapter health code.

Primary upstream references:

- [project index](https://github.com/charmbracelet/crush/blob/e3c970336d7ca889b75dd9bf8c1c4ffd42d65396/internal/projects/projects.go)
- [read-only SQLite connection](https://github.com/charmbracelet/crush/blob/e3c970336d7ca889b75dd9bf8c1c4ffd42d65396/internal/db/connect.go)
- [session queries](https://github.com/charmbracelet/crush/blob/e3c970336d7ca889b75dd9bf8c1c4ffd42d65396/internal/db/sql/sessions.sql)
- [message queries](https://github.com/charmbracelet/crush/blob/e3c970336d7ca889b75dd9bf8c1c4ffd42d65396/internal/db/sql/messages.sql)
- [message part types](https://github.com/charmbracelet/crush/blob/e3c970336d7ca889b75dd9bf8c1c4ffd42d65396/internal/message/content.go)

## Identity and routing

The global project index and its databases are scanned once into a short-lived
machine-wide cache. Indexed project paths are resolved through Side Dog's
existing worktree-root logic. Only top-level sessions get an agent identity;
all descendants of a `parent_session_id` chain contribute freshness and are
attributed to that top-level `SessionKey("crush", session_id)`.
The recent-session cap includes the selected sessions' bounded ancestor
closure. Missing, over-limit, or cyclic parent chains are discarded so a
child title can never be promoted to a top-level identity label.

A tree is `working` when any member has no terminal finish and its newest
persisted update is at most 60 seconds old. It is `idle` when unfinished but
quieter, and `done` when every member's latest persisted message has a terminal
finish. Identities age out after 15 minutes. `tool_use` is not terminal;
`end_turn`, `max_tokens`, `canceled`, `error`, and `content_filter` are.

## Activity and checkpoints

One Crush polling adapter receives every watched root and reads each relevant
project database at most once per cycle. It keeps a provider-qualified root
watch baseline and a checkpoint for every top-level and descendant session.
New watches begin at the current time, so old activity is not backfilled.
The root baseline advances only to the start of a confirmed project/session
cache snapshot. A session created while that snapshot is being read or while
the two-second cache remains live therefore stays after the baseline and is
collected when the next snapshot discovers it. Watermarks are rounded down to
whole seconds because supported Crush stores may use second-resolution
timestamps. The root watermark is withheld
when a recent session tree has not reached the current identity snapshot or
when any indexed database for that Git root fails to list or poll.

Crush updates message rows in place while tool input streams and results land.
Queries therefore apply a per-session lower bound at the five-minute overlap
floor before building downstream pages; lifetime history is not materialized
on each poll. New rows
are paged from the oldest pending timestamp, while overlap rows are read in a
separate cohort bounded independently per session so a busy session cannot
starve another session's call/result pairing or prevent cursor progress. When
a new result references an older call inside the overlap window, that call row
is included even if it fell beyond the ordinary per-session overlap cap.
Each session advances only to the newest message actually scanned for that
session. SQL's JSON functions return only relevant lifecycle scalars; repeated
running and terminal states use stable source IDs and are removed by Side
Dog's durable event deduplication. Inclusive overlap reads also make
equal-second updates safe. A checkpoint is applied only after all preceding
safe events are accepted.
If any persisted root or session checkpoint cannot be loaded, the entire poll
fails closed with a fixed health code and returns no events or checkpoints.

Completed tool input is reduced immediately:

| Crush part/tool | Side Dog mapping |
| --- | --- |
| `tool_call` + `tool_result` for `bash` | Shared command/test/Git/PR/issue normalizer |
| `edit`, `write` | Shared file/config normalizer after in-root path proof |
| `view`, `read`, `grep`, `glob`, `fetch` | Fixed context markers |
| todo tools | Count-only todo marker |
| child session creation/finish | Subagent lifecycle with completed, failed, or cancelled titles |
| terminal assistant `finish` | Turn completion with success, failed, or unknown status |
| `shell_command` | Shared command normalizer using command and exit code only |

## Privacy boundary

The Crush database is private source data. Side Dog does not select message
text, reasoning, result content/data/metadata, shell output, files-table rows,
or complete message rows. It transiently reads only the command or path fields
needed by the shared normalizer. Edit bodies and todo text are discarded in the
reader. Unknown tools and malformed or privacy-ambiguous parts are skipped.

Full commands never enter `PollBatch`; only normalized safe events do. Failed
commands retain a bounded program name, file paths must resolve within the
watched root, and diagnostics contain only fixed `PollErrorCode` values. The
panel receives the same closed `SafeEvent` projection as every other provider.

## Known limits

- The bounded five-minute overlap cannot reconstruct a tool call that began
  more than five minutes before a Side Dog restart and whose result is stored
  separately afterward. It is skipped instead of guessing or exposing output.
- Session liveness reflects persisted Crush lifecycle state, not process
  inspection. A crashed session with no terminal finish becomes idle and then
  ages out.
- Unknown future tool names and incompatible schema revisions fail closed until
  their privacy and lifecycle contracts are reviewed.
