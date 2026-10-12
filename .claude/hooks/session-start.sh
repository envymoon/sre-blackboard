#!/bin/bash
# SessionStart hook: prepares Claude Code cloud sessions so lint, type checks and tests run out of the box.
set -euo pipefail

# Only run in Claude Code cloud sessions; local machines manage their own env.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# Idempotent: uv only installs what is missing from the locked environment.
if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --quiet --disable-pip-version-check uv
fi
uv sync --locked --quiet

# Database tests start Postgres with testcontainers, which needs a Docker daemon.
# The cloud container ships dockerd but does not start it. Docker Hub rate-limits
# this environment, so the test image comes from Google's Docker Hub mirror instead,
# and testcontainers' Ryuk sidecar (also on Docker Hub) is switched off.
if command -v dockerd >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then
    nohup dockerd >/tmp/dockerd.log 2>&1 &
    for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
  if ! docker image inspect pgvector/pgvector:pg16 >/dev/null 2>&1; then
    docker pull -q mirror.gcr.io/pgvector/pgvector:pg16 >/dev/null \
      && docker tag mirror.gcr.io/pgvector/pgvector:pg16 pgvector/pgvector:pg16 \
      || echo "session-start: could not pull the Postgres test image; database tests will fail" >&2
  fi
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export TESTCONTAINERS_RYUK_DISABLED=true" >> "$CLAUDE_ENV_FILE"
  fi
fi
