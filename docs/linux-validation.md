# Linux runtime validation

Tracking [issue #194](https://github.com/qfennessy/side-dog/issues/194).
These checks exercise an installed application with synthetic activity. They do
not establish that every integration works on a user's Linux desktop.

## Environment and reproduction

September 8, 2026: Ubuntu 24.04.4 LTS, arm64 Docker container on Docker Desktop,
Python 3.12.3, UTF-8 Linux pseudo-terminal (`TERM=xterm-256color`), no Herdr or
GitHub CLI. The wheel reports 2.0.0; it is built from the changes in this PR on
source commit `5b20dbcb604ec7bd90d9f4408c484cc1e945979c`
(base `dcba7cf`), **not** the published 2.0.0 wheel. Python imports were
verified under `/opt/smoke/lib/python3.12/site-packages/side_dog` outside the
source checkout. Candidate wheel SHA-256:
`2a4e49e3e7f9c4cf2a39f0812905da6e2bfab2971f1ec5f4c34986f7911161f4`.
Display checks use Xvfb `:99`, not a physical desktop.

Build from the desired checkout, record its exact commit and wheel checksum,
then mount it read-only into a disposable Ubuntu container:

```sh
uv build
git rev-parse HEAD
sha256sum dist/*.whl
docker run --rm -it -v "$PWD:/source:ro" ubuntu:24.04 bash
```

Inside the container:

```sh
apt-get update
apt-get install -y python3 python3-venv git curl
python3 -m venv /opt/smoke
/opt/smoke/bin/pip install /source/dist/*.whl
export PATH="/opt/smoke/bin:$PATH"
cd /tmp
python /source/scripts/linux_smoke.py
```

The smoke script creates temporary HOME, XDG config/data and Side Dog state
folders, a Git repository and worktree, and synthetic failed/successful events.
It drives real PTYs at 100, 42 and 28 columns, sends help/View/navigation/quit
keys, checks UTF-8 output and no-color output, and compares terminal attributes
before and after exit. It also opens the panel over HTTP and shuts it down.
CI runs this script against its installed wheel after the six unit-test jobs.

For real browser execution (optional; downloads Chromium):

```sh
pip install playwright
playwright install --with-deps chromium
python /source/scripts/linux_smoke.py --browser
```

For the virtual display variant, install `xvfb dbus-x11 libnotify-bin
xfce4-notifyd`, start Xvfb, and run the same command with `DISPLAY=:99` inside
`dbus-run-session`. Playwright opens a headed Chromium window when DISPLAY is
set. A notification service can be started through D-Bus by `notify-send`.
No private agent configuration or credentials need to be mounted.

## Results

| Check | Outcome | Sanitized evidence / limit |
| --- | --- | --- |
| Clean wheel install and version | PASS | Isolated Python environment; installed package version 2.0.0 |
| uv tool install, PATH update, force reinstall, uninstall | PASS | `uv tool update-shell` updated disposable shell files; executable version matched before/after reinstall; executable removed on uninstall |
| Upgrade from an older published release | NOT TESTED | Force reinstall of the candidate is not a cross-version upgrade |
| Watch explicit root and automatic discovery without Herdr | PASS | PTY startup, resize, key input, quit, terminal restoration; automatic mode had no real agent to discover |
| Multiple Git worktrees | PASS | Repository and second worktree present during Watch; real agent assignment to worktrees not tested |
| Board without gh or optional agents | PASS | Empty roster remained responsive in the PTY |
| Unicode/color and no-color | PASS | UTF-8 decoding succeeded; no SGR color escapes in no-color runs |
| Demo quit | FAIL → FIXED | Viewer exited 0 on `q`, wrapper returned 1; wrapper now preserves the successful exit code and still propagates failures |
| Chromium browser / panel lifecycle | PASS | Playwright 1.62.0, headless Chromium: failed test rendered, subsequent successful test appeared live, Board SSE connected, no JavaScript errors, SIGINT shutdown returned 0 |
| Failed-test notifications | PASS | Watch/panel regression tests retain events without notification dispatch; removed failure-only rule and workers |
| notify-send missing / no session service | PASS | Installed notification adapter returned without error in under 0.01 seconds in each environment |
| Virtual desktop notification service | PASS | notify-send returned 0; D-Bus reported Xfce Notify Daemon 0.9.4, specification 1.2 |
| Physical desktop / SSH terminal | BLOCKED | No physical Linux desktop or SSH host supplied; Docker/Xvfb is not that environment |
| Real authenticated coding-agent activity | BLOCKED | No authenticated Linux agent environment supplied |
| Other supported agent fixture coverage | NOT TESTED | This harness uses synthetic Side Dog events, not native transcripts for each integration |
| Unauthenticated/offline gh with PR-bearing sessions | NOT TESTED | Missing gh covered; real GitHub readbacks not exercised here |
| XDG overrides and privacy-safe persistence | PASS | Disposable paths and approved synthetic event fields; no private transcripts in evidence |

Issue #194 stays open for the blocked real-agent run and outstanding environment
checks. No package publication or personal configuration changes are part of
this validation. Test-failure popups are removed by issue #145; Board transition
alerts still need a notification service and remain opt-in.
