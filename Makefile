# Usage: make <target> [ARGS="--dry-run"]
# Requires GNU make + a POSIX shell (Linux/macOS, WSL, or Git Bash on Windows).

ENGINE_REPO ?= https://github.com/harry0703/MoneyPrinterTurbo.git
ENGINE_REF  ?=
ENGINE_DIR  := vendor/video-engine
PY       := uv run python
ARGS     ?=
ENV_TEMPLATE := $(firstword $(wildcard .env.example) config/env.example)

.PHONY: help vendor setup up down logs generate review upload auth daily test caddy-hash

help:
	@echo "vendor setup up down logs generate review upload auth daily test caddy-hash"
	@echo "Pass flags with ARGS, e.g. make daily ARGS=--dry-run"

## Re-clone the video engine (no .git, bundled songs removed)
vendor:
	rm -rf $(ENGINE_DIR)
	git clone --depth 1 $(if $(ENGINE_REF),--branch $(ENGINE_REF),) $(ENGINE_REPO) $(ENGINE_DIR)
	rm -rf $(ENGINE_DIR)/.git
	find $(ENGINE_DIR)/resource/songs -type f -delete
	cp deploy/songs-README.md $(ENGINE_DIR)/resource/songs/README.md
	@echo "Vendored video engine into $(ENGINE_DIR)"

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

test:
	uv run pytest

## Print a bcrypt hash for BASIC_AUTH_HASH (prompts for the password)
caddy-hash:
	docker run --rm -it caddy:2-alpine caddy hash-password
