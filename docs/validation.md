# Documentation validation

Publication metadata checked September 8, 2026: PyPI hosts the 2.0.0 wheel and
source distribution. The old public package README still includes pre-publication
wording; this source documentation corrects it for the next package build.

Validation for issues #193 and #196 includes CLI help and manual generation,
unit regressions for attribution and live Board history, package contents, and
canonical and rendered documentation links. Terminal fixtures are synthetic;
no private user activity is included in documentation evidence.

The [Linux runtime matrix](linux-validation.md) records installed-wheel PTY,
PATH and notification-service checks and the remaining blockers under #194.
CI's Ubuntu/macOS Python matrix is automated runtime coverage, not evidence that
every desktop workflow was manually exercised.

## Local results

On macOS, the full 1,318-test suite passed on Python 3.13 with one platform
skip. A real terminal pseudo-TTY tour and browser demo displayed synthetic
activity with usage disabled. The wheel and source distribution built and
passed Twine metadata checks; the wheel's executable, doctor, Watch and Board
bundled man-page lookup passed isolated installation smoke checks.
Release-version validation passed, and manuals matched CLI metadata and passed
`mandoc` lint. Canonical links and Pages staging passed locally; the PR's
Documentation job also built Jekyll and validated rendered links.
