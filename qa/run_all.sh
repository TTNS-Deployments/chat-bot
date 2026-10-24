#!/usr/bin/env bash
# Run the whole QA suite (backend, LLM-failure, live-server, UI/front end) and rebuild the reports.
# Local/mock mode only: no Azure credentials are used and nothing costs money.
set -u
cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
rm -rf qa/results && mkdir -p qa/results qa/evidence
.venv/bin/python -m pytest qa/tests -p no:cacheprovider -q -rfEs --junitxml=qa/evidence/junit.xml \
  --basetemp="${QA_TMP:-${TMPDIR:-/tmp}/qa-chatbot-pytest}" 2>&1 | tee qa/evidence/pytest_run.txt
code=${PIPESTATUS[0]}
echo "pytest exit code: $code" | tee -a qa/evidence/pytest_run.txt
.venv/bin/python qa/build_report.py | tee qa/evidence/summary.txt
