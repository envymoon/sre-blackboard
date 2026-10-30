# Two targets from one base: `api` (also runs migrations) and `worker`, which adds the
# Claude Code CLI for the subscription backend (SRE_LLM_BACKEND=cli, the default) and the
# knowledge base's models (it also runs the ingest job).

FROM node:22-bookworm-slim AS claude-cli
# Pinned. The npm package installs a native binary (about 250 MB); only that is kept.
ARG CLAUDE_CODE_VERSION=2.1.300
RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && cp "$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe" /claude \
 && /claude --version

FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/srv/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY alembic.ini ./
COPY app/ app/
COPY kb/ kb/
ENV PATH="/srv/.venv/bin:$PATH"
# A home directory: the Claude CLI keeps its state under ~/.claude.
RUN useradd --system --create-home --uid 10001 app
USER app

FROM base AS worker
COPY --from=claude-cli /claude /usr/local/bin/claude
# The knowledge base's models (M6, app/kb/models.py: ~200 MB, CPU only), baked in so that
# neither the worker nor the ingest job downloads anything at run time.
USER root
RUN mkdir -p /srv/models \
 && SRE_KB_MODEL_DIR=/srv/models python -m app.kb models \
 && chmod -R a+rX /srv/models
USER app
ENV SRE_KB_MODEL_DIR=/srv/models SRE_KB_OFFLINE=true
# Authentication for the CLI comes from the environment: CLAUDE_CODE_OAUTH_TOKEN
# (from `claude setup-token`, uses the subscription) or ANTHROPIC_API_KEY.
CMD ["python", "-m", "app.worker"]

FROM base AS api
EXPOSE 8000
# The API is stateless (all state is in Postgres), so it can run several processes. Their
# Prometheus counters are files in PROMETHEUS_MULTIPROC_DIR, summed on scrape (M8); the
# directory is emptied at every start so a restart does not carry old processes' values.
# Set here, not with ENV: the executor runs from this image as one process with its own registry.
CMD ["sh", "-c", "export PROMETHEUS_MULTIPROC_DIR=/tmp/sre-metrics && rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\" && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --workers 2"]
