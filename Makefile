# Demo tooling for the Angi lead integration. "make help" lists the targets.
#
#   make install && cp .env.example .env     (then edit .env)
#   make run                                 terminal 1: the service
#   make tunnel                              terminal 2: a public URL for Angi
#   make send EMAIL=you@example.com          terminal 3: a lead, as Angi would send it (use your own address)

.DEFAULT_GOAL := help
.PHONY: help install run dev test lint e2e tunnel send canary simulate reset-db

# Variables for "make send", "make canary" and "make simulate".
SAMPLE ?= angi_ads_spec_example
EMAIL ?=
ARGS ?=

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  %-10s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the dependencies into .venv
	uv sync

run: ## Start the service on http://127.0.0.1:8000
	uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

dev: ## Start the service and restart it whenever the code changes
	uv run uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload

test: ## Run the tests
	uv run pytest

lint: ## Check the code with ruff
	uv run ruff check .

e2e: ## Start a real server on a scratch database and walk the whole flow over HTTP (sends no email)
	uv run python scripts/e2e_check.py

tunnel: ## Expose port 8000 with ngrok (NGROK_URL=https://your-name.ngrok-free.dev keeps the URL stable)
	@if [ -n "$$NGROK_URL" ]; then \
		ngrok http 8000 --url "$$NGROK_URL"; \
	else \
		ngrok http 8000; \
	fi

send: ## Send a sample lead to your own address: make send EMAIL=you@example.com [SAMPLE=angi_leads_example] [ARGS="--same-id"]
	uv run python scripts/send_lead.py --sample $(SAMPLE) $(if $(EMAIL),--email $(EMAIL)) $(ARGS)

canary: ## Run the end-to-end probe once, emailing your own address: make canary EMAIL=you@example.com
	@if [ -z "$(EMAIL)" ]; then echo "usage: make canary EMAIL=<an address you own>"; exit 2; fi
	uv run python scripts/canary.py --email $(EMAIL) $(ARGS)

simulate: ## Fill the database with two weeks of simulated history (sends no email; stop the service first) [ARGS="--leads 300"]
	uv run python scripts/simulate_history.py $(ARGS)

reset-db: ## Delete the local database and the captured emails (stop the service first)
	@found=$$(ls -d data/leads.db* data/outbox 2>/dev/null); \
	if [ -z "$$found" ]; then \
		echo "Nothing to remove (no data/leads.db*, no data/outbox)."; \
	else \
		echo "Removing:"; \
		echo "$$found" | sed 's/^/  /'; \
		rm -rf data/leads.db* data/outbox; \
	fi
