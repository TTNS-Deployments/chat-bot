#!/usr/bin/env bash
# One-command deployment: SKU validation, infrastructure (ARM) and application code.
#
#   Deployment/scripts/deploy.sh -g <resource-group> -e <dev|standard|prod|demo> [options]
#   Deployment/scripts/deploy.sh -e demo --param demoPassword='...'      # demo, from Azure Cloud Shell
#
# Options
#   -g, --resource-group   resource group (reused if it exists, created otherwise)  [required unless the
#                          environment file sets RESOURCE_GROUP]
#   -e, --environment      SKU preset: Deployment/ARM/parameters/<env>.parameters.json [required]
#                          Deployment/environments/<env>.env, if present, supplies defaults for the options.
#   -l, --location         region for a new resource group (default: the preset's location)
#   -s, --subscription     subscription ID or name (default: current az account)
#   -n, --deployment-name  ARM deployment name (default: chatbot-<environment>; with --skip-infra
#                          the latest chatbot deployment in the group is used if that name is missing)
#       --set NAME=VALUE   override one SKU parameter (repeatable, validated), e.g. --set searchSku=basic
#       --param NAME=VALUE pass a template parameter without printing or validating it (repeatable); for
#                          secrets: demoPassword, uploadApiKey. Stored values are kept when omitted later.
#       --seed             upload the sample documents in Data/sample_docs
#       --code-via-arm     deploy the code as a run-from-package zip using management (ARM) calls only.
#                          Needed in Azure Cloud Shell, where the App Service deployment endpoints fail.
#       --skip-infra       only deploy code
#       --skip-code        only deploy infrastructure
#       --skip-region-check  skip the model/region/quota checks (static checks still run)
#
# Prerequisites: Azure CLI (az login), python3 (and pip for --code-via-arm). The signed-in identity
# needs Owner (or Contributor + User Access Administrator) on the resource group, because the
# template creates role assignments.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPTS="$ROOT/Deployment/scripts"
CLI_RG="" ENV="" CLI_LOCATION="" CLI_SUBSCRIPTION="" DEPLOYMENT="" CLI_SEED="" CLI_CODE_VIA_ARM=""
SKIP_INFRA=0 SKIP_CODE=0 REGION_CHECK=1
OVERRIDES=() PARAMS_IN=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) CLI_RG="$2"; shift 2 ;;
    -e|--environment) ENV="$2"; shift 2 ;;
    -l|--location) CLI_LOCATION="$2"; shift 2 ;;
    -s|--subscription) CLI_SUBSCRIPTION="$2"; shift 2 ;;
    -n|--deployment-name) DEPLOYMENT="$2"; shift 2 ;;
    --set) OVERRIDES+=("$2"); shift 2 ;;
    --param) PARAMS_IN+=("$2"); shift 2 ;;
    --seed) CLI_SEED=1; shift ;;
    --code-via-arm) CLI_CODE_VIA_ARM=1; shift ;;
    --skip-infra) SKIP_INFRA=1; shift ;;
    --skip-code) SKIP_CODE=1; shift ;;
    --skip-region-check) REGION_CHECK=0; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Unknown option $1 (see --help)" >&2; exit 1 ;;
  esac
done
[[ -n "$ENV" ]] || { echo "Usage: $0 -g <resource-group> -e <environment>  (see --help)" >&2; exit 1; }

PARAMS="$ROOT/Deployment/ARM/parameters/$ENV.parameters.json"
[[ -f "$PARAMS" ]] || { echo "Parameters file not found: $PARAMS" >&2; exit 1; }

# Environment file defaults; command-line options win.
AZURE_SUBSCRIPTION="" RESOURCE_GROUP="" LOCATION="" SEED=0 CODE_VIA_ARM=0
ENV_FILE="$ROOT/Deployment/environments/$ENV.env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  echo "Using settings from ${ENV_FILE#"$ROOT"/}"
fi
RG="${CLI_RG:-$RESOURCE_GROUP}"
SUBSCRIPTION="${CLI_SUBSCRIPTION:-$AZURE_SUBSCRIPTION}"
LOCATION="${CLI_LOCATION:-$LOCATION}"
SEED="${CLI_SEED:-$SEED}"
CODE_VIA_ARM="${CLI_CODE_VIA_ARM:-$CODE_VIA_ARM}"
[[ -n "$RG" ]] || { echo "No resource group: pass -g <resource-group>." >&2; exit 1; }

command -v az >/dev/null || { echo "Azure CLI (az) not found. Install: https://learn.microsoft.com/cli/azure/install-azure-cli" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found." >&2; exit 1; }
preset() {  # preset <parameter> [default]: value from the SKU file, after --set overrides
  python3 - "$PARAMS" "$1" "${2:-}" "${OVERRIDES[@]+"${OVERRIDES[@]}"}" <<'PY'
import json, sys
path, name, default, *overrides = sys.argv[1:]
value = json.load(open(path))["parameters"].get(name, {}).get("value", default)
for item in overrides:
    key, _, raw = item.partition("=")
    if key == name:
        value = raw
print(str(value).lower() if isinstance(value, bool) else value)
PY
}
[[ -n "$LOCATION" ]] || LOCATION="$(preset location centralindia)"

[[ -n "$SUBSCRIPTION" ]] && az account set --subscription "$SUBSCRIPTION"
az account show >/dev/null 2>&1 || { echo "Run 'az login' first." >&2; exit 1; }
echo "Subscription: $(az account show --query name -o tsv)"

SET_ARGS=()
for o in "${OVERRIDES[@]+"${OVERRIDES[@]}"}"; do SET_ARGS+=(--set "$o"); done

DEPLOYMENT="${DEPLOYMENT:-chatbot-$ENV}"
EXISTING=0
az deployment group show -g "$RG" -n "$DEPLOYMENT" -o none 2>/dev/null && EXISTING=1
if [[ $SKIP_INFRA -eq 1 && $EXISTING -eq 0 ]]; then
  # For example after a Deploy to Azure button deployment, which has another name.
  LATEST="$(az deployment group list -g "$RG" --query "sort_by([?properties.outputs.webAppName], &properties.timestamp)[-1].name" -o tsv 2>/dev/null || true)"
  [[ -n "$LATEST" ]] || { echo "No chatbot deployment found in $RG. Deploy the infrastructure first." >&2; exit 1; }
  echo "Using deployment $LATEST"
  DEPLOYMENT="$LATEST"
fi

has_param() { local p; for p in "${PARAMS_IN[@]+"${PARAMS_IN[@]}"}"; do [[ "${p%%=*}" == "$1" ]] && return 0; done; return 1; }
NEW_UPLOAD_KEY=""

if [[ $SKIP_INFRA -eq 0 ]]; then
  echo "==> Validating the SKU selection"
  CHECK=()
  [[ $REGION_CHECK -eq 1 ]] && CHECK=(--check-region)
  python3 "$SCRIPTS/validate_skus.py" -e "$ENV" -l "$LOCATION" "${SET_ARGS[@]+"${SET_ARGS[@]}"}" "${CHECK[@]+"${CHECK[@]}"}"

  if [[ "$(preset authMode none)" == "password" && $EXISTING -eq 0 ]] && ! has_param demoPassword; then
    echo "This preset uses the demo password sign-in. On the first deployment, pass it: --param demoPassword='...'" >&2
    exit 1
  fi

  PLAN="$(preset existingAppServicePlanName)"
  if [[ -n "$PLAN" ]]; then
    # A Linux plan has kind "linux" and/or reserved=true; depending on the API version one of them can be empty.
    # One value per call: `-o tsv` prints a list of values one per line, which is easy to misread.
    PLAN_LOCATION="$(az appservice plan show -g "$RG" -n "$PLAN" --query location -o tsv 2>/dev/null || true)"
    [[ -n "$PLAN_LOCATION" ]] || { echo "App Service plan $PLAN not found in $RG." >&2; exit 1; }
    PLAN_KIND="$(az appservice plan show -g "$RG" -n "$PLAN" --query kind -o tsv 2>/dev/null || true)"
    PLAN_RESERVED="$(az appservice plan show -g "$RG" -n "$PLAN" --query reserved -o tsv 2>/dev/null || true)"
    if [[ "${PLAN_KIND,,}" != *linux* && "${PLAN_RESERVED,,}" != "true" ]]; then
      echo "App Service plan $PLAN is not a Linux plan (kind: ${PLAN_KIND:-none}, reserved: ${PLAN_RESERVED:-none})." >&2
      exit 1
    fi
    PRESET_LOCATION="$(preset location)"
    if [[ -n "$PRESET_LOCATION" && "$(echo "$PLAN_LOCATION" | tr -d ' ' | tr '[:upper:]' '[:lower:]')" != "$PRESET_LOCATION" ]]; then
      echo "App Service plan $PLAN is in $PLAN_LOCATION, but the preset's location is $PRESET_LOCATION. They must match." >&2
      exit 1
    fi
    echo "Web app will share the existing App Service plan $PLAN (the plan is not changed)."
  fi

  # Identity running the deployment gets data access for ingestion. The object ID comes from the
  # management token, because Microsoft Graph is not reachable everywhere (for example Cloud Shell).
  DEPLOYER_TYPE="User"
  [[ "$(az account show --query user.type -o tsv)" == "servicePrincipal" ]] && DEPLOYER_TYPE="ServicePrincipal"
  DEPLOYER_ID="$(az account get-access-token --query accessToken -o tsv 2>/dev/null | python3 -c '
import base64, json, sys
part = sys.stdin.read().strip().split(".")[1]
part += "=" * (-len(part) % 4)
print(json.loads(base64.urlsafe_b64decode(part)).get("oid", ""))' 2>/dev/null || true)"
  [[ -n "$DEPLOYER_ID" ]] || echo "Note: could not resolve the deploying identity; skipping its data-access role assignments."

  if [[ "$(az group exists -n "$RG")" == "true" ]]; then
    echo "==> Using existing resource group $RG"
  else
    echo "==> Creating resource group $RG ($LOCATION)"
    az group create -n "$RG" -l "$LOCATION" -o none
  fi

  # Secrets and generated values go in a private file, not on the command line.
  SECRET_FILE="$(mktemp)"
  trap 'rm -f "$SECRET_FILE"' EXIT
  chmod 600 "$SECRET_FILE"
  if [[ $EXISTING -eq 0 ]] && ! has_param uploadApiKey; then
    NEW_UPLOAD_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    PARAMS_IN+=("uploadApiKey=$NEW_UPLOAD_KEY")
  fi
  # Keep the running code: an infrastructure deployment rewrites all app settings.
  if [[ $EXISTING -eq 1 ]]; then
    OLD_WEB="$(az deployment group show -g "$RG" -n "$DEPLOYMENT" --query properties.outputs.webAppName.value -o tsv 2>/dev/null || true)"
    if [[ -n "$OLD_WEB" ]] && ! has_param appPackageUrl; then
      OLD_PACKAGE="$(az webapp config appsettings list -g "$RG" -n "$OLD_WEB" --query "[?name=='WEBSITE_RUN_FROM_PACKAGE'].value | [0]" -o tsv 2>/dev/null || true)"
      [[ -n "$OLD_PACKAGE" && "$OLD_PACKAGE" != "None" ]] && PARAMS_IN+=("appPackageUrl=$OLD_PACKAGE")
    fi
  fi
  python3 - "$SECRET_FILE" "${PARAMS_IN[@]+"${PARAMS_IN[@]}"}" <<'PY'
import json, sys
path, *pairs = sys.argv[1:]
params = {}
for pair in pairs:
    name, sep, value = pair.partition("=")
    if not sep:
        sys.exit(f"--param expects NAME=VALUE, got '{name}'")
    params[name] = {"value": value}
json.dump({"parameters": params}, open(path, "w"))
PY

  EXTRA=()
  [[ ${#OVERRIDES[@]} -gt 0 ]] && EXTRA=(--parameters "${OVERRIDES[@]}")
  echo "==> Deploying infrastructure with $(basename "$PARAMS")"
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
STORAGE="$(out storageAccountName)"
CONTAINER="$(out documentsContainer)"
AUTH_MODE="$(out authMode 2>/dev/null || echo none)"

# Storage access with the account key, which the management API returns. Used by --code-via-arm,
# where no Storage data-plane token is available.
with_storage_key() {
  AZURE_STORAGE_ACCOUNT="$STORAGE" AZURE_STORAGE_KEY="$(az storage account keys list -g "$RG" -n "$STORAGE" --query "[0].value" -o tsv)" "$@"
}
upload_samples_with_key() {
  echo "==> Uploading the sample documents to $STORAGE/$CONTAINER"
  local f
  for f in "$ROOT"/Data/sample_docs/*; do
    [[ -f "$f" ]] || continue
    echo "  $(basename "$f")"
    with_storage_key az storage blob upload -c "$CONTAINER" -f "$f" -n "$(basename "$f")" --overwrite -o none --only-show-errors
  done
}
SEEDED=0

if [[ $SKIP_CODE -eq 0 ]]; then
  if [[ "$CODE_VIA_ARM" == "1" ]]; then
    echo "==> Packaging with dependencies (no remote build)"
    python3 "$SCRIPTS/package.py" --vendor
    BLOB="webapp-$(date -u +%Y%m%d%H%M%S).zip"
    echo "==> Uploading $BLOB to $STORAGE/code-packages"
    with_storage_key az storage container create -n code-packages -o none --only-show-errors
    with_storage_key az storage blob upload -c code-packages -f "$ROOT/build/webapp.zip" -n "$BLOB" -o none --only-show-errors
    EXPIRY="$(python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(days=365)).strftime("%Y-%m-%dT%H:%MZ"))')"
    PACKAGE_URL="$(with_storage_key az storage blob generate-sas -c code-packages -n "$BLOB" --permissions r \
      --expiry "$EXPIRY" --https-only --full-uri -o tsv)"
    # Older packages are no longer used; keep the three most recent.
    with_storage_key az storage blob list -c code-packages --query "sort_by([], &name)[].name" -o tsv \
      | head -n -3 | while read -r old; do with_storage_key az storage blob delete -c code-packages -n "$old" -o none; done
    if [[ "$SEED" == "1" ]]; then upload_samples_with_key; SEEDED=1; fi
    echo "==> Pointing web app $WEB_APP at the new package and restarting"
    az webapp config appsettings set -g "$RG" -n "$WEB_APP" -o none --settings \
      WEBSITE_RUN_FROM_PACKAGE="$PACKAGE_URL" SCM_DO_BUILD_DURING_DEPLOYMENT=false PYTHONPATH=/home/site/wwwroot/.packages
    az webapp restart -g "$RG" -n "$WEB_APP" -o none
  else
    echo "==> Packaging"
    python3 "$SCRIPTS/package.py"
    echo "==> Deploying web app $WEB_APP (remote build, a few minutes)"
    az webapp deploy -g "$RG" -n "$WEB_APP" --src-path "$ROOT/build/webapp.zip" --type zip -o none
  fi

  echo "==> Waiting for $WEB_URL/healthz (the first start can take a few minutes)"
  state=""
  for _ in $(seq 1 40); do
    state="$(curl -fsS --max-time 20 "$WEB_URL/healthz" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("searchSetup",""))' 2>/dev/null || true)"
    [[ "$state" == "ready" || "$state" == "failed" ]] && break
    sleep 15
  done
  case "$state" in
    ready) echo "Web app is healthy and the search index is in place." ;;
    failed) echo "Warning: the web app is up but could not create the search index. See $WEB_URL/healthz (searchSetupDetail)." >&2 ;;
    *) echo "Warning: the web app did not report ready yet. Check $WEB_URL/healthz in a few minutes." >&2 ;;
  esac
fi

if [[ "$SEED" == "1" && $SEEDED -eq 0 ]]; then
  if [[ "$CODE_VIA_ARM" == "1" ]]; then
    upload_samples_with_key
    echo "The indexer picks the documents up within 30 minutes (or restart the web app to index them now)."
  else
    "$SCRIPTS/ingest.sh" -g "$RG" -n "$DEPLOYMENT" "$ROOT/Data/sample_docs"
  fi
fi

echo
echo "Done. Chat page: $WEB_URL"
if [[ "$AUTH_MODE" == "password" ]]; then
  echo "Sign-in:         the demo password (Key Vault secret demo-password in $VAULT)."
fi
if [[ -n "$NEW_UPLOAD_KEY" ]]; then
  echo "Upload key:      $NEW_UPLOAD_KEY"
  echo "                 Save it now. It is stored in Key Vault $VAULT as upload-api-key; pass"
  echo "                 --param uploadApiKey='...' on a later deployment to change it."
else
  echo "Upload key:      Key Vault $VAULT, secret upload-api-key (unchanged)."
fi
