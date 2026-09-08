# Terminal recordings

The README contains exactly two terminal GIFs: Watch and Board's live-session
roster (not its contributions view). Both use synthetic fixtures with the
production renderers in `scripts/terminal_demo.py`. No real sessions, history,
credentials, or GitHub data are collected. These are illustrative animations,
not end-to-end interaction tests.

From a checkout with Python dependencies installed, install
[VHS](https://github.com/charmbracelet/vhs) and its ffmpeg/ttyd dependencies.
Ensure the project's Python environment is first on PATH, then run:

```sh
vhs docs/watch.tape
vhs docs/board.tape
```

Inspect early and late frames of both GIFs before committing. Watch should
accumulate activity; Board should retain its roster columns while selection,
checks, and session status change. Keep the same dimensions and theme in both
tapes. The fixtures intentionally use fictional projects and PRs.

PyPI uses the README embedded in distribution metadata at publication time.
Merging a README change does not update an already-published version's
description. The next package release must contain the updated README. Image
URLs are absolute so GitHub and PyPI can both display them.
