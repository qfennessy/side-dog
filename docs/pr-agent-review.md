# Automatic PR review

Side Dog uses upstream `The-PR-Agent/pr-agent` with one GPT-5.6 Luna reviewer
at high reasoning effort through OpenRouter. DeepSeek V4 Flash is the fallback.
The action runs only review automatically; description and code suggestions
remain opt-in slash commands. Existing Codex reviews are independent.

## Setup and behavior

A maintainer must add the Actions repository secret `OPENROUTER_API_KEY`.
The workflow injects it as `openrouter__key`; no provider key belongs in Git.

Non-draft, same-repository PRs targeting `main` are reviewed on open, reopen,
ready-for-review, and subsequent pushes. Fork PRs do not receive a reviewer
step. Trusted owners, members, and collaborators can invoke `/review` on a PR;
the comment path checks the head repository before starting the action and
refuses fork or unknown heads. Bot and ordinary discussion comments do not
cancel an in-flight review.

The action is checked out from the PR's base SHA, or the default branch on a
comment event. Never change this to a PR head checkout: the action receives
the provider secret and a GitHub token allowed to comment. Checkout does not
persist Git credentials. The initial action/configuration bootstrap lands
before the workflow so the trusted base already contains it.

## Publication and lockfiles

A successful `/review` creates or updates one `PR Reviewer Guide` comment
owned by `github-actions[bot]`, with `<!-- pr-agent:review:full -->` on its own
line within the first three lines. The guard requires an `updated_at` at or
after this run's start. A successful process exit alone is insufficient:
missing, stale, or unreadable publication makes `PR Agent review` red.

Upstream skips generated lockfiles. A PR changing only `uv.lock` (including a
nested one) or the other listed package lockfiles skips the reviewer and
records that no review is expected. Mixed source/lockfile PRs are reviewed.
The guard tests extract and execute the workflow shell, including its jq
filters, as part of the normal stdlib unittest suite; CI requires jq.

## Bounds and pins

Each model has a 240-second timeout, with zero provider retries and no
same-model timeout retry. The workflow has a ten-minute job timeout. These
values appear in both `.pr_agent.toml` and workflow environment variables
because upstream reads repository configuration from the default branch.

Pins verified September 8, 2026:

- Image: `pragent/pr-agent:github_action@sha256:548b760b81ab4b3f729182428695ccc1194bbf87528c2b1e2b2b07e5223af7b6`.
- Source: `The-PR-Agent/pr-agent@6952c4783a1d0291ae8bb153d421b77da6842121`.

Docker Hub still served the September 5 image, which predates the Actions
identity fallback from upstream #3072. The Dockerfile overlays the pinned
upstream package and checks that the action runner imports. The wrapper is
copied from Cocos Story's source-pinned setup, not `qfennessy/pr-agent`.

Before changing pins, inspect the upstream diff and image dependency drift,
verify the source commit, build the local action without secrets, and run
`python -m unittest tests.test_pr_agent_action tests.test_pr_agent_workflow -v`.
Do not replace the wrapper with upstream's mutable-image action.

## Live acceptance

After setup, use a small same-repository PR to verify a reviewer-guide comment
is published. Record its ID and `updated_at`, push a second reviewable change,
then verify that the same ID updates and no Standalone PR Review appears.
Check both `PR Agent review` runs are green. Also exercise a lockfile-only PR
and a fork PR; the former records nothing to review and the latter starts no
credential-bearing reviewer step. Record links and sanitized observations in
the implementation PR. Unit tests and image builds are prerequisites, not
proof that paid review publication works.
