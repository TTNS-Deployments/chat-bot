#!/usr/bin/env bash
# Compile Deployment/bicep/main.bicep into Deployment/ARM/azuredeploy.json (the template the
# Deploy to Azure button and the deploy scripts use).
#
#   Deployment/scripts/build_arm.sh           rebuild azuredeploy.json
#   Deployment/scripts/build_arm.sh --check   fail if azuredeploy.json is out of date (CI, pre-commit)
#
# The comparison ignores the Bicep version stamp, so different Bicep versions agree.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BICEP="$ROOT/Deployment/bicep/main.bicep"
TEMPLATE="$ROOT/Deployment/ARM/azuredeploy.json"

command -v az >/dev/null || { echo "Azure CLI (az) not found. Install it, then run: az bicep install" >&2; exit 1; }

if [[ "${1:-}" == "--check" ]]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  az bicep build --file "$BICEP" --outfile "$tmp/azuredeploy.json"
  python3 - "$tmp/azuredeploy.json" "$TEMPLATE" <<'PY'
import json, sys
def strip(n):
    if isinstance(n, dict):
        return {k: strip(v) for k, v in n.items() if k != "_generator"}
    if isinstance(n, list):
        return [strip(x) for x in n]
    return n
built, committed = (strip(json.load(open(p))) for p in sys.argv[1:3])
if built != committed:
    sys.exit("Deployment/ARM/azuredeploy.json is out of date. Run Deployment/scripts/build_arm.sh and commit the result.")
print("azuredeploy.json matches the Bicep source.")
PY
else
  az bicep build --file "$BICEP" --outfile "$TEMPLATE"
  echo "Built ${TEMPLATE#"$ROOT"/}"
fi
