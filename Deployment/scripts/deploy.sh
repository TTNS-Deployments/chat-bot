#!/usr/bin/env bash
# One-command deployment: SKU validation, infrastructure (ARM) and application code.
#
#   Deployment/scripts/deploy.sh -g <resource-group> -e <dev|standard|prod> [options]
#
# Options
#   -g, --resource-group   resource group (created if missing)                         [required]
#   -e, --environment      SKU preset: Deployment/ARM/parameters/<env>.parameters.json [required]
#   -l, --location         region for the resource group (default: the preset's location)
#   -s, --subscription     subscription ID or name (default: current az account)
#   -n, --deployment-name  ARM deployment name (default: chatbot-<environment>; with --skip-infra
#                          the latest chatbot deployment in the group is used if that name is missing)
#       --set NAME=VALUE   override one SKU parameter (repeatable), e.g. --set searchSku=standard
#       --seed             upload the sample documents in Data/sample_docs after deployment
#       --skip-infra       only deploy code
#       --skip-code        only deploy infrastructure
#       --skip-region-check  skip the model/region/quota checks (static checks still run)
#
# Prerequisites: Azure CLI (az login) and python3. The signed-in identity needs Owner (or
# Contributor + User Access Administrator) on the resource group, because the template creates
# role assignments.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS="$ROOT/Deployment/scripts"
RG="" ENV="" LOCATION="" SUBSCRIPTION="" DEPLOYMENT="" SEED=0 SKIP_INFRA=0 SKIP_CODE=0 REGION_CHECK=1
OVERRIDES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) RG="$2"; shift 2 ;;
    -e|--environment) ENV="$2"; shift 2 ;;
    -l|--location) LOCATION="$2"; shift 2 ;;
    -s|--subscription) SUBSCRIPTION="$2"; shift 2 ;;
    -n|--deployment-name) DEPLOYMENT="$2"; shift 2 ;;
    --set) OVERRIDES+=("$2"); shift 2 ;;
    --seed) SEED=1; shift ;;
    --skip-infra) SKIP_INFRA=1; shift ;;
    --skip-code) SKIP_CODE=1; shift ;;
    --skip-region-check) REGION_CHECK=0; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown option $1 (see --help)" >&2; exit 1 ;;
  esac
done
[[ -n "$RG" && -n "$ENV" ]] || { echo "Usage: $0 -g <resource-group> -e <environment>  (see --help)" >&2; exit 1; }

PARAMS="$ROOT/Deployment/ARM/parameters/$ENV.parameters.json"
[[ -f "$PARAMS" ]] || { echo "Parameters file not found: $PARAMS" >&2; exit 1; }
command -v az >/dev/null || { echo "Azure CLI (az) not found. Install: https://learn.microsoft.com/cli/azure/install-azure-cli" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found." >&2; exit 1; }
if [[ -z "$LOCATION" ]]; then
  LOCATION="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["parameters"].get("location", {}).get("value", "centralindia"))' "$PARAMS")"
fi

[[ -n "$SUBSCRIPTION" ]] && az account set --subscription "$SUBSCRIPTION"
az account show >/dev/null 2>&1 || { echo "Run 'az login' first." >&2; exit 1; }
echo "Subscription: $(az account show --query name -o tsv)"

SET_ARGS=()
for o in "${OVERRIDES[@]+"${OVERRIDES[@]}"}"; do SET_ARGS+=(--set "$o"); done

DEPLOYMENT="${DEPLOYMENT:-chatbot-$ENV}"
if [[ $SKIP_INFRA -eq 1 ]] && ! az deployment group show -g "$RG" -n "$DEPLOYMENT" -o none 2>/dev/null; then
  # For example after a Deploy to Azure button deployment, which has another name.
  LATEST="$(az deployment group list -g "$RG" --query "sort_by([?properties.outputs.webAppName], &properties.timestamp)[-1].name" -o tsv 2>/dev/null || true)"
  [[ -n "$LATEST" ]] || { echo "No chatbot deployment found in $RG. Deploy the infrastructure first." >&2; exit 1; }
  echo "Using deployment $LATEST"
  DEPLOYMENT="$LATEST"
fi
if [[ $SKIP_INFRA -eq 0 ]]; then
  echo "==> Validating the SKU selection"
  CHECK=()
  [[ $REGION_CHECK -eq 1 ]] && CHECK=(--check-region)
  python3 "$SCRIPTS/validate_skus.py" -e "$ENV" -l "$LOCATION" "${SET_ARGS[@]+"${SET_ARGS[@]}"}" "${CHECK[@]+"${CHECK[@]}"}"

  # Identity running the deployment gets data access (Storage, Search, OpenAI, Key Vault) for ingestion.
  if [[ "$(az account show --query user.type -o tsv)" == "servicePrincipal" ]]; then
    DEPLOYER_ID="$(az ad sp show --id "$(az account show --query user.name -o tsv)" --query id -o tsv 2>/dev/null || true)"
    DEPLOYER_TYPE="ServicePrincipal"
  else
    DEPLOYER_ID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
    DEPLOYER_TYPE="User"
  fi
  [[ -n "$DEPLOYER_ID" ]] || echo "Note: could not resolve the deploying identity; skipping its data-access role assignments."

  echo "==> Resource group $RG ($LOCATION)"
  az group create -n "$RG" -l "$LOCATION" -o none

  # Keep the upload key stable across redeployments: reuse the one in Key Vault if there is one.
  SECRET_FILE="$(mktemp)"
  trap 'rm -f "$SECRET_FILE"' EXIT
  chmod 600 "$SECRET_FILE"
  echo '{"parameters":{}}' > "$SECRET_FILE"
  OLD_VAULT="$(az deployment group show -g "$RG" -n "$DEPLOYMENT" --query properties.outputs.keyVaultName.value -o tsv 2>/dev/null || true)"
  if [[ -n "$OLD_VAULT" ]]; then
    if KEY="$(az keyvault secret show --vault-name "$OLD_VAULT" -n upload-api-key --query value -o tsv 2>/dev/null)" && [[ -n "$KEY" ]]; then
      KEY="$KEY" python3 -c 'import json, os; print(json.dumps({"parameters": {"uploadApiKey": {"value": os.environ["KEY"]}}}))' > "$SECRET_FILE"
      echo "Reusing the existing upload key from $OLD_VAULT"
    fi
  fi

  EXTRA=()
  [[ ${#OVERRIDES[@]} -gt 0 ]] && EXTRA=(--parameters "${OVERRIDES[@]}")
  echo "==> Deploying infrastructure with $(basename "$PARAMS") (about 10-15 minutes)"
  az deployment group create -g "$RG" -n "$DEPLOYMENT" \
    --template-file "$ROOT/Deployment/ARM/azuredeploy.json" \
    --parameters @"$PARAMS" \
    --parameters @"$SECRET_FILE" \
    --parameters deployerPrincipalId="${DEPLOYER_ID:-}" deployerPrincipalType="$DEPLOYER_TYPE" \
    "${EXTRA[@]+"${EXTRA[@]}"}" \
    -o none
fi

out() { az deployment group show -g "$RG" -n "$DEPLOYMENT" --query "properties.outputs.$1.value" -o tsv; }
WEB_APP="$(out webAppName)"
WEB_URL="$(out webAppUrl)"
VAULT="$(out keyVaultName)"

if [[ $SKIP_CODE -eq 0 ]]; then
  echo "==> Packaging"
  python3 "$SCRIPTS/package.py"
  echo "==> Deploying web app $WEB_APP (remote build, a few minutes)"
  az webapp deploy -g "$RG" -n "$WEB_APP" --src-path "$ROOT/build/webapp.zip" --type zip -o none

  echo "==> Waiting for $WEB_URL/healthz"
  state=""
  for _ in $(seq 1 40); do
    state="$(curl -fsS --max-time 10 "$WEB_URL/healthz" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("searchSetup",""))' 2>/dev/null || true)"
    [[ "$state" == "ready" || "$state" == "failed" ]] && break
    sleep 15
  done
  case "$state" in
    ready) echo "Web app is healthy and the search index is in place." ;;
    failed) echo "Warning: the web app is up but could not create the search index. See $WEB_URL/healthz and Application Insights." >&2 ;;
    *) echo "Warning: the web app did not report ready yet. Check $WEB_URL/healthz in a few minutes." >&2 ;;
  esac
fi

if [[ $SEED -eq 1 ]]; then
  "$SCRIPTS/ingest.sh" -g "$RG" -n "$DEPLOYMENT" "$ROOT/Data/sample_docs"
fi

echo
echo "Done. Chat page: $WEB_URL"
echo "Upload key:      az keyvault secret show --vault-name $VAULT -n upload-api-key --query value -o tsv"
