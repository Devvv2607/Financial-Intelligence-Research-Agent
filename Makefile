# Task runner for Linux/macOS/CI and Windows Git Bash.
# Windows PowerShell users: `.\make.ps1 <target>` runs the same targets.
# Keep the two in sync — every target here has a twin in make.ps1.

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test check up down logs ready dev-api dev-web web-check clean migrate ingest-sample ingest-full ingest-report

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install Python (uv) and frontend (npm) dependencies
	uv sync
	npm --prefix frontend ci

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-fix lint and format
	uv run ruff check . --fix
	uv run ruff format .

typecheck: ## mypy (strict)
	uv run mypy

test: ## Unit + agent tests (no live services)
	uv run pytest

web-check: ## Frontend lint + typecheck
	npm --prefix frontend run lint
	npm --prefix frontend run typecheck

check: lint typecheck test web-check ## Everything CI runs, minus the frontend build

up: ## Build and start the full stack
	docker compose up -d --build

down: ## Stop the stack (keeps data volumes)
	docker compose down

logs: ## Tail stack logs
	docker compose logs -f --tail=100

ready: ## Query the API readiness probe
	curl -s -w "\nHTTP %{http_code}\n" http://localhost:8000/ready

migrate: ## Apply database migrations
	uv run alembic -c backend/alembic.ini upgrade head

ingest-sample: migrate ## Ingest the 10 agreed sample companies (+ headings-only experiment)
	uv run python -m ingestion run --sample 10 --experiment-headings-only

ingest-full: migrate ## Ingest the whole universe (resumable)
	uv run python -m ingestion run

ingest-report: ## Write the ingestion report for the sample (REPORT_ARGS= for others)
	uv run python -m ingestion report $(or $(REPORT_ARGS),--sample 10)

dev-api: ## Run the API on the host with reload
	uv run uvicorn api.main:app --reload --port 8000 --loop core.aio:selector_loop

dev-web: ## Run the frontend dev server
	npm --prefix frontend run dev

clean: ## Remove caches and build output
	rm -rf .mypy_cache .ruff_cache .pytest_cache frontend/.next
