# Common tasks. Run `make help` for the list.
PY ?= .venv/bin/python
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: help setup run test lint build-arm check-arm validate-skus package hooks

help:            ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

setup:           ## Create .venv and install everything
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt

run:             ## Start the chatbot on http://localhost:8000 (local mode unless .env says otherwise)
	cd Code/backend && ../../$(PY) -m uvicorn chatbot.main:app --reload --port 8000

test:            ## Run all tests
	$(PY) -m pytest Code/backend -q

lint:            ## shellcheck, PSScriptAnalyzer and the Bicep linter
	shellcheck Deployment/scripts/*.sh .githooks/pre-commit
	pwsh -NoProfile -Command "if (Invoke-ScriptAnalyzer -Path Deployment/scripts -Recurse) { exit 1 }"
	az bicep lint --file Deployment/bicep/main.bicep

build-arm:       ## Compile Bicep into Deployment/ARM/azuredeploy.json
	Deployment/scripts/build_arm.sh

check-arm:       ## Fail if azuredeploy.json is out of date
	Deployment/scripts/build_arm.sh --check

validate-skus:   ## Validate every SKU preset (static checks)
	@for f in Deployment/ARM/parameters/*.parameters.json; do \
	  python3 Deployment/scripts/validate_skus.py -f $$f -q && echo "$$f: valid" || exit 1; done

package:         ## Build build/webapp.zip
	python3 Deployment/scripts/package.py

hooks:           ## Use the repository's git hooks (rebuilds azuredeploy.json check before commits)
	git config core.hooksPath .githooks
