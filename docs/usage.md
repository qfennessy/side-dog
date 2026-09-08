# Tokens and estimated cost

### Token usage and estimated spend

Side Dog can optionally read [ccusage](https://ccusage.com/) JSON reports and
show a compact token and API-equivalent cost summary in `watch` and the browser
panel. The dependency stays optional: when `ccusage` is absent, normal activity
collection continues and `side-dog doctor` reports token usage as unavailable.

Install `ccusage` separately so its executable is on `PATH`, then inspect a
report without starting the live display:

```sh
side-dog usage daily
side-dog usage monthly --since 2026-01-01 --json
side-dog usage session --root .
```

Use `--agent`, `--since`, and `--until` to narrow a report, `--no-cost` when
only token counts should be shown, or `--cost-mode` to pass an explicit
ccusage cost mode. Cost is labelled as estimated, recorded, unpriced, omitted,
partial, unavailable, or stale rather than silently treated as exact. The
`API est` figure applies public API prices to token counts in local agent logs.
It is useful for comparing activity, but it is not a subscription bill and may
not match a provider invoice.

`--root` is supported only with the `session` view, where Side Dog can filter
to sessions it has associated with that folder. ccusage does not expose enough
project information to scope daily or monthly reports honestly, so those
combinations are rejected with an explanatory message.

The live header combines two independently captured views into one gauge:

```text
API est · 5h $23.00 ▰▰▰▰▱▱▱▱ 2h 26m left · pace $10.48/hr · today $88.95 · as of 10:33
```

The bar shows elapsed time in the active five-hour block. Its cost, pace, and
time left are machine-wide; today's figure is scoped to the shown folders.
The single `as of` time is the oldest capture used by the line. On narrow
terminals the line drops the capture time, then the bar and pace before
dropping today's total. An extremely narrow pane keeps only the five-hour API
estimate. If pricing is partial, the unpriced model and token count replace the
capture time so the gap stays visible.

When a figure is missing, the line says why rather than calling everything
unavailable:

| Line reads | Meaning |
| --- | --- |
| `usage loading` | The first reports have yet to arrive. A figure that arrives first is shown beside `today loading`. |
| `ccusage not installed` | The configured command is not on `PATH`. |
| `usage off in config` | `enabled = false` under `[usage]`. |
| `no active block` | No local agent usage in the current five-hour window. |
| `ccusage timed out` | The report did not finish in time. |
| `ccusage will not start` | The command could not be launched. |
| `ccusage report failed` | `ccusage` ran and returned an error, or its output could not be read. |
| `unavailable` | Any state Side Dog does not recognise. |

Side Dog matches these against states it produced itself, so `ccusage` error
text is never copied into the pane; an unrecognised state falls back to
`unavailable` rather than printing what the command said. `no matched
sessions` means the report arrived and covered none of the shown folders'
sessions, which is different from a refresh that failed. No bar is drawn
without a block report.

Expanded usage details retain the three underlying views:

- **Today** totals provider-qualified ccusage sessions associated with the
  shown root or roots since the start of the current day.
- **Current 5-hour window** is machine-wide. It covers all local agent usage
  ccusage can identify and shows the API estimate per hour plus time left in
  the rolling window.
- **Tracked lifetime** totals the matched sessions Side Dog has seen for the
  shown root or roots. “Tracked” is deliberate: this is not an account-wide
  billing ledger.

The terminal status bar names Side Dog and its installed version, describes
the visible scope as a folder name, `all N folders`, or `N/M folders`, and
shows how many agents are working. The clock stays at the right edge, and
`╱` stripes fill the space between the two, so the top line reads as a
masthead rather than another divider. In color the name is purple and the
stripes run from purple to blue; the stripes are decoration and never carry
meaning. In a narrow pane, the stripes go first, then the working count, then
scope, then version; the Side Dog name and clock remain for as long as the
pane can fit them.

An all-folder view aggregates today's and tracked-lifetime associations across
its shown roots. The five-hour window remains machine-wide, regardless of
focus.

The terminal roster and the browser's expanded usage details show privacy-safe
Side Dog task labels and active/idle state. The terminal's expanded header
(`E`) reveals folder paths, discovery mode, and usage totals, and `u` lists
the per-session contributions, lifetime totals, and last activity under the
gauge. The expanded header keeps to about forty percent of the pane so the
timeline keeps the rest; when it overflows, the folder list folds first.
Neither view exposes raw session IDs. You do not
need to terminate an agent session to see its estimate: the active block is
refreshed about every 10 seconds, while the more expensive session scans are
staggered and refreshed every few minutes. Finished sessions stay in **Tracked
lifetime**.

Side Dog tries current online model prices first and falls back to ccusage's
cached price list. Each snapshot records the pricing source and capture age;
unknown models are named with their unpriced token count, and failed or old
refreshes retain the last good values marked stale. Usage snapshots remain in
memory, so pausing freezes the displayed values and resuming catches up. Raw
ccusage rows are never written to Side Dog's event history or sent to a panel.

