#!/usr/bin/env bash
# common.sh - shared settings and helpers for deploy.sh and deploy-openai.sh.
# Meant to be sourced, not run directly.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE_DIR="$ROOT/templates"
SKU_FILE="${SKU_FILE:-$ROOT/config/skus.env}"

# ---------- Configuration (override with environment variables or edit here) ----------
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-chatbot-dev}"
LOCATION="${LOCATION:-centralindia}"                 # network, app, search, data, gateway
OPENAI_LOCATION="${OPENAI_LOCATION:-eastus2}"        # Azure OpenAI account (must offer the chosen models)
NAME_PREFIX="${NAME_PREFIX:-chatbot}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
HUB_ADDRESS_PREFIX="${HUB_ADDRESS_PREFIX:-10.10.0.0/24}"
HUB_APPGW_SUBNET_PREFIX="${HUB_APPGW_SUBNET_PREFIX:-10.10.0.0/26}"
SPOKE_ADDRESS_PREFIX="${SPOKE_ADDRESS_PREFIX:-10.20.0.0/24}"
SPOKE_APP_SUBNET_PREFIX="${SPOKE_APP_SUBNET_PREFIX:-10.20.0.0/26}"
SPOKE_PE_SUBNET_PREFIX="${SPOKE_PE_SUBNET_PREFIX:-10.20.0.64/26}"
# Disabled = web app reachable only through its private endpoint (and so through the gateway).
# Set to Enabled if you need to deploy code from outside the VNet. The private endpoint stays.
APP_PUBLIC_ACCESS="${APP_PUBLIC_ACCESS:-Disabled}"
ASSUME_YES="${ASSUME_YES:-false}"
# -------------------------------------------------------------------------------------------

log()  { echo -e "\n\033[1;36m==> $1\033[0m"; }
warn() { echo -e "\033[1;33m! $1\033[0m"; }
die()  { echo -e "\033[1;31mError: $1\033[0m" >&2; exit 1; }

require_tools() {
  command -v az >/dev/null 2>&1 || die "Azure CLI not found. Install it first."
  command -v jq >/dev/null 2>&1 || die "jq not found. Install it first (used to parse deployment outputs)."
  az account show >/dev/null 2>&1 || die "Run 'az login' first."
  SUBSCRIPTION_ID=$(az account show --query id -o tsv)
  echo "Using subscription: $SUBSCRIPTION_ID"
}

load_skus() {
  [[ -f "$SKU_FILE" ]] || die "SKU file not found: $SKU_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$SKU_FILE"
  set +a
  local required=(
    APPGW_SKU APPGW_CAPACITY APP_PLAN_SKU APP_RUNTIME OPENAI_ACCOUNT_SKU
    CHAT_MODEL_NAME CHAT_MODEL_VERSION CHAT_DEPLOYMENT_NAME CHAT_DEPLOYMENT_SKU CHAT_CAPACITY_K_TPM
    EMBEDDING_MODEL_NAME EMBEDDING_MODEL_VERSION EMBEDDING_DEPLOYMENT_NAME EMBEDDING_DEPLOYMENT_SKU EMBEDDING_CAPACITY_K_TPM
    SEARCH_SKU SEARCH_REPLICAS SEARCH_PARTITIONS SEARCH_SEMANTIC COSMOS_CAPACITY_MODE COSMOS_THROUGHPUT
    STORAGE_SKU STORAGE_ACCESS_TIER KEYVAULT_SKU KEYVAULT_SOFT_DELETE_DAYS
    LOG_ANALYTICS_SKU LOG_RETENTION_DAYS APPINSIGHTS_RETENTION_DAYS
  )
  local v missing=()
  for v in "${required[@]}"; do
    [[ -n "${!v:-}" ]] || missing+=("$v")
  done
  (( ${#missing[@]} == 0 )) || die "Missing values in $SKU_FILE: ${missing[*]}"
  case "$APP_PLAN_SKU" in
    F1|FREE|D1|SHARED) die "APP_PLAN_SKU=$APP_PLAN_SKU does not support private endpoints. Use B1 or above." ;;
  esac
  if [[ "$SEARCH_SKU" == "free" ]]; then die "SEARCH_SKU=free does not support private endpoints. Use basic or above."; fi
}

confirm() {
  [[ "$ASSUME_YES" == "true" ]] && return 0
  local ans
  read -r -p "$1 [y/N] " ans
  [[ "$ans" == "y" || "$ans" == "Y" ]]
}

# deploy_template <name> <param>...   (prints outputs JSON; deployment is named after the template)
deploy_template() {
  local name="$1"; shift
  az deployment group create \
    --name "$name" \
    --resource-group "$RESOURCE_GROUP" \
    --template-file "$TEMPLATE_DIR/$name.json" \
    --parameters "$@" \
    --query properties.outputs -o json
}

# get_output <json> <jq-path>
get_output() { jq -r "$2" <<<"$1"; }

# deployment_outputs <deployment-name>   (outputs of an earlier deployment in the resource group)
deployment_outputs() {
  az deployment group show --resource-group "$RESOURCE_GROUP" --name "$1" \
    --query properties.outputs -o json 2>/dev/null || die "Deployment '$1' not found in $RESOURCE_GROUP. Run ./deploy.sh first."
}

# Warn early if the chosen model/version/deployment type isn't offered in the OpenAI region.
check_openai_models() {
  log "Checking Azure OpenAI model availability in $OPENAI_LOCATION"
  local models
  if ! models=$(az cognitiveservices model list --location "$OPENAI_LOCATION" -o json 2>/dev/null); then
    warn "Could not list models for $OPENAI_LOCATION; skipping the availability check."
    return 0
  fi
  local pair name version sku ok bad=0
  for pair in "$CHAT_MODEL_NAME|$CHAT_MODEL_VERSION|$CHAT_DEPLOYMENT_SKU" \
              "$EMBEDDING_MODEL_NAME|$EMBEDDING_MODEL_VERSION|$EMBEDDING_DEPLOYMENT_SKU"; do
    IFS='|' read -r name version sku <<<"$pair"
    ok=$(jq -r --arg n "$name" --arg v "$version" --arg s "$sku" \
      '[.[] | select(.model.name==$n and .model.version==$v) | .model.skus[]? | select(.name==$s)] | length' <<<"$models")
    if [[ "$ok" == "0" || -z "$ok" ]]; then
      warn "Not found in $OPENAI_LOCATION: $name version $version with deployment type $sku"
      bad=1
    else
      echo "OK: $name $version ($sku)"
    fi
  done
  if (( bad )); then
    warn "Chat models this region lists (name, version):"
    jq -r '[.[] | select(.model.name | test("gpt|o[0-9]")) | "  \(.model.name)  \(.model.version)"] | unique | .[]' <<<"$models" | head -25
    [[ "$ASSUME_YES" == "true" ]] && die "Model not available in $OPENAI_LOCATION. Edit $SKU_FILE or OPENAI_LOCATION and retry."
    confirm "The deployment will probably fail. Continue anyway?" || die "Stopped. Edit $SKU_FILE or OPENAI_LOCATION and retry."
  fi
}
