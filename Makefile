.PHONY: install fmt lint typecheck test check db migrate run worker up down token loadtest

install:  ## Install runtime + dev dependencies and git hooks
	uv sync
	uv run pre-commit install

fmt:  ## Auto-format and apply safe lint fixes
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

test:  ## Needs Docker (testcontainers), or SRE_TEST_DATABASE_URL pointing at a scratch database
	uv run pytest

check: lint typecheck test  ## Everything CI runs

db:  ## Start only Postgres (for running the API and worker on the host)
	docker compose up -d db

migrate:
	uv run alembic upgrade head

run:  ## API on :8000 with reload
	uv run uvicorn app.main:create_app --factory --reload

worker:
	uv run python -m app.worker

up:  ## Whole stack in Docker: db, migrations, API (2 processes), 2 workers
	docker compose up --build -d

down:
	docker compose down

token:  ## Print a token, e.g. make token ROLES=ingest TENANT=t1
	@uv run python -m app.core.security mint --sub $${SUB:-dev} --tenant $${TENANT:-t1} --roles $${ROLES:-viewer}

loadtest:  ## 1000 duplicate alerts against a running stack (see scripts/loadtest.py)
	uv run python -m scripts.loadtest
