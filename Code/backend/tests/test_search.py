import json

import httpx
import pytest

from chatbot.config import Settings
from chatbot.errors import UpstreamBusy, UpstreamError
from chatbot.search import (SearchClient, datasource_definition, ensure_search_resources, index_definition,
                            indexer_definition, skillset_definition)

RESOURCE_ID = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/st"


def settings(**overrides):
    base = dict(mode="azure", openai_endpoint="https://aoai.openai.azure.com", search_endpoint="https://srch.search.windows.net",
                storage_account_url="https://st.blob.core.windows.net/", storage_resource_id=RESOURCE_ID,
                search_semantic_config="default", embedding_model="text-embedding-3-large", embedding_dimensions=3072)
    base.update(overrides)
    return Settings(**base)


def test_index_vector_field_matches_embedding_dimensions():
    index = index_definition(settings())
    vector = next(f for f in index["fields"] if f["name"] == "text_vector")
    assert vector["dimensions"] == 3072
    assert next(f for f in index["fields"] if f.get("key"))["analyzer"] == "keyword"
    vectorizer = index["vectorSearch"]["vectorizers"][0]["azureOpenAIParameters"]
    assert vectorizer == {"resourceUri": "https://aoai.openai.azure.com", "deploymentId": "embedding",
                          "modelName": "text-embedding-3-large"}
    assert index["semantic"]["defaultConfiguration"] == "default"


def test_semantic_configuration_only_when_enabled():
    assert "semantic" not in index_definition(settings(search_semantic_config=""))


def test_datasource_uses_managed_identity_connection():
    ds = datasource_definition(settings())
    assert ds["credentials"]["connectionString"] == f"ResourceId={RESOURCE_ID};"
    assert "AccountKey" not in json.dumps(ds)
    assert ds["container"]["name"] == "documents"


def test_skillset_projects_chunks_into_index():
    sk = skillset_definition(settings())
    embed = sk["skills"][1]
    assert embed["dimensions"] == 3072 and "apiKey" not in embed
    selector = sk["indexProjections"]["selectors"][0]
    assert selector["targetIndexName"] == "documents-index"
    assert {m["name"] for m in selector["mappings"]} == {"chunk", "text_vector", "title"}
    index_fields = {f["name"] for f in index_definition(settings())["fields"]}
    assert {m["name"] for m in selector["mappings"]} | {selector["parentKeyFieldName"]} <= index_fields


def test_indexer_links_the_other_definitions():
    s = settings()
    ix = indexer_definition(s)
    assert (ix["dataSourceName"], ix["skillsetName"], ix["targetIndexName"]) == (s.search_datasource, s.search_skillset, s.search_index)


def make_client(handler, s=None):
    return SearchClient(s or settings(), lambda: "token-123", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_hybrid_semantic_query():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"value": [{"chunk_id": "1"}]})

    assert make_client(handler).search("warranty", 5) == [{"chunk_id": "1"}]
    assert seen["url"] == "https://srch.search.windows.net/indexes/documents-index/docs/search?api-version=2024-07-01"
    assert seen["auth"] == "Bearer token-123"
    body = seen["body"]
    assert body["search"] == "warranty" and body["top"] == 5
    assert body["vectorQueries"][0] == {"kind": "text", "text": "warranty", "fields": "text_vector", "k": 50}
    assert body["queryType"] == "semantic" and body["semanticConfiguration"] == "default"


def test_query_without_semantic_ranker():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"value": []})

    make_client(handler, settings(search_semantic_config="")).search("q", 3)
    assert "queryType" not in seen["body"] and seen["body"]["vectorQueries"][0]["k"] == 3


def test_busy_and_error_responses():
    with pytest.raises(UpstreamBusy):
        make_client(lambda r: httpx.Response(429)).search("q", 1)
    with pytest.raises(UpstreamError, match="Index not found"):
        make_client(lambda r: httpx.Response(404, json={"error": {"message": "Index not found"}})).search("q", 1)

    def unreachable(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(UpstreamError, match="could not be reached"):
        make_client(unreachable).search("q", 1)


def test_run_indexer_when_already_running():
    assert make_client(lambda r: httpx.Response(202)).run_indexer() is True
    assert make_client(lambda r: httpx.Response(409)).run_indexer() is False


def test_indexer_status_summary():
    payload = {"status": "running", "lastResult": {"status": "success", "endTime": "2026-01-01T00:00:00Z",
                                                    "itemsProcessed": 3, "itemsFailed": 1,
                                                    "errors": [{"errorMessage": "bad file"}]}}
    status = make_client(lambda r: httpx.Response(200, json=payload)).indexer_status()
    assert status == {"status": "success", "lastRunEnd": "2026-01-01T00:00:00Z", "itemsProcessed": 3,
                      "itemsFailed": 1, "errors": ["bad file"]}


def test_ensure_creates_definitions_in_dependency_order():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(201)

    ensure_search_resources(make_client(handler))
    assert calls == [("PUT", "/indexes/documents-index"), ("PUT", "/datasources/documents-index-datasource"),
                     ("PUT", "/skillsets/documents-index-skillset"), ("PUT", "/indexers/documents-index-indexer")]


def test_ensure_explains_incompatible_index_change():
    client = make_client(lambda r: httpx.Response(400, json={"error": {"message": "Field 'text_vector' dimensions cannot be changed"}}))
    with pytest.raises(UpstreamError, match="Delete the index"):
        ensure_search_resources(client)


def test_token_failure_is_an_upstream_error():
    def no_token():
        raise RuntimeError("no managed identity")

    client = SearchClient(settings(), no_token, http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    with pytest.raises(UpstreamError, match="Microsoft Entra token"):
        client.search("q", 1)
