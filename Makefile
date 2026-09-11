# Usage: make <target> [ARGS="--dry-run"]
# Requires GNU make + a POSIX shell (Linux/macOS, WSL, or Git Bash on Windows).

ENGINE_REPO ?= https://github.com/harry0703/MoneyPrinterTurbo.git
ENGINE_REF  ?= $(shell cat deploy/engine.ref)
ENGINE_DIR  := vendor/video-engine
PY       := uv run python
ARGS     ?=
ENV_TEMPLATE := $(firstword $(wildcard .env.example) config/env.example)

.PHONY: help vendor setup up down logs generate review upload auth daily calendar test caddy-hash

help:
	@echo "vendor setup up down logs generate review upload auth daily calendar test caddy-hash"
	@echo "Pass flags with ARGS, e.g. make daily ARGS=--dry-run"

## Vendor the video engine: pinned commit (deploy/engine.ref) + deploy/engine.patch, no .git, songs removed
vendor:
	ENGINE_REPO=$(ENGINE_REPO) bash scripts/ci/vendor-engine.sh $(ENGINE_REF)

## Python env, local dirs, config + .env from templates (never overwrites)
setup:
	uv sync
	mkdir -p output/pending output/approved output/rejected output/uploaded .credentials
	@[ -f config/config.toml ] || cp config/config.template.toml config/config.toml
	@[ -f .env ] || cp $(ENV_TEMPLATE) .env
	@echo "Now edit .env (see README checklist)."

up:
	@[ -d $(ENGINE_DIR) ] || { echo "run 'make vendor' first"; exit 1; }
	@[ -f config/config.toml ] || { echo "run 'make setup' first"; exit 1; }
	@[ -f .env ] || { echo "run 'make setup' and fill .env first"; exit 1; }
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

generate:
	$(PY) -m orchestrator.generate $(ARGS)

review:
	$(PY) -m orchestrator.review $(ARGS)

upload:
	$(PY) -m orchestrator.upload $(ARGS)

## One-time interactive Google OAuth consent; writes .credentials/youtube.token.json
auth:
	$(PY) -m orchestrator.upload --auth-only $(ARGS)

daily:
	$(PY) -m orchestrator.scheduler $(ARGS)

## Check topics/calendar/*.json against the editorial rules
calendar:
	$(PY) -m orchestrator.content_calendar validate $(ARGS)

test:
	uv run pytest

## Print a bcrypt hash for BASIC_AUTH_HASH (prompts for the password)
caddy-hash:
	docker run --rm -it caddy:2-alpine caddy hash-password
