# T3 Code integration

This design was checked against T3 Code commit
`ca63d42d670837b918081d1fc1ebada553814b4c` on 2026-09-02.

## Role

T3 Code is an optional context source, not a coding agent. It supplies the
thread title, provider, status, workspace, and worktree for agents it launches.
Codex, Claude Code, and OpenCode keep their native Side Dog identity and
activity readers. Cursor Agent and Grok Build use T3 Code's projected activity
because Side Dog has no standalone reader for either provider.

Herdr remains independent and optional. When Herdr and T3 Code describe the
same native session, Herdr keeps ownership of terminal-pane details while T3
Code can add the working folder and thread title.

## Store contract

Side Dog resolves only `T3CODE_HOME`, falling back to `~/.t3`, and opens
`userdata/state.sqlite` read-only with `query_only` enabled. It requires the
columns it uses in these tables before returning any session or activity:

- `projection_projects`
- `projection_threads`
- `projection_thread_sessions`
- `provider_session_runtime`
- `projection_thread_activities`
- `projection_turns`

T3 Code uses WAL mode, so the normal SQLite reader sees committed data without
copying or modifying the database. A missing, busy, unreadable, or changed
schema produces unavailable/degraded readiness instead of partial guesses.

Provider names are normalized to Side Dog's canonical names. The provider
resume cursor supplies the native identity:

| Provider | Resume cursor field |
| --- | --- |
| Codex | `threadId` |
| Claude Code | `resume`, with `sessionId` accepted for compatibility |
| Cursor Agent | `sessionId` |
| Grok Build | `sessionId` |
| OpenCode | `sessionId` |

If that id is absent, Side Dog does not merge the T3 thread with a native
session by time or folder.

## Safe activity mapping

One machine-wide query serves every watched root in a poll. Each Cursor or Grok
thread has provider-qualified activity and turn checkpoints. A new watch starts
after the current maximum sequence, while later reads overlap the last sequence
and rely on T3 activity ids plus Side Dog's durable event ids to deduplicate a
restart safely.

The SQL query extracts only these scalar fields from projected payload JSON:

- item type, lifecycle status, and tool-call id;
- command text, used transiently by Side Dog's existing test/Git classifier;
- changed-file paths, resolved against the thread working folder.

Supported command and file-change rows go through Side Dog's shared tool
normalizer. Commands retain only classified events; failed commands retain the
program name. Paths outside the watched Git root are rejected before an event
is persisted. Completed projection turns become turn-boundary events.

Side Dog never selects `projection_thread_messages`, `orchestration_events`,
provider logs, or a whole projected payload. It never persists prompts,
responses, reasoning, diffs, patches, file contents, full commands, stdout, or
stderr.

## Known limits

- Cursor and Grok are visible only when launched through a local T3 Code store.
- Remote T3 Code servers and the WebSocket API are not supported.
- Unknown and privacy-ambiguous projected activity kinds are skipped.
- T3 Code schema changes fail closed until Side Dog's required-column contract
  is reviewed and updated.
