.PHONY: help setup bootstrap run catalog rank publish verify demo-drift serve serve-fixtures tunnel-fixtures fixtures clean

SHELL := /bin/bash
PY := python3
export PYTHONPATH := src

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## install deps and OTel instrumentation
	$(PY) -m pip install -r requirements.txt
	opentelemetry-bootstrap -a install
	@echo "-> copy .env.example to .env and fill it in"
	@echo "-> start SigNoz separately (see README): self-hosted docker compose, or Cloud"

bootstrap: ## create Port blueprints, automation, and self-service action (idempotent)
	$(PY) scripts/bootstrap_port.py

serve: ## run the factory API, auto-instrumented so endpoints are traced
	# telemetry.init() wires logs/metrics/traces in-process (the same path the CLI uses),
	# so leave OTel's auto-logging OFF here -- enabling it would double-export every record.
	OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=false \
	opentelemetry-instrument $(PY) -m uvicorn factory.app:app --host 0.0.0.0 --port 8000

run: ## scrape 7 days of guide windows for every provider (use `--days 1` via CLI for cheap dev runs)
	$(PY) -m factory run --days 7

catalog: ## refresh the Philo title -> record-URL map
	$(PY) -m factory catalog

rank: ## compute the top-20 upcoming titles and write them to Port
	$(PY) -m factory rank

publish: ## publish the top-20 to Port's Context Lake (the webapp reads it there)
	$(PY) -m factory publish

verify: ## every scraper against its fixture; non-zero exit on mismatch (CI gate)
	$(PY) -m factory verify

demo-drift: ## point a scraper at the tunneled mutated fixture and let the loop repair it
	./scripts/demo_drift.sh

serve-fixtures: ## serve fixtures/ on :9000 (tunnel it: cloudflared tunnel --url http://localhost:9000)
	$(PY) -m http.server 9000 --directory fixtures

tunnel-fixtures: ## expose the fixture server publicly; set FIXTURE_BASE_URL to the printed URL
	cloudflared tunnel --url http://localhost:9000

fixtures: ## capture fresh HTML fixtures from the live targets
	$(PY) -m factory fixtures

clean: ## drop local snapshot state
	rm -f out/factory.db
