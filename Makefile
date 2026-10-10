.PHONY: install fmt lint typecheck test check run

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

test:
	uv run pytest

check: lint typecheck test  ## Everything CI runs

run:  ## Start the API on :8000
	uv run uvicorn app.main:app --reload
