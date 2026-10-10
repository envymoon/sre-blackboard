#!/bin/bash
# SessionStart hook: prepares Claude Code cloud sessions so tests run out of the box.
set -euo pipefail

# Only run in Claude Code cloud sessions; local machines manage their own env.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# Idempotent: pip skips packages that are already satisfied (container state is cached).
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt

# Persist env vars for the whole session.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PYTHONPATH=\"$PWD\${PYTHONPATH:+:\$PYTHONPATH}\"" >> "$CLAUDE_ENV_FILE"
fi
