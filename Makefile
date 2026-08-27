# The Lenny Growth Assistant — task runner
#
# Every target's raw `docker compose` equivalent is documented in README.md,
# so `make` is a convenience, never a requirement (it is not installed by
# default on Windows).

SHELL := /bin/bash
COMPOSE := docker compose
LIMIT ?=

.DEFAULT_GOAL := help
.PHONY: help env up down restart logs ps build ingest reingest search test test-local evaluate calibrate prune-ads psql clean nuke bootstrap

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

up: env ## Start db + backend
	$(COMPOSE) up -d --build
	@echo "Backend  → http://localhost:8000/docs"
	@echo "Readiness→ http://localhost:8000/readyz"

down: ## Stop everything (volumes preserved)
	$(COMPOSE) down

restart: ## Restart the backend only
	$(COMPOSE) restart backend

build: ## Rebuild images
	$(COMPOSE) build

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Tail backend logs
	$(COMPOSE) logs -f backend

ingest: ## Ingest transcripts.  make ingest LIMIT=20  for a fast subset
	$(COMPOSE) exec backend python -m app.rag.ingest $(if $(LIMIT),--limit $(LIMIT),)

reingest: ## Force a full re-ingest, ignoring content hashes
	$(COMPOSE) exec backend python -m app.rag.ingest --force $(if $(LIMIT),--limit $(LIMIT),)

search: ## Smoke-test retrieval.  make search Q="how do I find product market fit"
	@curl -sS -X POST http://localhost:8000/api/search \
	  -H 'Content-Type: application/json' \
	  -d '{"q": "$(or $(Q),how do I find product market fit)", "k": 5}' | python -m json.tool

test: ## Run the backend test suite inside the container
	$(COMPOSE) exec backend python -m pytest -q

test-local: ## Run the tests on your host (no Docker, no Ollama, no API keys needed)
	cd backend && python -m pytest -q

evaluate: ## Measure the PRD success metric end-to-end (needs a live model)
	$(COMPOSE) exec backend python -m scripts.evaluate

calibrate: ## Regenerate the retrieval calibration evidence
	$(COMPOSE) exec backend python -m scripts.calibrate_retrieval

prune-ads: ## Remove sponsor reads from an existing index (add DRY=1 to preview)
	$(COMPOSE) exec backend python -m scripts.prune_ads $(if $(DRY),--dry-run,)

psql: ## Open a psql shell against the dev database
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-lenny} -d $${POSTGRES_DB:-lenny}

bootstrap: up ## First run: start, ingest 20 episodes, prove retrieval works
	@echo "Waiting for the backend to become ready..."
	@for i in $$(seq 1 30); do \
	  curl -sf http://localhost:8000/healthz >/dev/null && break || sleep 2; \
	done
	$(MAKE) ingest LIMIT=20
	@$(MAKE) --no-print-directory search
	@echo ""
	@echo "Bootstrap complete. Open http://localhost:8000/readyz"

clean: ## Stop and remove containers + volumes (corpus and DB are lost)
	$(COMPOSE) down -v

nuke: clean ## clean + remove built images
	$(COMPOSE) down --rmi local -v
