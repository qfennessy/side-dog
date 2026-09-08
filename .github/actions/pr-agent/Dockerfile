# Base: upstream's published GitHub Action image, pinned by digest so it cannot
# change out of band. Provides Python, the dependency set (/app/.venv, uv.lock),
# PYTHONPATH=/app and /entrypoint.sh, which runs
# /app/pr_agent/servers/github_action_runner.py.
#
# pragent/pr-agent:github_action as published 2026-09-05. To bump: read the
# new tag's digest from Docker Hub and diff /app/uv.lock against upstream's at
# the source commit below; on 2026-09-07 the only drift was litellm 1.99->1.100.
FROM pragent/pr-agent:github_action@sha256:548b760b81ab4b3f729182428695ccc1194bbf87528c2b1e2b2b07e5223af7b6

# Source: The-PR-Agent/pr-agent at an exact commit, replacing the package the
# image shipped with. The image's copy predates fixes this repository needs --
# on 2026-09-07 upstream main had the github-actions[bot] identity fallback
# (#3072) that lets the workflow token update the persistent review comment,
# and the published image did not. Bump deliberately, reading the upstream diff
# first: this code runs upstream-authored prompts with this repository's
# OpenRouter key and a write-scoped GITHUB_TOKEN.
ARG PR_AGENT_COMMIT=6952c4783a1d0291ae8bb153d421b77da6842121
ADD https://github.com/The-PR-Agent/pr-agent/archive/${PR_AGENT_COMMIT}.tar.gz /tmp/pr-agent-src.tar.gz
RUN set -eu \
  && tar -xzf /tmp/pr-agent-src.tar.gz -C /tmp \
  && rm -rf /app/pr_agent \
  && mv "/tmp/pr-agent-${PR_AGENT_COMMIT}/pr_agent" /app/pr_agent \
  && rm -rf /tmp/pr-agent-src.tar.gz "/tmp/pr-agent-${PR_AGENT_COMMIT}" \
  && python -c "import pr_agent.servers.github_action_runner"
