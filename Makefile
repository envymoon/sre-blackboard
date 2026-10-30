.PHONY: install fmt lint typecheck test check db migrate run worker up down token audit-verify loadtest \
	llm-smoke llm-probe env-up env-down faults fault recover lab-status lab-probe lab-accept lab-env lab-workers rules-test

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

audit-verify:  ## Recompute the audit hash chain; fails at the first tampered row
	uv run python -m app.db.audit verify

loadtest:  ## 1000 duplicate alerts against a running stack (see scripts/loadtest.py)
	uv run python -m scripts.loadtest

llm-smoke:  ## One real agent step through claude -p (needs a logged-in Claude Code); prints usage
	uv run python -m scripts.llm_probe

llm-probe:  ## Everything the M4 probe checks: final answers, tool isolation, error shapes
	uv run python -m scripts.llm_probe --all

# --- Fault lab (M2): kind + OpenTelemetry Demo; see deploy/kind/ and docs/learn/m2-fault-lab.md ---
PROMTOOL_IMAGE ?= prom/prometheus:v3.14.0

env-up:  ## Create or update the lab; start the API first (make up)
	./deploy/kind/up.sh

env-down:
	./deploy/kind/down.sh

faults:  ## List injectable faults
	uv run python -m scripts.lab list

fault:  ## Inject one, e.g. make fault SCENARIO=payment-failure
	uv run python -m scripts.lab inject $(SCENARIO)

recover:  ## Undo one (SCENARIO=...) or everything (ALL=1)
	uv run python -m scripts.lab recover $(if $(ALL),--all,$(SCENARIO))

lab-status:  ## Active faults and firing alerts
	uv run python -m scripts.lab status

lab-probe:  ## Per-service request rate, error ratio and p95 right now
	uv run python -m scripts.lab probe

lab-accept:  ## Acceptance: inject, alert within 5 min, diagnosis (M4), approved repair (M5), recover; needs lab-workers
	uv run python -m scripts.lab accept --diagnose --remediate

lab-env:  ## Endpoints and a 12h read-only token for the tools (python -m app.tools), into .lab.env
	uv run python -m scripts.lab env > .lab.env

lab-workers:  ## Point the compose workers at the lab (read-only token), start the executor (its own token), send traces to the lab's Jaeger; see deploy/kind/compose.lab.yml
	uv run python -m scripts.lab env --internal > .lab.worker.env
	uv run python -m scripts.lab env --internal --executor > .lab.executor.env
	docker compose -f docker-compose.yml -f deploy/kind/compose.lab.yml up -d --no-deps worker executor api

rules-test:  ## promtool unit tests for the lab's SLO alerts and the system's own alerts (needs Docker)
	docker run --rm -v $(CURDIR)/deploy/kind/rules:/rules -w /rules/tests --entrypoint promtool $(PROMTOOL_IMAGE) test rules slo_test.yml
	docker run --rm -v $(CURDIR)/deploy/observability/rules:/rules -w /rules/tests --entrypoint promtool $(PROMTOOL_IMAGE) test rules sre_blackboard_test.yml
