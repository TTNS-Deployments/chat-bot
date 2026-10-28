import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.config import Settings
from chatbot.errors import UpstreamBusy, UpstreamError
from chatbot.history import ConversationStore
from chatbot.main import RateLimiter, create_app
from chatbot.retrieval import Passage
from chatbot.service import ChatService, Components


class FakeRetriever:
    def __init__(self):
        self.queries, self.scopes = [], []

    def document_names(self):
        return ["policy.pdf", "faq.pdf"]

    def retrieve(self, query, top, documents=()):
        self.queries.append(query)
        self.scopes.append(tuple(documents))
        passages = [Passage("c1", "policy.pdf", "Refunds take 5 days."), Passage("c2", "faq.pdf", "Unrelated.")]
        return [p for p in passages if not documents or p.title in documents]


class FakeModel:
    def __init__(self, reply="Refunds take 5 days [1].", error=None):
        self.reply, self.error, self.messages, self.max_tokens = reply, error, None, None

    def complete(self, messages, max_tokens=None):
        self.messages, self.max_tokens = messages, max_tokens
        if self.error:
            raise self.error
        return self.reply


class MemoryStore:
    def __init__(self):
        self.files = {}

    def save(self, name, data, content_type):
        from chatbot.documents import StoredDocument
        self.files[name] = data
        return StoredDocument(name, len(data))

    def list(self):
        from chatbot.documents import StoredDocument
        return [StoredDocument(n, len(d)) for n, d in self.files.items()]

    def delete(self, name):
        return self.files.pop(name, None) is not None


class FakeSearch:
    def __init__(self):
        self.runs = 0

    def run_indexer(self):
        self.runs += 1
        return True

    def indexer_status(self):
        return {"status": "success"}


def make(settings=None, model=None, search=None):
    settings = settings or Settings(mode="local", upload_api_key="secret-key", rate_limit_per_minute=0)
    retriever, model = FakeRetriever(), model or FakeModel()
    components = Components(chat=ChatService(settings, retriever, model), documents=MemoryStore(), search=search,
                            conversations=ConversationStore(Path(tempfile.mkdtemp(prefix="chatbot-test-"))))
    client = TestClient(create_app(settings, components, start_search_setup=False))
    return client, components, retriever, model


def test_health_and_config():
    client, *_ = make()
    assert client.get("/healthz").json()["status"] == "ok"
    cfg = client.get("/api/config").json()
    assert cfg["title"] == "AI Based Chatbot" and cfg["subtitle"] == "AI based chatbot platform"


def test_chat_returns_answer_with_cited_sources_only():
    client, _, retriever, model = make()
    history = [{"role": "user", "content": "What about refunds?"}, {"role": "assistant", "content": "Which product?"}]
    r = client.post("/api/chat", json={"message": "  the fridge  ", "history": history})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Refunds take 5 days [1]."
    assert body["citations"] == [{"id": 1, "title": "policy.pdf", "snippet": "Refunds take 5 days.", "section": ""}]
    assert retriever.queries == ["What about refunds? the fridge"]
    assert [m["role"] for m in model.messages] == ["system", "user", "assistant", "user"]
    assert model.messages[-1]["content"].endswith("Question: the fridge")


@pytest.mark.parametrize("payload, field", [
    ({"message": ""}, "message"),
    ({"message": "   "}, "message"),
    ({"message": "x" * 4001}, "message"),
    ({}, "message"),
    ({"message": "hi", "history": [{"role": "system", "content": "x"}]}, "history"),
    ({"message": "hi", "history": [{"role": "user", "content": "x"}] * 41}, "history"),
])
def test_chat_input_validation(payload, field):
    client, *_ = make()
    r = client.post("/api/chat", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    assert r.json()["error"]["message"].startswith(field)


def test_upstream_errors_are_hidden_and_busy_is_503():
    client, *_ = make(model=FakeModel(error=UpstreamError("secret internal detail")))
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 502 and "secret" not in r.text
    client, *_ = make(model=FakeModel(error=UpstreamBusy("busy")))
    assert client.post("/api/chat", json={"message": "hi"}).status_code == 503


def test_unexpected_errors_return_500_json():
    settings = Settings(mode="local", rate_limit_per_minute=0)
    client, *_ = make(settings, model=FakeModel(error=RuntimeError("boom")))
    client = TestClient(client.app, raise_server_exceptions=False)
    r = client.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"


def test_rate_limit():
    client, *_ = make(Settings(mode="local", rate_limit_per_minute=2))
    codes = [client.post("/api/chat", json={"message": "hi"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_rate_limiter_window_and_disable():
    limiter = RateLimiter(1)
    assert limiter.allow("a", now=0) and not limiter.allow("a", now=30) and limiter.allow("a", now=61)
    assert limiter.allow("b", now=30)
    assert all(RateLimiter(0).allow("a") for _ in range(100))


def test_upload_needs_no_key():
    client, components, *_ = make(search=FakeSearch())
    files = {"file": ("Policy Guide.pdf", b"%PDF-1.7 data", "application/pdf")}
    r = client.post("/api/documents", files=files)
    assert r.status_code == 201
    assert r.json() == {"name": "Policy Guide.pdf", "size": 13, "indexing": "started"}
    assert components.search.runs == 1
    listing = client.get("/api/documents").json()
    assert listing["documents"][0]["name"] == "Policy Guide.pdf" and listing["indexer"]["status"] == "success"


def test_upload_ignores_a_stale_key_header():
    client, *_ = make()
    r = client.post("/api/documents", files={"file": ("a.txt", b"x", "text/plain")}, headers={"X-Upload-Key": "wrong"})
    assert r.status_code == 201


def test_upload_rejects_bad_files():
    client, *_ = make(Settings(mode="local", upload_api_key="k", max_upload_mb=1, rate_limit_per_minute=0))
    assert client.post("/api/documents", files={"file": ("run.exe", b"MZ", "application/octet-stream")}).status_code == 415
    assert client.post("/api/documents", files={"file": ("big.txt", b"a" * (1024 * 1024 + 1), "text/plain")}).status_code == 413
    assert client.post("/api/documents", files={"file": ("empty.txt", b"", "text/plain")}).status_code == 422


def test_upload_disabled():
    client, *_ = make(Settings(mode="local", allow_document_upload=False))
    r = client.post("/api/documents", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 403 and r.json()["error"]["code"] == "upload_disabled"


def test_upload_is_rate_limited():
    client, *_ = make(Settings(mode="local", rate_limit_per_minute=1))
    codes = [client.post("/api/documents", files={"file": ("a.txt", b"x", "text/plain")}).status_code for _ in range(2)]
    assert codes == [201, 429]


def test_upload_works_in_azure_mode_while_the_key_vault_reference_is_unresolved():
    literal = "@Microsoft.KeyVault(VaultName=kv;SecretName=upload-api-key)"
    settings = Settings(mode="azure", upload_api_key=literal, rate_limit_per_minute=0,
                        openai_endpoint="https://a.openai.azure.com", search_endpoint="https://s.search.windows.net",
                        storage_account_url="https://st.blob.core.windows.net/", storage_resource_id="/subscriptions/x")
    client, *_ = make(settings, search=FakeSearch())
    assert client.post("/api/documents", files={"file": ("a.txt", b"x", "text/plain")}).status_code == 201
    # Deleting still needs the key, and an unresolved reference never matches.
    r = client.delete("/api/documents/a.txt", headers={"X-Upload-Key": literal})
    assert r.status_code == 503 and r.json()["error"]["code"] == "not_configured"


def test_upload_failures_are_reported_clearly():
    class BrokenStore(MemoryStore):
        def save(self, name, data, content_type):
            raise OSError("storage account unreachable")

    class BrokenSearch(FakeSearch):
        def run_indexer(self):
            raise UpstreamError("indexer busy")

    client, components, *_ = make()
    components.documents = BrokenStore()
    r = client.post("/api/documents", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upload_failed" and "unreachable" not in r.text
    client, components, *_ = make(search=BrokenSearch())
    r = client.post("/api/documents", files={"file": ("a.txt", b"x", "text/plain")})
    assert r.status_code == 201 and r.json()["indexing"] == "scheduled"


def test_chat_page_has_no_key_field():
    client, *_ = make()
    page, script = client.get("/").text.lower(), client.get("/app.js").text.lower()
    assert 'id="file"' in page and 'id="upload-form"' in page
    for text in (page, script):
        assert "upload-key" not in text and "upload key" not in text and "x-upload-key" not in text
        assert 'type="password"' not in text and "api key" not in text


def test_delete_document_needs_the_key():
    client, components, *_ = make(search=FakeSearch())
    components.documents.files["a.txt"] = b"x"
    assert client.delete("/api/documents/a.txt").status_code == 401
    assert client.delete("/api/documents/a.txt", headers={"X-Upload-Key": "wrong"}).status_code == 401
    h = {"X-Upload-Key": "secret-key"}
    assert client.delete("/api/documents/a.txt", headers=h).json() == {"deleted": "a.txt"}
    assert client.delete("/api/documents/a.txt", headers=h).status_code == 404
    assert components.search.runs == 1


def test_chat_page_is_served_with_security_headers():
    client, *_ = make()
    r = client.get("/")
    assert r.status_code == 200 and "<title>AI Based Chatbot</title>" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/app.js").status_code == 200


def test_azure_mode_needs_endpoints():
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        create_app(Settings(mode="azure"), start_search_setup=False)
