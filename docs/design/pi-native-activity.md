# Design: deeper native activity collection for Pi

## Status

Proposed. Builds on the Pi naming support in PR #53, which teaches Side Dog
to discover Pi sessions from `~/.pi/agent/sessions` and name them with their
model, effort and working state. This document specifies the next step: reading
what a Pi agent *does*, the way Side Dog already reads Codex's native stream, so
that Pi's tool calls, file writes, test runs and git operations appear in the
timeline without any hook installation.

## Goal and non-goals

**Goal.** Turn a Pi session's transcript into the same privacy-filtered event
stream Side Dog builds for Codex: commands starting, passing and failing; file
and config writes with line counts against the last commit; branch, commit,
push, pull-request and issue operations; and session boundaries. Pi should be a
first-class activity source, not merely a named row.

**Non-goals.**

- No new dependency, no daemon, no hook installed into Pi. Pi already writes a
  complete JSONL transcript per session; Side Dog reads it, exactly as it reads
  Codex's `rollout-*.jsonl`.
- No prompts, responses, thinking text, file contents, diffs, full shell
  command lines, stdout or stderr are ever stored. The existing
  `normalized_tool_events` pipeline enforces this and is reused unchanged.
- No change to how Pi is *named*; that path already works.

## What Pi writes

A Pi session is one JSONL file at
`~/.pi/agent/sessions/<encoded-cwd>/<timestamp>_<uuid>.jsonl` (honouring
`PI_HOME`). Records are append-only. The types that matter here:

| Record | Shape | Meaning for Side Dog |
| --- | --- | --- |
| `session` | `{"type":"session","id","cwd","timestamp"}` | Session opened; already used for naming. |
| `model_change` | `{"type":"model_change","modelId","provider"}` | Current model. |
| `thinking_level_change` | `{"type":"thinking_level_change","thinkingLevel"}` | Current reasoning effort. |
| `message` role `user` | `{"message":{"role":"user",...}}` | A turn begins. |
| `message` role `assistant` | `{"message":{"role":"assistant","content":[...]}}` | May carry `toolCall` items; a text-only assistant message ends a turn. |
| `message` role `toolResult` | `{"message":{"role":"toolResult","toolCallId","toolName","isError"}}` | A tool call finished. |

A tool **call** is an item inside an assistant message's `content` array:

```json
{"type":"toolCall","id":"toolu_017qqV...","name":"bash",
 "arguments":{"command":"..."}}
```

A tool **result** is its own record, referring back by id:

```json
{"type":"message","timestamp":"2026-09-01T22:07:30.436Z",
 "message":{"role":"toolResult","toolCallId":"toolu_017qqV...",
            "toolName":"bash","isError":false}}
```

The tools Pi exposes and how they map to Side Dog's vocabulary:

| Pi tool | `arguments` | Side Dog treatment |
| --- | --- | --- |
| `bash` | `{"command": str}` | `tool_name:"Bash"`, classified by `classify_commands` (tests, git, pr, issue…) or reduced to a program name on failure. |
| `write` | `{"path": str, "content": str}` | `tool_name:"Write"` (an `EDIT_TOOLS` member); path only. |
| `edit` | `{"path": str, "oldText"/"newText"` or `"edits":[…]}` | `tool_name:"Edit"`; path only. |
| `read` | `{"path": str, …}` | Ignored — reading is not activity. |

The call carries the arguments; the result carries only `isError`. This is the
one structural difference from Codex, whose `CommandExecution` /`FileChange`
items are self-contained. It means the Pi reader must **remember each open call
by id** and complete it when the matching `toolResult` lands — the same
call/result pairing the Codex reader already does for
`custom_tool_call` → `custom_tool_call_output`, keyed on `call_id`.

## Mapping onto the existing pipeline

Everything downstream of a normalized event is agent-agnostic. The design adds a
Pi-specific *reader* and reuses the rest verbatim:

- `normalized_tool_events(payload, root, status=…)` — turns a
  `{tool_name, tool_input, …context}` payload into timeline events with the
  right titles, `kind`, and (for successful writes) line counts via
  `git_line_changes`. Reused unchanged for both `bash` and `write`/`edit`.
- `failed_command_events` — names a failed command by program only. Reused.
- `append_event_once(root, event)` — idempotent write keyed on
  `source_event_id`, so re-reading a transcript never double-counts. Reused.
- `hook_context` / `_stream_context` — stamp agent, session, model, effort,
  turn and Herdr pane onto each event. Reused; `_stream_context` gains an
  `agent` field (below).
- `NativeAgentStream` + `native_streams` position table — per-session file
  cursor persisted in SQLite so restarts resume where they left off. Reused.

Because the normalization and de-duplication layers are shared, Pi automatically
inherits the same privacy guarantees and the same timeline grouping (pipeline
stages, compaction) that Codex and Claude already get.

## Code changes

### 1. Make `NativeAgentStream` agent-aware

Add two fields; default keeps Codex behaviour:

```python
@dataclass
class NativeAgentStream:
    session_id: str
    path: Path
    position: int
    agent: str = "codex"                 # NEW: "codex" | "pi"
    agent_root: str = ""
    model: str = ""
    effort: str = ""
    turn_id: str = ""
    pending_commands: deque[...] = ...    # Codex
    completed_commands: deque[...] = ...  # Codex
    pending_calls: dict[str, dict[str, Any]] = field(default_factory=dict)  # NEW: Pi
```

`pending_calls` maps a Pi `toolCallId` to the minimal, already-filtered payload
derived from its call record: `{"tool_name", "tool_input", "kind_hint"}`. It is
bounded implicitly — a call is removed when its result arrives — with a safety
cap (e.g. 256, dropping oldest) to survive a transcript that records a call
whose result never lands.

**`pending_calls` must survive a restart.** Unlike Codex, whose completion
records are self-contained, a Pi `toolResult` carries only `isError` and its id;
the arguments live in the earlier `toolCall`. If Side Dog stops after the cursor
has advanced past a call but before its result is written, an in-memory-only
`pending_calls` would be lost, the result would find no pending entry on
restart, and the operation would sit "running" forever with no success/failure
event. Two ways to close this, in preference order:

1. **Reconstruct on attach (preferred, no schema change).** When a Pi stream is
   first attached in `sync_native_streams`, scan the transcript from the session
   start up to the saved cursor and rebuild `pending_calls` from every
   `toolCall` whose `toolResult` has not yet appeared. This is a bounded,
   one-time read per session (the same file already open) and needs no new
   storage. It also naturally repairs a `pending_calls` cap eviction, since the
   reconstruction is authoritative.
2. **Persist alongside the cursor.** Widen the `native_streams` row (or add a
   sibling `native_pending_calls` table keyed on `(session_id, call_id)`) so the
   filtered pending payload is written whenever the cursor is saved and cleared
   when the result is emitted. More durable across a crash mid-tick, at the cost
   of a schema migration.

The reconstruct-on-attach path is recommended: it reuses the transcript as the
source of truth, matching the "the agent's own file is the record" principle the
whole integration rests on. `save_native_stream_position` continues to persist
only the cursor.

`_stream_context` emits `stream.agent` instead of the literal `"codex"`, so Pi
events carry `agent:"pi"` and render under the "Pi" label already added to
`agent_label`.

### 2. A Pi record reader: `_poll_pi_record`

Mirrors `_poll_codex_record`. One record in, count of appended events out.

```python
def _poll_pi_record(root, stream, record) -> int:
    t = record.get("type")
    if t == "model_change":
        stream.model = record.get("modelId") or stream.model
        return 0
    if t == "thinking_level_change":
        stream.effort = record.get("thinkingLevel") or stream.effort
        return 0
    if t != "message":
        return 0
    message = record.get("message") or {}
    role = message.get("role")
    if role == "user":
        return _emit_pi_turn_boundary(root, stream, record, message)   # optional; see below
    if role == "assistant":
        return _emit_pi_tool_calls(root, stream, record, message)      # "running" events
    if role == "toolResult":
        return _emit_pi_tool_result(root, stream, record, message)     # "success"/"failed"
    return 0
```

**Tool calls (`assistant`).** For each `content` item with `type == "toolCall"`:

- `read` → skip.
- `bash` → remember `{tool_name:"Bash", tool_input:{command}}` under the call
  id; emit the `running` events via `_append_native_tool_events`, filtered to
  the watched root (the command's `cwd` is not always present, so fall back to
  `stream.agent_root` exactly as Codex does with `_native_path_matches_root`).
- `write`/`edit` → remember `{tool_name:"Write"|"Edit", tool_input:{path}}`;
  emit `running` ("Writing file"/"Writing config"). Skip when the path is
  outside the root.

`source_event_id = f"pi:{session_id}:call:{call_id}:running"`, `operation_id`
= the call id, so start and finish share a group.

**Tool result (`toolResult`).** Look up `pending_calls[toolCallId]`; if absent,
ignore (its call predates our cursor). Status is `"failed"` if
`message.isError` is true, else `"success"`. Re-run `_append_native_tool_events`
with the remembered payload and the terminal status, then drop the pending
entry. `source_event_id = f"pi:{session_id}:call:{call_id}:output"`. Line counts
for successful writes are computed here by the shared pipeline, against the last
commit, never from Pi.

The call/result split means a `bash` that runs `pytest` produces a *Tests
running* event when the call is seen and a *Tests passed*/*Tests failed* event
when the result's `isError` is known — the same two-beat the timeline already
shows for Codex.

### 3. Timestamps

Pi records carry ISO `timestamp` at top level and an epoch `timestamp` inside
`message`. `_record_time` already handles both an ISO `timestamp` and an epoch
field, so `_record_time(record)` (ISO) is the default and
`_record_time(message, "timestamp")` (epoch ms) is preferred for the finish
event, giving millisecond ordering identical to Codex's `completed_at_ms`.

### 4. Wire the reader into the stream loop

`sync_codex_streams` becomes `sync_native_streams` (or gains a sibling) that
also attaches Pi identities:

```python
for identity in identities.values():
    agent = identity.get("agent")
    if agent == "codex":
        path = codex_session_path(session_id)
    elif agent == "pi":
        path = pi_session_path(session_id)
    else:
        continue
    ...
    streams[session_id] = NativeAgentStream(..., agent=agent, path=path, ...)
```

`poll_native_agent_events` already walks every stream line by line and persists
the cursor; only the per-record dispatch changes:

```python
if isinstance(record, dict):
    if stream.agent == "pi":
        count += _poll_pi_record(root, stream, record)
    else:
        count += _poll_codex_record(root, stream, record)
```

`announce_native_history` is reused, but making it work for Pi takes more than
a new milestone id. It calls `native_event_count`, whose SQL counts only rows
whose `source_event_id` matches `codex:{session_id}:%`. A Pi backfill would
therefore always count zero, return before emitting, and test case 7 would fail.
`native_event_count` must become agent-aware — take the agent (or the full
prefix) and match `f"{agent}:{session_id}:%"` — and `announce_native_history`
must pass `stream.agent` through. Its milestone id likewise becomes
`f"{stream.agent}:{session_id}:history-backfill-complete-v3"`, so Codex and Pi
each get their own "caught up on earlier activity" line and their own count.

### 5. Session and turn boundaries (optional, phase 2)

Codex's native stream mostly emits tool and subagent events, leaving session
framing to Herdr/Claude hooks. Pi's transcript makes turn framing cheap:

- On the first `session` record, emit a `kind:"session"` "Pi session active"
  event (id `pi:{session_id}:session:start`).
- A `user` message opens a turn; a subsequent text-only `assistant` message
  closes it. These can emit "Pi turn started"/"Pi turn finished" the way the
  Claude hooks do, keyed by the message id so they de-dupe.

This is isolated behind `_emit_pi_turn_boundary` and can ship after the tool and
file path, since the tool path is what makes Pi useful in the timeline.

## Idempotency, restarts and races

- **Restart / reopen.** The `native_streams` table stores `(session_id,
  transcript_path, position)`. On startup the Pi stream resumes at its saved
  offset; nothing is re-emitted. Identical to Codex.
- **Restart between a call and its result.** Because a Pi result is not
  self-contained, resuming the cursor is not enough: the pending call it
  completes was only ever in memory. On attach, the Pi stream rebuilds
  `pending_calls` by scanning the transcript up to the saved cursor for calls
  whose results have not yet appeared (see §1), so the result read after
  restart still finds its call and emits the terminal event. This case gets its
  own test (case 8).
- **Partial lines.** `poll_native_agent_events` already refuses a line without a
  trailing newline and rewinds to its start, so a half-written record Pi is
  mid-append on is read whole on the next tick.
- **Call without result (yet).** The `running` event stands alone until the
  result arrives; when it does, the shared `operation_id`/`group_id` collapses
  the pair into one timeline row. A result that never arrives simply leaves the
  "running" row, which is truthful.
- **Result without a remembered call.** Ignored — it belongs to a call from
  before our cursor, whose "running" we also never emitted, so there is nothing
  to complete.
- **De-dup across views.** Two Side Dog panes on one repo both read the same
  transcript; `append_event_once` keyed on `source_event_id` means the second
  writer no-ops. Identical to Codex.

## Testing

Extend `tests/test_native_agent_events.py` with a `write_pi_transcript`
helper (a superset of the existing `write_pi_session`) that appends `toolCall`
and `toolResult` records, then assert against `latest_events` / `PanelFeed`:

1. **A bash test run reports running then passed/failed.** A `bash` call running
   `pytest` followed by a `toolResult` with `isError:false` yields a *Tests
   running* then *Tests passed* pair sharing a group; `isError:true` yields
   *Tests failed*.
2. **A write reports a file event with line counts.** A `write` call to a
   tracked file, on success, carries `lines_added`/`lines_removed` from
   `git_line_changes`, and a `.toml`/`.github` path is classified as config.
3. **`read` produces nothing.** No event for a `read` call/result.
4. **A command outside the root is skipped.** A `bash` whose only path is another
   repo is filtered by `_native_path_matches_root`.
5. **Only the failing program name is stored.** A failed non-classified command
   stores the program name, never the full command line (reuses
   `failed_command_events`, already covered for Codex — assert it holds for Pi).
6. **Restart resumes at the saved position.** Poll once, record the cursor, poll
   again with no new records → zero new events; append one record → exactly one.
7. **Backfill milestone fires once per agent.** After `native_event_count` is
   made agent-aware, a Pi backfill counts its own `pi:{id}:…` rows and emits
   `pi:{id}:history-backfill-…` exactly once; a second pane does not duplicate
   it. (This case fails until the count fix in §4 lands.)
8. **Restart between a call and its result completes the operation.** Write a
   transcript ending at a `toolCall`, poll (emits *running*, saves the cursor),
   then discard the in-memory streams to simulate a restart, append the matching
   `toolResult`, and poll again on a freshly attached stream. The reconstructed
   `pending_calls` must let the result emit exactly one terminal
   (*passed*/*failed* or *Wrote file*) event sharing the call's group — no
   orphaned "running" row.

These mirror the Codex cases in the same file, so coverage parity is easy to
verify.

## Rollout

1. **Phase 1 — tools and files.** Items 1–4 above. This is the bulk of the value
   and is fully testable against synthetic transcripts.
2. **Phase 2 — session/turn framing.** Item 5. Additive; behind its own helper.
3. **Docs.** Flip the README's "Pi — collecting activity" line from
   *not yet wired up* to *ready, and it needs no setup*, noting that unlike
   Claude, Pi needs no `side-dog init` because it, like Codex, keeps a local
   activity stream Side Dog can read directly.

## Why this shape

The whole point of the Codex integration was that a self-describing local
transcript needs no cooperation from the agent — no hook, no daemon, no
patched settings. Pi has exactly such a transcript, and its records differ from
Codex's only in surface: an assistant-message envelope around tool calls and a
separate result record. By confining those differences to one reader function
and reusing normalization, de-duplication, persistence and rendering wholesale,
Pi's activity collection is a small, well-bounded addition that inherits every
privacy and correctness property the Codex path already proved.
