#!/usr/bin/env bash
# deploy-openai.sh - redeploys only the Azure OpenAI account and its model deployments,
# using the network from an earlier ./deploy.sh run. Handy when you change a model,
# version, deployment type or capacity in config/skus.env.
#
# Usage: ./deploy-openai.sh [--yes]
# Note: if the account name already exists, this updates it in place. Keys do not change,
# so the Key Vault secret stays valid.
set -euo pipefail

# shellcheck disable=SC2034
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=true ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"

require_tools
load_skus
check_openai_models

NET=$(deployment_outputs 01-network)
PE_SUBNET_ID=$(get_output "$NET" .peSubnetId.value)
DNS_OPENAI=$(get_output "$NET" .dnsZoneOpenAiId.value)

log "Deploying Azure OpenAI in $OPENAI_LOCATION (resource group: $RESOURCE_GROUP)"
OAI=$(deploy_template 07-openai namePrefix="$NAME_PREFIX" environment="$ENVIRONMENT" \
  location="$OPENAI_LOCATION" peLocation="$LOCATION" accountSku="$OPENAI_ACCOUNT_SKU" \
  chatDeploymentName="$CHAT_DEPLOYMENT_NAME" chatModelName="$CHAT_MODEL_NAME" \
  chatModelVersion="$CHAT_MODEL_VERSION" chatDeploymentSku="$CHAT_DEPLOYMENT_SKU" chatCapacity="$CHAT_CAPACITY_K_TPM" \
  embeddingDeploymentName="$EMBEDDING_DEPLOYMENT_NAME" embeddingModelName="$EMBEDDING_MODEL_NAME" \
  embeddingModelVersion="$EMBEDDING_MODEL_VERSION" embeddingDeploymentSku="$EMBEDDING_DEPLOYMENT_SKU" \
  embeddingCapacity="$EMBEDDING_CAPACITY_K_TPM" peSubnetId="$PE_SUBNET_ID" dnsZoneId="$DNS_OPENAI")

log "Done"
cat <<SUMMARY

  account:              $(get_output "$OAI" .accountName.value)
  endpoint:             $(get_output "$OAI" .endpoint.value)
  chat deployment:      $(get_output "$OAI" .chatDeploymentName.value)  ($CHAT_MODEL_NAME $CHAT_MODEL_VERSION)
  embedding deployment: $(get_output "$OAI" .embeddingDeploymentName.value)  ($EMBEDDING_MODEL_NAME $EMBEDDING_MODEL_VERSION)

If you changed deployment names, update AZURE_OPENAI_CHAT_DEPLOYMENT and
AZURE_OPENAI_EMBEDDING_DEPLOYMENT on the web app.

SUMMARY
