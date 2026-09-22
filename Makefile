# Developer commands.
#
# Why every target clears PYTHONPATH: a system ROS installation on some
# machines puts its own Python 3.14 site-packages on PYTHONPATH. pytest
# auto-loads plugins it finds there, and one of them (`launch_testing`) fails
# to import under this project's Python 3.12 venv, which breaks the test run
# before any of our code executes. Clearing the variable isolates us from
# whatever else is installed on the host.

PY := .venv/bin/python
CLEAN_ENV := env PYTHONPATH=

.PHONY: help install test test-unit test-integration lint fmt db-up db-down db-reset db-seed \
        api web web-install web-build bench clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install the package with dev dependencies
	uv venv --python 3.12
	uv pip install --python .venv/bin/python -e ".[dev]"

test:  ## Run the whole suite (unit + integration)
	$(CLEAN_ENV) $(PY) -m pytest

test-unit:  ## Run only tests that need no database
	$(CLEAN_ENV) $(PY) -m pytest -m "not integration"

test-integration:  ## Run only tests that need a live Postgres
	$(CLEAN_ENV) $(PY) -m pytest -m integration

lint:  ## Check style and common errors
	$(CLEAN_ENV) $(PY) -m ruff check src tests
	$(CLEAN_ENV) $(PY) -m ruff format --check src tests

fmt:  ## Reformat the code
	$(CLEAN_ENV) $(PY) -m ruff format src tests
	$(CLEAN_ENV) $(PY) -m ruff check --fix src tests

db-up:  ## Start the throwaway Postgres used by integration tests
	docker run -d --name sqlagent-test-pg \
		-e POSTGRES_PASSWORD=testpass \
		-e POSTGRES_USER=testuser \
		-e POSTGRES_DB=testdb \
		-p 5434:5432 postgres:16-alpine

db-down:  ## Stop and remove the test database
	-docker rm -f sqlagent-test-pg

db-reset: db-down db-up  ## Recreate the test database from scratch

db-seed:  ## Load the example schema and sample data into the test database
	$(CLEAN_ENV) $(PY) -c "from sqlalchemy import create_engine; from pathlib import Path; \
	e = create_engine('postgresql+psycopg://testuser:testpass@localhost:5434/testdb'); \
	c = e.begin().__enter__(); \
	c.exec_driver_sql(Path('tests/fixtures/schema.sql').read_text()); \
	c.exec_driver_sql(Path('tests/fixtures/seed.sql').read_text()); \
	c.commit()"

api:  ## Run the API server (http://localhost:8000)
	$(CLEAN_ENV) $(PY) -m uvicorn sqlagent.api.app:app --reload --port 8000

web-install:  ## Install frontend dependencies
	cd frontend && npm install

web:  ## Run the web interface (http://localhost:5173)
	cd frontend && npm run dev

web-typecheck:  ## Type-check the frontend
	# `tsc -b`, not `tsc --noEmit`. The root tsconfig has "files": [] and only
	# project references, so a bare `tsc --noEmit` checks the root project —
	# zero files — and exits 0 having read nothing in src/. That silence was
	# mistaken for a passing check while a `ReferenceError` shipped to the
	# browser and blanked the page.
	cd frontend && npm run typecheck

web-build:  ## Build the frontend for production
	cd frontend && npm run build

verify:  ## Everything: lint, types, tests
	$(MAKE) lint
	$(MAKE) web-typecheck
	$(MAKE) test

bench:  ## Run the BIRD benchmark (override: make bench ARGS="--limit 100")
	$(CLEAN_ENV) $(PY) benchmarks/bird.py $(ARGS)

bench-bg:  ## Run the benchmark in the background, streaming progress to a log
	# Never pipe this through `tail`. The harness prints a line per question
	# with flush=True, but `| tail -N` buffers the whole stream until the
	# process exits — so a long run looks frozen for an hour and there is no
	# way to tell a working run from a hung one. `tee` streams and keeps a copy.
	@mkdir -p benchmarks/results
	$(CLEAN_ENV) nohup $(PY) benchmarks/bird.py $(ARGS) \
		2>&1 | tee benchmarks/results/run-$$(date +%%Y%%m%%d-%%H%%M%%S).log &
	@echo "started; follow with: tail -f benchmarks/results/run-*.log"

clean:  ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
