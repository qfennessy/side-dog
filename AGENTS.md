# Side Dog – Agent Guide

Side Dog is a narrow terminal timeline and local browser panel for watching
coding agents work. It reads private local files from nine supported agents
(Codex, Claude Code, Pi, OpenCode, Cursor, Grok, DeepSeek Harness, Cline,
Antigravity) and displays edits, tests, Git activity, PRs, and agent turns
in real time.

---

## Commands

```sh
# Install / sync dependencies
uv sync --locked

# Run tests (the only test runner; no pytest)
uv run python -m unittest discover -s tests -q

# Run a single test module
uv run python -m unittest tests.test_basics -v

# Build wheel + sdist
uv build

# Check wheel metadata
uvx twine check dist/*
```

CI runs Python 3.11, 3.12, and 3.13 on Ubuntu and macOS. Tests must pass on
all six combinations.

---

## Module Map

| File | Role |
|---|---|
| `side_dog/cli.py` | ~11,700-line monolith: CLI parser, watch loop, rendering, all per-agent collectors |
| `side_dog/integrations.py` | Typed boundary objects (`SafeEvent`, `AgentIdentity`, `SessionKey`, `StreamCheckpoint`, `IntegrationDescriptor`) and the `INTEGRATIONS` registry |
| `side_dog/model.py` | Pure event-transformation helpers (no I/O) |
| `side_dog/privacy.py` | Privacy policy: validates raw `EventObservation` → `SafeEvent` |
| `side_dog/polling.py` | Shared polling lifecycle (`PollCoordinator`, `PollTarget`, `CheckpointStore`) |
| `side_dog/panel.py` | Browser panel (embedded HTTP server + SSE + inline JS) |
| `side_dog/config.py` | Optional TOML config at `~/.config/side-dog/config.toml` |
| `side_dog/notify.py` | Desktop notifications (`osascript` on macOS, `notify-send` on Linux) |
| `side_dog/doctor.py` | `side-dog doctor` health-check logic |
| `side_dog/t3code.py` | Read-only access to T3 Code's projected SQLite state |

**`cli.py` is deliberately a single large file.** Do not split it without
understanding this choice; import cycles with `integrations.py` are avoided
through `LazyCliCallable`.

---

## Architecture and Data Flow

### Privacy boundary (most important invariant)

Raw collector output lives in memory as `EventObservation` (in
`privacy.py`). Only `SafeEvent` instances created by `safe_event()` /
`safe_events()` may be:

- written to the per-project JSONL history
- sent to the browser panel
- returned from `PollCoordinator`

`SafeEvent` is a frozen dataclass that validates every field on construction.
`SAFE_EVENT_FIELDS` and `SAFE_EVENT_KINDS` are closed sets; adding a field
requires updating them in `integrations.py` and then reviewing the privacy
implications. `PrivacyRejection` never copies the rejected input into its
message.

### State storage

```
~/.local/state/side-dog/projects/{sha256(root)}/events.jsonl
                                                native-events.sqlite3
```

Override with `SIDE_DOG_STATE_DIR`. State is disposable; configuration is not.

### Configuration

`~/.config/side-dog/config.toml` (override parent with `XDG_CONFIG_HOME`).
Parse errors are silently ignored and defaults are used – a malformed file
must never prevent the pane from starting.

### Integration registry

`INTEGRATIONS` in `integrations.py` is the single source of truth for all
nine agents. Each `IntegrationDescriptor` carries:

- `provider` – canonical kebab-case name (e.g. `"claude-code"`, `"deepseek"`)
- `aliases` – alternate spellings that `normalize_provider()` maps to the canonical name
- `capabilities` – frozenset of `IntegrationCapability` values
- `identity_loader`, `metadata_loader`, `working_folders_loader` – `LazyCliCallable` references into `cli.py`
- `readiness_probe` – `LazyCliCallable` into `doctor.py`

`LazyCliCallable` defers import until call time to break the `cli ↔
integrations` import cycle. `herdr` and `t3code` are context providers, not
agents; they are **not** in `INTEGRATION_REGISTRY`.

### Watch loop

`watch()` in `cli.py` manages a list of `WatchRootState` objects. Each root
gets its own JSONL file and agent-stream cursors. A `PollCoordinator` in a
`ThreadPoolExecutor` calls each integration adapter every poll interval;
results arrive as `PollBatch` objects containing validated `SafeEvent`
instances and updated `StreamCheckpoint` positions.

### Serde pattern

Every typed boundary object uses `from_wire(mapping)` / `to_wire() → dict`.
`SafeEvent.from_wire()` enforces `SAFE_EVENT_FIELDS`; any unknown key raises
`ValueError`.

---

## Testing Patterns

- All tests use stdlib `unittest`, no pytest.
- Test files are named `test_<topic>.py` and contain `TestCase` subclasses.
- Integration fixtures are minimal: `unittest.mock.patch` for I/O, `tempfile.TemporaryDirectory` for on-disk state.
- **`test_integration_conformance.py`** hardcodes `len(INTEGRATIONS) == 9`. Adding an agent breaks this test; update the assertion.
- **`test_integration_registry.py`** asserts the exact set of `CODING_AGENT_PROVIDERS`. Update it when the registry changes.
- **`test_readiness_registry.py`** asserts that every integration has a `readiness_probe` and specific environment overrides. Extend it when adding an agent.
- **`test_ci_workflow.py`** parses the `.github/workflows/ci.yml` YAML as text and asserts specific strings. Changing the CI file may break this test.
- **`test_readme_support.py`** likely validates claims in `README.md` against the code; keep it in mind when updating the README's agent support table.

---

## Key Conventions

### Provider names

Provider strings are **kebab-case** (e.g. `claude-code`, not `claude_code`).
`normalize_provider()` canonicalizes aliases and always returns a string; it
never raises. Unknown valid names pass through unchanged.

### Event kinds (closed set)

`branch`, `command`, `commit`, `config`, `file`, `github`, `issue`, `merge`,
`pr`, `push`, `search`, `session`, `test`, `todo`, `worktree`

Adding a kind requires updating `SAFE_EVENT_KINDS` in `integrations.py` and
typically the rendering logic in `cli.py`.

### Frozen dataclasses

All typed boundary objects are `@dataclass(frozen=True, slots=True)`. They
validate and normalize in `__post_init__` using `object.__setattr__` (the
only way to write to frozen fields). Never pass mutable dicts through a
persistence or panel boundary; use the typed objects.

### ANSI rendering

`cli.py` defines an `ANSI` dict of escape sequences by name and renders to a
fixed-width string. The `render()` function takes `width`, `height`, `color`
flags. Tests call `render(..., color=False)` to get plain text for assertions.

---

## Gotchas

- **`cli.py` imports everything** – virtually all functionality lives there. When adding a new per-agent collector, add it to `cli.py` and register it via `LazyCliCallable` in `integrations.py`.
- **T3 Code scopes worktrees per session** – identities for Cursor/Grok are keyed to their T3 thread, not just the worktree path. See `t3code.py` and issue #77 context in recent commits.
- **`NormalizedEvent` and `ActivityEvent` are aliases** for `SafeEvent` kept for backward compatibility. Use `SafeEvent` in new code.
- **Herdr is not an integration** – `integration_for("herdr")` returns `None`. Herdr provides context (pane/tab/workspace IDs) consumed by other integrations.
- **Config parse errors are silent** – `read_toml()` catches all exceptions and returns `{}`. There is no validation error shown to users for malformed config.
- **`notify-send` branch is unreachable on macOS** – pyrefly reports this as unreachable code in `notify.py`. This is expected; the branch is correct for Linux.
- **State vs. config** – `~/.local/state/side-dog` holds disposable runtime data (events, checkpoints). `~/.config/side-dog/config.toml` holds hand-authored settings. The two are strictly separate; never write config from a watch loop.
- **`--save` spaces** – `side-dog watch --save <name>` writes to `~/.config/side-dog/spaces.toml`, which is rewritten wholesale. Hand-authored spaces belong in `config.toml` under `[spaces]` to survive a `--save`.
- **Column layout** – a worktree root only gets its own column once it has activity *or* a directly-seated agent. An empty worktree adopted early won't steal layout width.

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `SIDE_DOG_STATE_DIR` | Override state directory (default: `~/.local/state/side-dog`) |
| `XDG_CONFIG_HOME` | Override config parent (default: `~/.config`) |
| `CODEX_HOME` | Custom Codex data directory |
| `PI_CODING_AGENT_DIR` | Custom Pi session directory |
| `XDG_DATA_HOME` | OpenCode data parent |
| `T3CODE_HOME` | Custom T3 Code base directory |
| `DSH_HOME` | DeepSeek Harness sessions directory |
| `CLINE_DIR` / `CLINE_DATA_DIR` / `CLINE_DB_DATA_DIR` / `CLINE_SESSION_DATA_DIR` | Cline data locations |
| `ANTIGRAVITY_APP_DATA_DIR` / `GEMINI_HOME` | Antigravity CLI data |

---

## Release Preparation

For every request to bump Side Dog's version or prepare a patch, minor, or
major release, you **must use** the project skill
`prepare-side-dog-release` before changing `side_dog.__version__` or
`CHANGELOG.md`. Follow the skill's increment selection, dry-run, validation,
and pull-request workflow. Do not substitute a manual version edit.

Using the skill does not authorize a Git tag, GitHub release, TestPyPI upload,
or PyPI publication. Those external release actions always require separate,
explicit user authorization.
