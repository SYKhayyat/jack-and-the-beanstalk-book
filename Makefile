.PHONY: install dev test migrate lint typecheck check clean

VENV := .venv
PY   := $(VENV)/bin/python

install: ## Create the venv and install everything
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --requirement requirements-dev.lock

dev: ## Run with reload in mock mode (no API key needed)
	$(VENV)/bin/uvicorn app.main:create_app --factory --reload --port 8000

test: ## Run the suite with coverage
	$(VENV)/bin/python -m pytest --cov=app --cov-report=term-missing

migrate: ## Apply database migrations (needs DATABASE_URL in the environment)
	$(VENV)/bin/python -m alembic upgrade head

lint: ## Check formatting and lint rules
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

typecheck: ## Static types
	$(VENV)/bin/mypy app

check: lint typecheck test ## Everything CI runs

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
