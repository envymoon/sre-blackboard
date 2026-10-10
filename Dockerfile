FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/srv/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY app/ app/
COPY kb/ kb/
ENV PATH="/srv/.venv/bin:$PATH"
EXPOSE 8000
# Single worker: ingress dedup state is in process memory until M1 moves it to Postgres.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
