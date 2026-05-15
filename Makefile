# Local dev workflow for face-capture.
#
# Typical first-time setup:
#   cp .env.example .env
#   make install               # python + node deps
#   make dev                   # API + SPA in one terminal
#
# No database, no auth, no migrations. State is in-memory; restart kills it.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# --- Config ----------------------------------------------------------------
VENV        := .venv
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
UVICORN     := $(VENV)/bin/uvicorn
API_PORT    := 8000
WEB_PORT    := 5173

# --- Help ------------------------------------------------------------------
.PHONY: help
help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- Setup -----------------------------------------------------------------
.PHONY: venv
venv: ## Create the Python venv if missing.
	@test -d $(VENV) || python3 -m venv $(VENV)

.PHONY: install
install: venv ## Install Python and Node deps.
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	cd web && npm ci

# --- Run -------------------------------------------------------------------
.PHONY: api
api: ## Run the FastAPI service with autoreload.
	$(UVICORN) app.main:app --host 0.0.0.0 --port $(API_PORT) --reload

.PHONY: web
web: ## Run the Vite SPA dev server.
	cd web && npm run dev

# `make dev` runs api + web concurrently. Ctrl-C kills both.
.PHONY: dev
dev: ## Run API + SPA together. Ctrl-C stops both.
	@set -m; \
	  trap 'kill 0' INT TERM EXIT; \
	  $(UVICORN) app.main:app --host 0.0.0.0 --port $(API_PORT) --reload & \
	  cd web && npm run dev; \
	  wait

# --- Frontend build / checks ----------------------------------------------
.PHONY: web-build
web-build: ## Production build of the SPA into web/dist.
	cd web && npm run build

.PHONY: typecheck
typecheck: ## TypeScript typecheck on the frontend.
	cd web && npm run typecheck

# --- Docker (parity with Railway) -----------------------------------------
.PHONY: docker-build
docker-build: ## Build the production image locally.
	docker build -t face-capture:dev .

.PHONY: docker-run
docker-run: ## Run the production image with .env mounted.
	docker run --rm -p 8000:8000 --env-file .env face-capture:dev

# --- Cleanup ---------------------------------------------------------------
.PHONY: clean
clean: ## Remove local storage + job working dirs.
	rm -rf _storage jobs web/dist
