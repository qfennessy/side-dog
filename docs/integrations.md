# Agent integrations

## Coding agent support

| Agent | Finds and names sessions | Collects live activity | Setup |
| --- | --- | --- | --- |
| **Codex** | Yes, including terminal and Codex Desktop sessions | Yes, from Codex's local session stream | None |
| **Claude Code** | Yes, including terminal, desktop, and editor sessions | Yes, after project hooks are installed | Optional project hooks: run `side-dog setup . --claude`, then restart Claude Code |
| **Pi** | Yes | Yes, from Pi's local session files | None |
| **Oh My Pi** | Yes | Yes, from Oh My Pi's local session files | None |
| **OpenCode** | Yes | Yes, from OpenCode's local SQLite store | None |
| **Crush** | Yes | Yes, from Crush's local SQLite stores | None |
| **Cursor Agent** | Yes, when launched through T3 Code | Yes, from T3 Code's projected activity store | None |
| **Grok Build** | Yes, when launched through T3 Code | Yes, from T3 Code's projected activity store | None |
| **DeepSeek Harness** | Yes | Yes, from Harness session logs | None |
| **Cline** | Yes, across CLI, editor, desktop, and background sessions | Yes, from Cline's local session store | None |
| **Antigravity CLI** | Yes | Yes, from Antigravity's local history and transcripts | None |
| **Muse Code** | Yes, including persistent subagents | Yes, from Muse Code's local event logs | None |

### Optional context

Herdr and T3 Code are not coding agents. They add context to the agents above.

| Context provider | What it adds | How Side Dog uses it | Setup |
| --- | --- | --- | --- |
| **Herdr** | Adds pane, tab, workspace, and terminal-title details | Routes activity to the right terminal context | Optional |
| **T3 Code** | Adds thread title, provider, status, and worktree details | Supplies projected activity for Cursor and Grok Build | Optional; no Side Dog hooks |

Most people do not need to set data-location variables. If an agent stores its
data somewhere custom, Side Dog honours `CODEX_HOME` for Codex,
`PI_CODING_AGENT_DIR` for Pi, `OMP_AGENT_DIR` for Oh My Pi,
`XDG_DATA_HOME` for OpenCode, `MUSE_DATA_DIR` for Muse Code,
`CRUSH_GLOBAL_DATA` for Crush, `T3CODE_HOME` for T3 Code, `DSH_HOME` for
DeepSeek Harness, `CLINE_DIR`, `CLINE_DATA_DIR`,
`CLINE_DB_DATA_DIR`, and `CLINE_SESSION_DATA_DIR` for Cline, and
`ANTIGRAVITY_APP_DATA_DIR` or `GEMINI_HOME` for Antigravity CLI.

### Codex

Codex needs no hooks. Side Dog reads a privacy-filtered view of Codex's local
activity stream and identifies recent sessions by repository. This works for
Codex in a terminal, editor, or Codex Desktop. If Codex uses a custom data
folder, set `CODEX_HOME` to that folder.

### Antigravity CLI

Antigravity needs no hooks. Side Dog joins
`~/.gemini/antigravity-cli/history.jsonl` to each recent
`brain/<conversation-id>/.system_generated/logs/transcript.jsonl`, so sessions
are associated with the correct workspace and their turns, edits, commands,
tests, Git operations, and subagents appear as they happen. Set
`ANTIGRAVITY_APP_DATA_DIR` if Antigravity stores its application data
elsewhere. If that is not set, Side Dog also honours `GEMINI_HOME` as the
parent of Antigravity's data folders.

The collector uses stable per-step IDs and persistent cursors, so terminal and
browser views can run together without duplicating activity. A pending call is
replayed after restart until its result arrives. Command output is inspected
only for an exit code and is then discarded; prompts, responses, file content,
full commands, stdout, and stderr are never copied into Side Dog's feed.

### Claude Code

Side Dog can identify a live Claude Code session without setup. To see Claude's
tool activity, install Side Dog's hooks in each project:

```sh
cd ~/src/my-project
side-dog setup . --claude
```

Restart Claude Code after setup. Side Dog writes only to the machine-local
`.claude/settings.local.json` file. It preserves other hooks and does not change
the shared `.claude/settings.json` file.

Without these hooks, Claude's file changes still appear, but they are shown as
unattributed filesystem activity.

### Pi

Pi needs no hooks. Side Dog reads Pi's local session files to find the session,
model, reasoning level, and live activity. It honours `PI_CODING_AGENT_DIR` when
Pi stores its files somewhere other than the default location.

### Oh My Pi

Oh My Pi needs no hooks. Side Dog reads its local JSONL session files to find
the session, model, reasoning level, and safe live tool activity. It supports
Oh My Pi's current fixed-width title record as well as older sessions that
begin directly with the session header. Set `OMP_AGENT_DIR` when the agent data
directory is not `~/.omp/agent`.

### Muse Code

Muse Code needs no hooks. Side Dog reads its local append-only event logs,
including persistent subagent logs, to find workspace-local sessions, the
configured model, task lifecycle, and safe tool lifecycle markers. Tool
arguments, command text, file paths, output, prompts, responses, and reasoning
are never copied into Side Dog state. Set `MUSE_DATA_DIR` when Muse stores its
data somewhere other than `~/.local/share/muse` (or `$XDG_DATA_HOME/muse`).

When Oh My Pi or Muse Code runs in a Herdr pane that has no session ID, Side Dog
joins the pane to exactly one fresh local session for the same agent and
worktree. This supplies Herdr's pane context without creating a second Board
row. If more than one local session is possible, Side Dog leaves them separate
rather than guessing.

### OpenCode

OpenCode needs no hooks. Side Dog reads its local SQLite store to find the
session, model, reasoning variant, title, activity, and subagents. It shows
edits, tests, Git operations, and small markers for context tools such as read,
search, web fetch, and todo updates. Set `XDG_DATA_HOME` if OpenCode stores its
data under a custom data-directory parent.

### Crush

Crush needs no hooks. Side Dog reads Crush's machine-wide `projects.json`
index, then opens each indexed project's `crush.db` read-only. It uses the
indexed `data_dir` exactly as Crush recorded it, including configured absolute
locations, and attributes child-agent sessions to their top-level session.

Streaming tool rows are reread with a bounded overlap and stable event IDs, so
a call that finishes after Side Dog restarts converges without replaying old
activity. Side Dog selects only session metadata and relevant tool lifecycle
scalars; prompts, responses, reasoning, command output, result payloads, diffs,
and file snapshots are not copied into its state or panel feed. Set
`CRUSH_GLOBAL_DATA` to Crush's global data directory when Crush stores its
project index somewhere other than `~/.local/share/crush`.

### T3 Code, Cursor, and Grok

T3 Code is optional context, not an agent name. When T3 Code launches Codex,
Claude Code, or OpenCode, Side Dog uses the T3 thread title and worktree while
keeping that agent's native model, reasoning level, and activity reader.

Cursor Agent and Grok Build are supported when they run through T3 Code. Side
Dog reads only narrowly selected fields from T3 Code's local projected activity
store; it does not read messages, raw orchestration events, command output, or
provider logs. No hooks are needed. T3 Code normally stores its data under
`~/.t3`; set `T3CODE_HOME` only when T3 Code uses a different base directory.

### DeepSeek Harness

DeepSeek Harness needs no hooks. Side Dog reads event-sourced sessions from
`~/.dsh/sessions`, or `$DSH_HOME/sessions` when configured. Both Harness's
default Zstandard-compressed logs and diagnostic plain JSONL are supported.
Top-level sessions show their model, reasoning effort, status, edits, tests,
Git commands, subagents, and turn completion without storing prompts,
responses, command output, diffs, or file contents.

### Cline

Cline needs no hooks. Side Dog reads its shared SQLite session database and
structured message artifacts under `~/.cline/data`, or Cline's file-backed
session manifests when SQLite is unavailable. It honours `CLINE_DIR`,
`CLINE_DATA_DIR`, `CLINE_DB_DATA_DIR`, and `CLINE_SESSION_DATA_DIR`.

Side Dog names Cline sessions with their model, task title, and status, and
shows editor, patch, command, test, Git, and subagent activity. Child-session
activity is attributed to its top-level session. Prompts, responses, tool
output, patch contents, full shell commands, and file contents are not copied
into Side Dog's event log.
