.PHONY: install dev test test-fast migrate lint typecheck check clean \
        plates-prompts plates-make plates-import

VENV := .venv
PY   := $(VENV)/bin/python

install: ## Create the venv and install everything
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --requirement requirements-dev.lock

dev: ## Run with reload in mock mode (no API key, no database server needed)
	$(VENV)/bin/uvicorn app.main:create_app --factory --reload --port 8000

test: ## Run the suite with coverage
	$(VENV)/bin/python -m pytest --cov=app --cov-report=term-missing

test-fast: ## Run the suite in parallel, without coverage (the inner loop)
	$(VENV)/bin/python -m pytest -n auto --dist loadscope

migrate: ## Apply database migrations (empty DATABASE_URL = the local SQLite file)
	$(VENV)/bin/python -m alembic upgrade head

seed: ## Load the story seeds into the database (idempotent; run after migrate)
	$(VENV)/bin/python -m app.scripts.seed_stories

lint: ## Check formatting and lint rules
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

typecheck: ## Static types
	$(VENV)/bin/mypy app

check: lint typecheck test ## Everything CI runs

# --- plates ------------------------------------------------------------------
# Plates are painted once per story. Neither of the first two makes an image
# call; the third brings pictures painted anywhere back in. STORY and DIR are
# overridable: `make plates-prompts STORY=little_red_riding_hood`.

STORY ?= jack_and_the_beanstalk
DIR   ?= plates/$(STORY)

plates-prompts: ## Write every plate prompt as DIR/<page>.txt (no image calls)
	$(PY) -m app.scripts.plate_prompts --story $(STORY) --out $(DIR)
	@echo "Paint each one, save it as its page number, then: make plates-import"

plates-make: ## Paint the plates through the configured provider (costs money)
	$(PY) -m app.scripts.make_plates --story $(STORY) --contact-sheet $(DIR)/sheet.png

plates-import: ## Bring plates from DIR back in and approve them (no image calls)
	$(PY) -m app.scripts.import_plates --story $(STORY) --dir $(DIR) --approve

# --- measuring ---------------------------------------------------------------
# T11: the numbers the plan asks for, from a running server rather than an
# estimate. Free against a mock-mode instance; against a real key it makes real
# books, so it refuses to report a cost estimate without the flag.
URL ?= http://localhost:8000

time-book: ## Time one book end to end against $(URL) (mock mode is free)
	$(PY) scripts/time_book.py --url $(URL) --books 1

time-books: ## Make two books at once, to see whether one waits for the other
	$(PY) scripts/time_book.py --url $(URL) --books 2 --concurrent

stress: ## Load-test cartoonify against $(URL) without spending anything
	$(PY) scripts/stress_test.py --url $(URL)

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
