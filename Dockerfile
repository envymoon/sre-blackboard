FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/srv/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY alembic.ini ./
COPY app/ app/
COPY kb/ kb/
ENV PATH="/srv/.venv/bin:$PATH"
RUN useradd --system --uid 10001 app
USER app
EXPOSE 8000
# The API is stateless (all state is in Postgres), so it can run several processes.
# The same image runs the worker: `python -m app.worker`.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
