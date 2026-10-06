#!/usr/bin/env bash
# ==============================================================================
# deploy.sh - Deploys the full RAG chatbot infrastructure on Azure
#
# Order:
#   1. Network              (hub + spoke VNets, NSGs, peering, private DNS zones)
#   2. Azure Monitor        (Log Analytics + Application Insights)
#   3. Key Vault            (private endpoint)
#   4. Storage Account      (documents container, private endpoint)
#   5. Cosmos DB            (session history, private endpoint)
#   6. Azure AI Search      (hybrid + semantic retrieval, private endpoint)
#   7. Azure OpenAI         (chat + embedding deployments, private endpoint)
#   8. App Service          (web app, managed identity, VNet integration, private endpoint)
#   9. Application Gateway  (public entry point -> web app)
#  10. Key Vault secrets    (keys and connection strings)
#  11. RBAC                 (web app identity -> each backend service)
#      then app settings (Key Vault references)
#
# SKUs and capacities are read from config/skus.env.
# Requires: az cli logged in, jq, Contributor + User Access Administrator (or Owner)
# on the subscription or resource group (step 11 creates role assignments).
#
# Usage: ./deploy.sh [--yes] [--help]
# ==============================================================================
set -euo pipefail

# shellcheck disable=SC2034
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=true ;;
    -h|--help) sed -n '2,24p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)"; exit 1 ;;
  esac
done

# shellcheck source=scripts/common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"

require_tools
load_skus

cat <<PLAN

Deployment plan
  Resource group:   $RESOURCE_GROUP  ($LOCATION; Azure OpenAI in $OPENAI_LOCATION)
  Name prefix/env:  $NAME_PREFIX / $ENVIRONMENT
  SKU file:         $SKU_FILE
  Application GW:   $APPGW_SKU x$APPGW_CAPACITY
  App Service:      $APP_PLAN_SKU  ($APP_RUNTIME, public access: $APP_PUBLIC_ACCESS)
  AI Search:        $SEARCH_SKU  ($SEARCH_REPLICAS replica, $SEARCH_PARTITIONS partition, semantic: $SEARCH_SEMANTIC)
  Azure OpenAI:     chat $CHAT_MODEL_NAME $CHAT_MODEL_VERSION ($CHAT_DEPLOYMENT_SKU, ${CHAT_CAPACITY_K_TPM}K TPM)
                    embedding $EMBEDDING_MODEL_NAME $EMBEDDING_MODEL_VERSION ($EMBEDDING_DEPLOYMENT_SKU, ${EMBEDDING_CAPACITY_K_TPM}K TPM)
  Cosmos DB:        $COSMOS_CAPACITY_MODE  ($COSMOS_THROUGHPUT RU/s if provisioned)
  Storage:          $STORAGE_SKU ($STORAGE_ACCESS_TIER)
  Key Vault:        $KEYVAULT_SKU
  Monitoring:       $LOG_ANALYTICS_SKU, ${LOG_RETENTION_DAYS}-day retention

PLAN
confirm "Deploy this to subscription $SUBSCRIPTION_ID?" || { echo "Cancelled."; exit 0; }

check_openai_models

log "Creating resource group: $RESOURCE_GROUP"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none

COMMON=(namePrefix="$NAME_PREFIX" environment="$ENVIRONMENT" location="$LOCATION")

# ---------- 1. Network ----------
log "Deploying network (hub, spoke, NSGs, peering, private DNS)"
NET=$(deploy_template 01-network "${COMMON[@]}" \
  hubAddressPrefix="$HUB_ADDRESS_PREFIX" hubAppGwSubnetPrefix="$HUB_APPGW_SUBNET_PREFIX" \
  spokeAddressPrefix="$SPOKE_ADDRESS_PREFIX" spokeAppSubnetPrefix="$SPOKE_APP_SUBNET_PREFIX" \
  spokePeSubnetPrefix="$SPOKE_PE_SUBNET_PREFIX")
APPGW_SUBNET_ID=$(get_output "$NET" .appGwSubnetId.value)
APP_SUBNET_ID=$(get_output "$NET" .appIntegrationSubnetId.value)
PE_SUBNET_ID=$(get_output "$NET" .peSubnetId.value)
DNS_BLOB=$(get_output "$NET" .dnsZoneBlobId.value)
DNS_SEARCH=$(get_output "$NET" .dnsZoneSearchId.value)
DNS_OPENAI=$(get_output "$NET" .dnsZoneOpenAiId.value)
DNS_SITES=$(get_output "$NET" .dnsZoneSitesId.value)
DNS_VAULT=$(get_output "$NET" .dnsZoneVaultId.value)
DNS_COSMOS=$(get_output "$NET" .dnsZoneCosmosId.value)

# ---------- 2. Monitoring ----------
log "Deploying Log Analytics and Application Insights"
MON=$(deploy_template 02-monitor "${COMMON[@]}" logAnalyticsSku="$LOG_ANALYTICS_SKU" \
  logRetentionDays="$LOG_RETENTION_DAYS" appInsightsRetentionDays="$APPINSIGHTS_RETENTION_DAYS")
APPI_CONN=$(get_output "$MON" .appInsightsConnectionString.value)

# ---------- 3. Key Vault ----------
log "Deploying Key Vault"
KV=$(deploy_template 03-keyvault "${COMMON[@]}" skuName="$KEYVAULT_SKU" \
  softDeleteRetentionDays="$KEYVAULT_SOFT_DELETE_DAYS" peSubnetId="$PE_SUBNET_ID" dnsZoneId="$DNS_VAULT")
KEY_VAULT_NAME=$(get_output "$KV" .keyVaultName.value)
KEY_VAULT_URI=$(get_output "$KV" .keyVaultUri.value)
echo "Key Vault: $KEY_VAULT_NAME"

# ---------- 4. Storage ----------
log "Deploying Storage Account"
ST=$(deploy_template 04-storage "${COMMON[@]}" skuName="$STORAGE_SKU" accessTier="$STORAGE_ACCESS_TIER" \
  peSubnetId="$PE_SUBNET_ID" dnsZoneId="$DNS_BLOB")
STORAGE_NAME=$(get_output "$ST" .storageAccountName.value)
STORAGE_BLOB_ENDPOINT=$(get_output "$ST" .blobEndpoint.value)
STORAGE_CONTAINER=$(get_output "$ST" .containerName.value)
echo "Storage Account: $STORAGE_NAME"

# ---------- 5. Cosmos DB ----------
log "Deploying Cosmos DB"
COSMOS=$(deploy_template 05-cosmosdb "${COMMON[@]}" capacityMode="$COSMOS_CAPACITY_MODE" \
  throughput="$COSMOS_THROUGHPUT" peSubnetId="$PE_SUBNET_ID" dnsZoneId="$DNS_COSMOS")
COSMOS_NAME=$(get_output "$COSMOS" .accountName.value)
COSMOS_ENDPOINT=$(get_output "$COSMOS" .endpoint.value)
COSMOS_DB=$(get_output "$COSMOS" .databaseName.value)
COSMOS_CONTAINER=$(get_output "$COSMOS" .containerName.value)
echo "Cosmos DB: $COSMOS_NAME"

# ---------- 6. Azure AI Search ----------
log "Deploying Azure AI Search"
SRCH=$(deploy_template 06-search "${COMMON[@]}" skuName="$SEARCH_SKU" replicaCount="$SEARCH_REPLICAS" \
  partitionCount="$SEARCH_PARTITIONS" semanticSearch="$SEARCH_SEMANTIC" \
  peSubnetId="$PE_SUBNET_ID" dnsZoneId="$DNS_SEARCH")
SEARCH_NAME=$(get_output "$SRCH" .serviceName.value)
SEARCH_ENDPOINT=$(get_output "$SRCH" .endpoint.value)
echo "AI Search: $SEARCH_NAME"

# ---------- 7. Azure OpenAI ----------
log "Deploying Azure OpenAI in $OPENAI_LOCATION (private endpoint stays in $LOCATION)"
OAI=$(deploy_template 07-openai namePrefix="$NAME_PREFIX" environment="$ENVIRONMENT" \
  location="$OPENAI_LOCATION" peLocation="$LOCATION" accountSku="$OPENAI_ACCOUNT_SKU" \
  chatDeploymentName="$CHAT_DEPLOYMENT_NAME" chatModelName="$CHAT_MODEL_NAME" \
  chatModelVersion="$CHAT_MODEL_VERSION" chatDeploymentSku="$CHAT_DEPLOYMENT_SKU" chatCapacity="$CHAT_CAPACITY_K_TPM" \
  embeddingDeploymentName="$EMBEDDING_DEPLOYMENT_NAME" embeddingModelName="$EMBEDDING_MODEL_NAME" \
  embeddingModelVersion="$EMBEDDING_MODEL_VERSION" embeddingDeploymentSku="$EMBEDDING_DEPLOYMENT_SKU" \
  embeddingCapacity="$EMBEDDING_CAPACITY_K_TPM" peSubnetId="$PE_SUBNET_ID" dnsZoneId="$DNS_OPENAI")
OAI_NAME=$(get_output "$OAI" .accountName.value)
OAI_ENDPOINT=$(get_output "$OAI" .endpoint.value)
OAI_CHAT=$(get_output "$OAI" .chatDeploymentName.value)
OAI_EMB=$(get_output "$OAI" .embeddingDeploymentName.value)
echo "Azure OpenAI: $OAI_NAME (chat: $OAI_CHAT, embedding: $OAI_EMB)"

# ---------- 8. App Service ----------
log "Deploying App Service"
APP=$(deploy_template 08-appservice "${COMMON[@]}" planSku="$APP_PLAN_SKU" linuxFxVersion="$APP_RUNTIME" \
  publicNetworkAccess="$APP_PUBLIC_ACCESS" appInsightsConnectionString="$APPI_CONN" \
  integrationSubnetId="$APP_SUBNET_ID" peSubnetId="$PE_SUBNET_ID" dnsZoneId="$DNS_SITES")
WEB_APP_NAME=$(get_output "$APP" .webAppName.value)
WEB_APP_HOST=$(get_output "$APP" .defaultHostName.value)
APP_PRINCIPAL_ID=$(get_output "$APP" .principalId.value)
echo "Web app: $WEB_APP_NAME"

# ---------- 9. Application Gateway ----------
log "Deploying Application Gateway (this one takes a while)"
AGW=$(deploy_template 09-appgateway "${COMMON[@]}" skuName="$APPGW_SKU" capacity="$APPGW_CAPACITY" \
  appGwSubnetId="$APPGW_SUBNET_ID" backendFqdn="$WEB_APP_HOST")
AGW_NAME=$(get_output "$AGW" .gatewayName.value)
AGW_FQDN=$(get_output "$AGW" .publicFqdn.value)
AGW_IP=$(get_output "$AGW" .publicIpAddress.value)

# ---------- 10. Key Vault secrets ----------
log "Storing keys and connection strings in Key Vault"
deploy_template 10-secrets keyVaultName="$KEY_VAULT_NAME" searchName="$SEARCH_NAME" openAiName="$OAI_NAME" \
  cosmosName="$COSMOS_NAME" storageName="$STORAGE_NAME" >/dev/null

# ---------- 11. RBAC ----------
log "Granting the web app's managed identity access to each service"
deploy_template 11-rbac principalId="$APP_PRINCIPAL_ID" keyVaultName="$KEY_VAULT_NAME" storageName="$STORAGE_NAME" \
  searchName="$SEARCH_NAME" openAiName="$OAI_NAME" cosmosName="$COSMOS_NAME" >/dev/null

# ---------- App settings (Key Vault references) ----------
log "Pointing the web app at Key Vault secrets and service endpoints"
az webapp config appsettings set --resource-group "$RESOURCE_GROUP" --name "$WEB_APP_NAME" --settings \
  "AZURE_OPENAI_ENDPOINT=$OAI_ENDPOINT" \
  "AZURE_OPENAI_API_KEY=@Microsoft.KeyVault(SecretUri=${KEY_VAULT_URI}secrets/OpenAIApiKey/)" \
  "AZURE_OPENAI_CHAT_DEPLOYMENT=$OAI_CHAT" \
  "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=$OAI_EMB" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_ENDPOINT" \
  "AZURE_SEARCH_ADMIN_KEY=@Microsoft.KeyVault(SecretUri=${KEY_VAULT_URI}secrets/SearchAdminKey/)" \
  "COSMOS_ENDPOINT=$COSMOS_ENDPOINT" \
  "COSMOS_DATABASE=$COSMOS_DB" \
  "COSMOS_CONTAINER=$COSMOS_CONTAINER" \
  "COSMOS_CONNECTION_STRING=@Microsoft.KeyVault(SecretUri=${KEY_VAULT_URI}secrets/CosmosDbConnectionString/)" \
  "STORAGE_ACCOUNT_NAME=$STORAGE_NAME" \
  "STORAGE_BLOB_ENDPOINT=$STORAGE_BLOB_ENDPOINT" \
  "STORAGE_CONTAINER=$STORAGE_CONTAINER" \
  "STORAGE_CONNECTION_STRING=@Microsoft.KeyVault(SecretUri=${KEY_VAULT_URI}secrets/StorageConnectionString/)" \
  "KEY_VAULT_URI=$KEY_VAULT_URI" \
  -o none

log "Deployment complete"
cat <<SUMMARY

Resource Group:        $RESOURCE_GROUP  ($LOCATION; Azure OpenAI in $OPENAI_LOCATION)
Public endpoint:       http://$AGW_FQDN   (Application Gateway $AGW_NAME, IP $AGW_IP)
Web app:               $WEB_APP_NAME  (private; reached through the gateway)
Key Vault:             $KEY_VAULT_NAME
Storage Account:       $STORAGE_NAME  (container "$STORAGE_CONTAINER")
Cosmos DB:             $COSMOS_NAME  (database "$COSMOS_DB", container "$COSMOS_CONTAINER")
Azure AI Search:       $SEARCH_NAME
Azure OpenAI:          $OAI_NAME  (chat "$OAI_CHAT", embedding "$OAI_EMB")

Secrets stored in Key Vault "$KEY_VAULT_NAME":
  - OpenAIApiKey
  - SearchAdminKey
  - CosmosDbConnectionString
  - StorageConnectionString

Next: the gateway listens on HTTP port 80 only. Add a TLS certificate and an HTTPS listener
before real users reach it (see the README). Role assignments and Key Vault references can take
a few minutes to start working; restart the web app if settings show as unresolved.

SUMMARY
