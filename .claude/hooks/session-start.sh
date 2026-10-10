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
