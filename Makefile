SHELL := /usr/bin/env bash
PY ?= python3
PROMETHEUS_IMAGE ?= prom/prometheus:v3.15.1

.DEFAULT_GOAL := help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

## --- stack -----------------------------------------------------------------
up: ## Build and start everything (Grafana: http://localhost:3000)
	docker compose up -d --build --wait --wait-timeout 300
	@echo "Grafana http://localhost:3000  Prometheus http://localhost:9090  Alertmanager http://localhost:9093"

down: ## Stop and delete volumes
	docker compose down -v

logs: ## Follow logs of all services
	docker compose logs -f --tail=50

reload: ## Reload Prometheus rules and config without restarting
	curl -fsS -X POST http://localhost:9090/-/reload && echo reloaded

e2e: ## Run the end-to-end test against a running stack
	tests/e2e/run.sh

## --- chaos -----------------------------------------------------------------
chaos-errors: ## 30% 5xx in payments (burns checkout budget fast)
	scripts/chaos.sh errors payments 0.3

chaos-latency: ## +800ms on half of payments requests (burns latency budget)
	scripts/chaos.sh latency payments 800 0.5

chaos-clear: ## Remove every injected fault
	scripts/chaos.sh clear all

## --- dev -------------------------------------------------------------------
install: ## Install dev dependencies into the current Python env
	$(PY) -m pip install -r services/requirements-dev.txt -e "tools/slogen[dev]" yamllint shellcheck-py

slo: ## Regenerate Prometheus rules from slo/specs
	cd tools/slogen && $(PY) -m slogen generate --specs ../../slo/specs --out ../../observability/prometheus/rules/slo

dashboards: ## Regenerate Grafana dashboards
	$(PY) tools/dashboards/build.py

lint: ## ruff, yamllint, shellcheck
	ruff check . && ruff format --check .
	yamllint .
	shellcheck tests/e2e/*.sh scripts/*.sh

test: ## Unit tests + generated-code drift + PromQL syntax
	cd services && $(PY) -m pytest -q
	cd tools/slogen && $(PY) -m pytest -q
	cd tools/slogen && $(PY) -m slogen validate --specs ../../slo/specs
	cd tools/slogen && $(PY) -m slogen generate --specs ../../slo/specs --out ../../observability/prometheus/rules/slo --check
	$(PY) tools/dashboards/build.py --check

promtool: ## promtool check config + alerting unit tests (needs Docker)
	docker run --rm -v "$$PWD/observability/prometheus:/etc/prometheus:ro" --entrypoint promtool $(PROMETHEUS_IMAGE) check config /etc/prometheus/prometheus.yml
	docker run --rm -v "$$PWD/observability/prometheus:/etc/prometheus:ro" -w /etc/prometheus/tests --entrypoint sh $(PROMETHEUS_IMAGE) -c 'promtool test rules *.test.yml'

check: lint test promtool ## Everything CI runs except e2e

.PHONY: help up down logs reload e2e chaos-errors chaos-latency chaos-clear install slo dashboards lint test promtool check
