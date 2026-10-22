"""Azure AI Search over REST (GA API version 2024-07-01) with Microsoft Entra ID tokens.

Holds the definitions of the index, data source, skillset and indexer that turn files in the
`documents` blob container into searchable chunks (integrated vectorization), and creates them
idempotently. The definitions are plain JSON so they can be read next to the REST reference.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import httpx

from .config import ALLOWED_EXTENSIONS, Settings
from .errors import UpstreamBusy, UpstreamError

log = logging.getLogger(__name__)

VECTOR_PROFILE = "default-profile"
VECTORIZER = "aoai-vectorizer"
SEMANTIC_CONFIG = "default"
CHUNK_CHARS = 2000
CHUNK_OVERLAP = 300


# --------------------------------------------------------------------------------------
# Definitions
# --------------------------------------------------------------------------------------
def _aoai_parameters(s: Settings) -> dict:
    params = {
        "resourceUri": s.embedding_resource_url,
        "deploymentId": s.embedding_deployment,
        "modelName": s.embedding_model,
    }
    # Without a key the search service uses its managed identity (Basic tier and higher).
    if s.embedding_api_key:
        params["apiKey"] = s.embedding_api_key
    return params


def index_definition(s: Settings) -> dict:
    index: dict[str, Any] = {
        "name": s.search_index,
        "fields": [
            {"name": "chunk_id", "type": "Edm.String", "key": True, "searchable": True,
             "filterable": False, "sortable": True, "facetable": False, "analyzer": "keyword"},
            {"name": "parent_id", "type": "Edm.String", "searchable": False, "filterable": True,
             "sortable": False, "facetable": False},
            {"name": "title", "type": "Edm.String", "searchable": True, "filterable": True,
             "sortable": False, "facetable": False},
            {"name": "chunk", "type": "Edm.String", "searchable": True, "filterable": False,
             "sortable": False, "facetable": False},
            {"name": "text_vector", "type": "Collection(Edm.Single)", "searchable": True,
             "retrievable": False, "stored": False, "dimensions": s.embedding_dimensions,
             "vectorSearchProfile": VECTOR_PROFILE},
        ],
        "vectorSearch": {
            "algorithms": [{"name": "hnsw", "kind": "hnsw", "hnswParameters": {"metric": "cosine"}}],
            "profiles": [{"name": VECTOR_PROFILE, "algorithm": "hnsw", "vectorizer": VECTORIZER}],
            "vectorizers": [{"name": VECTORIZER, "kind": "azureOpenAI", "azureOpenAIParameters": _aoai_parameters(s)}],
        },
    }
    if s.search_semantic_config:
        index["semantic"] = {
            "defaultConfiguration": s.search_semantic_config,
            "configurations": [{
                "name": s.search_semantic_config,
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedContentFields": [{"fieldName": "chunk"}],
                },
            }],
        }
    return index


def datasource_definition(s: Settings) -> dict:
    return {
        "name": s.search_datasource,
        "type": "azureblob",
        # A resource ID (no key) makes the search service connect with its managed identity. A Free
        # search service has no identity and gets a key-based connection string from Key Vault.
        "credentials": {"connectionString": s.search_datasource_connection or f"ResourceId={s.storage_resource_id};"},
        "container": {"name": s.storage_container},
        "dataDeletionDetectionPolicy": {
            "@odata.type": "#Microsoft.Azure.Search.NativeBlobSoftDeleteDeletionDetectionPolicy"
        },
    }


def skillset_definition(s: Settings) -> dict:
    embed: dict[str, Any] = {
        "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
        "name": "embed-chunks",
        "context": "/document/pages/*",
        **_aoai_parameters(s),
        "inputs": [{"name": "text", "source": "/document/pages/*"}],
        "outputs": [{"name": "embedding", "targetName": "text_vector"}],
    }
    if s.embedding_model.startswith("text-embedding-3"):
        embed["dimensions"] = s.embedding_dimensions
    return {
        "name": s.search_skillset,
        "description": "Split documents into overlapping chunks and embed each chunk.",
        "skills": [
            {
                "@odata.type": "#Microsoft.Skills.Text.SplitSkill",
                "name": "split-pages",
                "context": "/document",
                "textSplitMode": "pages",
                "maximumPageLength": CHUNK_CHARS,
                "pageOverlapLength": CHUNK_OVERLAP,
                "inputs": [{"name": "text", "source": "/document/content"}],
                "outputs": [{"name": "textItems", "targetName": "pages"}],
            },
            embed,
        ],
        "indexProjections": {
            "selectors": [{
                "targetIndexName": s.search_index,
                "parentKeyFieldName": "parent_id",
                "sourceContext": "/document/pages/*",
                "mappings": [
                    {"name": "chunk", "source": "/document/pages/*"},
                    {"name": "text_vector", "source": "/document/pages/*/text_vector"},
                    {"name": "title", "source": "/document/metadata_storage_name"},
                ],
            }],
            "parameters": {"projectionMode": "skipIndexingParentDocuments"},
        },
    }


def indexer_definition(s: Settings) -> dict:
    return {
        "name": s.search_indexer,
        "dataSourceName": s.search_datasource,
        "skillsetName": s.search_skillset,
        "targetIndexName": s.search_index,
        # The app starts a run after each upload; the schedule picks up files added in other ways.
        "schedule": {"interval": "PT30M"},
        "parameters": {
            "maxFailedItems": -1,
            "maxFailedItemsPerBatch": -1,
            "configuration": {
                "dataToExtract": "contentAndMetadata",
                "parsingMode": "default",
                "indexedFileNameExtensions": ",".join(ALLOWED_EXTENSIONS),
                "failOnUnsupportedContentType": False,
                "failOnUnprocessableDocument": False,
            },
        },
    }


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------
class SearchClient:
    def __init__(self, settings: Settings, token_provider: Callable[[], str], http: httpx.Client | None = None):
        self.settings = settings
        self._token = token_provider
        self._http = http or httpx.Client(timeout=30.0)

    def _request(self, method: str, path: str, *, json: Any = None, ok: tuple[int, ...] = (200, 201, 202, 204)) -> httpx.Response:
        url = f"{self.settings.search_endpoint}/{path}"
        headers = {"Content-Type": "application/json"}
        if self.settings.search_api_key:  # Free tier: no Microsoft Entra ID, key from Key Vault
            headers["api-key"] = self.settings.search_api_key
        else:
            try:
                headers["Authorization"] = f"Bearer {self._token()}"
            except Exception as exc:  # azure.identity raises ClientAuthenticationError and friends
                raise UpstreamError(f"Could not get a Microsoft Entra token for Azure AI Search: {exc}") from exc
        try:
            response = self._http.request(method, url, params={"api-version": self.settings.search_api_version},
                                          json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Azure AI Search could not be reached: {exc}") from exc
        if response.status_code in ok:
            return response
        detail = _error_message(response)
        if response.status_code in (429, 503):
            raise UpstreamBusy("Azure AI Search is busy. Try again in a moment.")
        raise UpstreamError(f"Azure AI Search returned {response.status_code} for {method} {path}: {detail}",
                            status_code=502)

    # ---- query ----
    def search(self, text: str, top: int) -> list[dict]:
        s = self.settings
        body: dict[str, Any] = {
            "search": text,
            "top": top,
            "select": "chunk_id,parent_id,title,chunk",
            # Hybrid: keyword + vector. The index's vectorizer embeds the text on the service side.
            "vectorQueries": [{
                "kind": "text",
                "text": text,
                "fields": "text_vector",
                "k": 50 if s.search_semantic_config else top,
            }],
        }
        if s.search_semantic_config:
            body["queryType"] = "semantic"
            body["semanticConfiguration"] = s.search_semantic_config
        return self._request("POST", f"indexes/{s.search_index}/docs/search", json=body).json().get("value", [])

    # ---- management ----
    def put(self, collection: str, definition: dict) -> None:
        self._request("PUT", f"{collection}/{definition['name']}", json=definition)

    def run_indexer(self) -> bool:
        """Starts the indexer. Returns False when a run is already in progress."""
        response = self._request("POST", f"indexers/{self.settings.search_indexer}/run", ok=(202, 409))
        return response.status_code == 202

    def indexer_status(self) -> dict:
        data = self._request("GET", f"indexers/{self.settings.search_indexer}/status").json()
        last = data.get("lastResult") or {}
        return {
            "status": last.get("status") or data.get("status", "unknown"),
            "lastRunEnd": last.get("endTime"),
            "itemsProcessed": last.get("itemsProcessed", 0),
            "itemsFailed": last.get("itemsFailed", 0),
            "errors": [e.get("errorMessage", "") for e in (last.get("errors") or [])[:5]],
        }


def _error_message(response: httpx.Response) -> str:
    try:
        return response.json().get("error", {}).get("message", "") or response.text[:300]
    except ValueError:
        return response.text[:300]


def ensure_search_resources(client: SearchClient) -> None:
    """Create or update the index, data source, skillset and indexer. Safe to run repeatedly."""
    s = client.settings
    try:
        client.put("indexes", index_definition(s))
    except UpstreamError as exc:
        if "cannot be changed" in exc.message or "dimensions" in exc.message:
            raise UpstreamError(
                f"{exc.message}. The index was created with different settings (for example another embedding "
                f"model). Delete the index '{s.search_index}' and restart the app to rebuild it."
            ) from exc
        raise
    client.put("datasources", datasource_definition(s))
    client.put("skillsets", skillset_definition(s))
    client.put("indexers", indexer_definition(s))
    log.info("Search resources are in place (index %s, indexer %s)", s.search_index, s.search_indexer)
