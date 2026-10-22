#!/usr/bin/env bash
# Add documents to a deployed chatbot: upload files to the `documents` container and start the
# search indexer, which splits, embeds and indexes them.
#
#   Deployment/scripts/ingest.sh -g <resource-group> [-n <deployment-name>] [--account-key] [--restart] <file-or-folder>...
#
# -n defaults to the latest chatbot deployment in the resource group.
# --account-key  upload with the storage account key (from the management API) instead of your Entra
#                ID login. Use it in Azure Cloud Shell, where Storage tokens are not available. Needs
#                shared-key access on the storage account (the demo preset turns it on).
# --restart      restart the web app afterwards; it starts the indexer on startup. Useful when the
#                indexer cannot be started directly (Cloud Shell, or a Free-tier search service).
#
# Example: Deployment/scripts/ingest.sh -g rg-chatbot-dev ./policies ./faq.pdf
#
# Uses your az login identity (Microsoft Entra ID; storage keys are disabled). deploy.sh gives the
# deploying identity Storage Blob Data Contributor and Search Service Contributor. Another person
# needs those two roles on the storage account and search service.
set -euo pipefail

RG="" DEPLOYMENT="" USE_KEY=0 RESTART=0
EXTENSIONS="pdf docx pptx xlsx txt md html htm csv json"
PATHS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g|--resource-group) RG="$2"; shift 2 ;;
    -n|--deployment-name) DEPLOYMENT="$2"; shift 2 ;;
    --account-key) USE_KEY=1; shift ;;
    --restart) RESTART=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "Unknown option $1 (see --help)" >&2; exit 1 ;;
    *) PATHS+=("$1"); shift ;;
  esac
done
[[ -n "$RG" && ${#PATHS[@]} -gt 0 ]] || { echo "Usage: $0 -g <resource-group> [-n <deployment>] <file-or-folder>..." >&2; exit 1; }

if [[ -z "$DEPLOYMENT" ]]; then
  DEPLOYMENT="$(az deployment group list -g "$RG" --query "sort_by([?properties.outputs.webAppName], &properties.timestamp)[-1].name" -o tsv 2>/dev/null || true)"
  [[ -n "$DEPLOYMENT" ]] || { echo "No chatbot deployment found in $RG." >&2; exit 1; }
fi
out() { az deployment group show -g "$RG" -n "$DEPLOYMENT" --query "properties.outputs.$1.value" -o tsv; }
ACCOUNT="$(out storageAccountName)"
CONTAINER="$(out documentsContainer)"
SEARCH="$(out searchEndpoint)"
INDEXER="$(out searchIndexerName)"
WEB_APP="$(out webAppName)"
SEARCH_USES_KEYS="$(out searchUsesKeys 2>/dev/null || echo false)"
[[ -n "$ACCOUNT" ]] || { echo "Deployment $DEPLOYMENT not found in $RG (use -n for another environment)." >&2; exit 1; }

supported() {
  local ext="${1##*.}"
  ext="$(echo "$ext" | tr '[:upper:]' '[:lower:]')"
  [[ " $EXTENSIONS " == *" $ext "* ]]
}

FILES=()
for p in "${PATHS[@]}"; do
  if [[ -d "$p" ]]; then
    while IFS= read -r -d '' f; do supported "$f" && FILES+=("$f"); done < <(find "$p" -maxdepth 1 -type f -print0)
  elif [[ -f "$p" ]]; then
    if supported "$p"; then FILES+=("$p"); else echo "Skipping $p (unsupported type)"; fi
  else
    echo "Not found: $p" >&2; exit 1
  fi
done
[[ ${#FILES[@]} -gt 0 ]] || { echo "No supported files found ($EXTENSIONS)." >&2; exit 1; }

# Role assignments made by a fresh deployment can take a few minutes to apply; retry on 403.
retry() {
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$@"; then return 0; fi
    echo "  not ready yet (attempt $attempt/10); waiting 30 s (new role assignments take a few minutes)"
    sleep 30
  done
  return 1
}

echo "==> Uploading ${#FILES[@]} file(s) to $ACCOUNT/$CONTAINER"
if [[ $USE_KEY -eq 1 ]]; then
  KEY="$(az storage account keys list -g "$RG" -n "$ACCOUNT" --query "[0].value" -o tsv)"
fi
for f in "${FILES[@]}"; do
  echo "  $(basename "$f")"
  if [[ $USE_KEY -eq 1 ]]; then
    AZURE_STORAGE_KEY="$KEY" az storage blob upload --account-name "$ACCOUNT" -c "$CONTAINER" \
      -f "$f" -n "$(basename "$f")" --overwrite -o none --only-show-errors
  else
    retry az storage blob upload --auth-mode login --account-name "$ACCOUNT" -c "$CONTAINER" \
      -f "$f" -n "$(basename "$f")" --overwrite -o none --only-show-errors
  fi
done

if [[ $RESTART -eq 1 ]]; then
  echo "==> Restarting web app $WEB_APP; it starts the indexer when it comes back"
  az webapp restart -g "$RG" -n "$WEB_APP" -o none
  echo "Done. Documents are searchable a few minutes after the web app is back."
  exit 0
fi
if [[ $USE_KEY -eq 1 || "${SEARCH_USES_KEYS,,}" == "true" ]]; then
  echo "Done. The indexer picks the documents up within 30 minutes. To index them now, re-run with --restart."
  exit 0
fi

echo "==> Starting indexer $INDEXER"
run_indexer() {
  local result
  if result="$(az rest --method post --url "$SEARCH/indexers/$INDEXER/run?api-version=2024-07-01" \
       --resource https://search.azure.com 2>&1)"; then
    return 0
  fi
  if [[ "$result" == *"409"* || "$result" == *"already running"* ]]; then
    echo "  The indexer is already running; it picks up the new files on its next pass."
    return 0
  fi
  if [[ "$result" == *"404"* ]]; then
    echo "  The indexer does not exist yet. The web app creates it on startup: open the chat page once, then re-run." >&2
    return 2
  fi
  echo "$result" >&2
  return 1
}
retry run_indexer
echo "Done. Documents are searchable after the indexer finishes (usually a few minutes)."
echo "Status: az rest --url \"$SEARCH/indexers/$INDEXER/status?api-version=2024-07-01\" --resource https://search.azure.com --query lastResult"
