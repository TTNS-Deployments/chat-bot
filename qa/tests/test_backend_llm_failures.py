"""LLM / search failure handling. The real `openai` SDK and the app's AzureChatModel / SearchClient are
used; only the network is replaced (httpx.MockTransport or a local socket that never answers)."""
from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
from openai import AzureOpenAI

from conftest import StaticRetriever, components_with, local_settings, make_client

OK_BODY = {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
           "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Refunds take 5 days [1]."}}]}


def sdk(handler, retries=0, timeout=5.0):
    return AzureOpenAI(azure_endpoint="https://mock.openai.azure.com", api_key="qa-not-a-real-key",
                       api_version="2025-04-01-preview", max_retries=retries, timeout=timeout,
                       http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def client_for(tmp_path, handler, **kw):
    from chatbot.llm import AzureChatModel

    model = AzureChatModel(local_settings(tmp_path), client=sdk(handler, **kw))
    return make_client(tmp_path, components=components_with(model, tmp_path))


def ask(c):
    t0 = time.monotonic()
    r = c.post("/api/chat", json={"message": "How long do refunds take?"})
    return r, time.monotonic() - t0


def assert_clean_error(r, status, code):
    assert r.status_code == status, (r.status_code, r.text)
    body = r.json()
    assert body["error"]["code"] == code
    assert "Traceback" not in r.text and "qa-not-a-real-key" not in r.text and "mock.openai" not in r.text


def test_BE_031_llm_timeout(tmp_path, record):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    r, dt = ask(client_for(tmp_path, handler))
    record(actual=f"{r.status_code} {r.text} in {dt:.2f}s", evidence="mock transport raises httpx.ReadTimeout")
    assert_clean_error(r, 502, "upstream_error")


def test_BE_032_llm_rate_limited(tmp_path, record):
    r, dt = ask(client_for(tmp_path, lambda req: httpx.Response(429, json={"error": {"code": "429", "message": "Rate limit"}})))
    record(actual=f"{r.status_code} {r.text} in {dt:.2f}s")
    assert_clean_error(r, 503, "busy")


@pytest.mark.parametrize("status", [500, 502, 503])
def test_BE_033_llm_server_error(tmp_path, record, status):
    r, dt = ask(client_for(tmp_path, lambda req: httpx.Response(status, json={"error": {"message": "boom"}})))
    record(actual=f"upstream {status} -> {r.status_code} {r.text} in {dt:.2f}s")
    assert_clean_error(r, 502, "upstream_error")


@pytest.mark.parametrize("content", ["", None, "   "])
def test_BE_034_llm_empty_response(tmp_path, record, content):
    body = json.loads(json.dumps(OK_BODY))
    body["choices"][0]["message"]["content"] = content
    r, _ = ask(client_for(tmp_path, lambda req: httpx.Response(200, json=body)))
    record(actual=f"content={content!r} -> {r.status_code} {r.text[:140]}")
    assert r.status_code == 200 and "could not produce an answer" in r.json()["answer"]


@pytest.mark.parametrize("label,resp", [
    ("non-JSON body", lambda req: httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"})),
    ("JSON without choices", lambda req: httpx.Response(200, json={"id": "x"})),
    ("empty choices", lambda req: httpx.Response(200, json={**OK_BODY, "choices": []})),
    ("truncated JSON", lambda req: httpx.Response(200, content=b'{"choices": [', headers={"content-type": "application/json"})),
])
def test_BE_035_llm_malformed_response(tmp_path, record, label, resp):
    r, dt = ask(client_for(tmp_path, resp))
    record(actual=f"{label} -> {r.status_code} {r.text[:160]} in {dt:.2f}s")
    # Expected: a clean JSON 5xx error with a friendly message (no stack trace, no hang).
    assert r.status_code >= 500 and r.json()["error"]["message"]
    assert "Traceback" not in r.text and dt < 10


def test_BE_036_llm_content_filter(tmp_path, record):
    prompt_blocked = client_for(tmp_path, lambda req: httpx.Response(400, json={"error": {
        "code": "content_filter", "message": "The response was filtered due to the prompt triggering Azure OpenAI's content management policy."}}))
    body = json.loads(json.dumps(OK_BODY))
    body["choices"][0]["finish_reason"] = "content_filter"
    answer_blocked = client_for(tmp_path, lambda req: httpx.Response(200, json=body))
    r1, _ = ask(prompt_blocked)
    r2, _ = ask(answer_blocked)
    record(actual=f"prompt filtered -> {r1.status_code} {r1.text}; answer filtered -> {r2.status_code} {r2.text}")
    assert_clean_error(r1, 400, "content_filtered")
    assert_clean_error(r2, 400, "content_filtered")


def test_BE_037_hung_llm_is_bounded(tmp_path, record):
    """Production client settings (from the app's own AzureChatModel) against a server that never replies."""
    from chatbot.config import Settings
    from chatbot.errors import UpstreamError
    from chatbot.llm import AzureChatModel

    prod = AzureChatModel(Settings(mode="azure", openai_endpoint="https://example.openai.azure.com"))
    timeout, retries = prod.client.timeout, prod.client.max_retries

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])  # accept and never answer
            except OSError:
                pass

    threading.Thread(target=accept, daemon=True).start()
    port = srv.getsockname()[1]
    hung = AzureOpenAI(azure_endpoint=f"http://127.0.0.1:{port}", api_key="qa-not-a-real-key", api_version="2025-04-01-preview",
                       max_retries=retries, timeout=timeout)
    model = AzureChatModel(Settings(mode="azure"), client=hung)
    t0 = time.monotonic()
    try:
        with pytest.raises(UpstreamError):
            model.complete([{"role": "user", "content": "hi"}])
    finally:
        dt = time.monotonic() - t0
        stop.set()
        srv.close()
    record(actual=f"production client timeout={timeout}, max_retries={retries}; non-responding LLM -> UpstreamError (HTTP 502) after {dt:.1f}s "
                  f"({len(held)} connection attempts). Front end fetch has no timeout, so the user waits this long.")
    assert dt < 240, "request could exceed App Service's 230 s front-end timeout"


# ------------------------------------------------------------------ Azure AI Search failures
@pytest.mark.parametrize("label,handler,status,code", [
    ("search 500", lambda req: httpx.Response(500, json={"error": {"message": "boom"}}), 502, "upstream_error"),
    ("search 503", lambda req: httpx.Response(503, json={"error": {"message": "busy"}}), 503, "busy"),
    ("search 429", lambda req: httpx.Response(429, json={"error": {"message": "slow down"}}), 503, "busy"),
    ("search 403", lambda req: httpx.Response(403, json={"error": {"message": "forbidden"}}), 502, "upstream_error"),
    ("search timeout", "timeout", 502, "upstream_error"),
])
def test_BE_038_search_failures(tmp_path, record, label, handler, status, code):
    from conftest import CapturingModel
    from chatbot.config import Settings
    from chatbot.retrieval import AzureSearchRetriever
    from chatbot.search import SearchClient

    if handler == "timeout":
        def handler(req):
            raise httpx.ConnectTimeout("timed out", request=req)
    s = Settings(mode="azure", search_endpoint="https://mock.search.windows.net")
    search = SearchClient(s, lambda: "qa-token", http=httpx.Client(transport=httpx.MockTransport(handler)))
    c = make_client(tmp_path, components=components_with(CapturingModel(), tmp_path, AzureSearchRetriever(search)))
    r, dt = ask(c)
    record(actual=f"{label} -> {r.status_code} {r.text[:140]} in {dt:.2f}s")
    assert_clean_error(r, status, code)


def test_BE_065_malformed_upstream_follows_documented_contract(tmp_path, record):
    """Documents/local-development.md: '502 upstream_error – Azure OpenAI or AI Search failed'."""
    from conftest import CapturingModel
    from chatbot.config import Settings
    from chatbot.retrieval import AzureSearchRetriever
    from chatbot.search import SearchClient

    llm = client_for(tmp_path, lambda req: httpx.Response(200, json={**OK_BODY, "choices": []}))
    s = Settings(mode="azure", search_endpoint="https://mock.search.windows.net")
    search = SearchClient(s, lambda: "qa-token", http=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text="oops"))))
    srch = make_client(tmp_path, components=components_with(CapturingModel(), tmp_path, AzureSearchRetriever(search)))
    r1, _ = ask(llm)
    r2, _ = ask(srch)
    record(actual=f"LLM reply with empty choices -> {r1.status_code} {r1.json()['error']['code']}; AI Search 200 with non-JSON body -> "
                  f"{r2.status_code} {r2.json()['error']['code']} (server log shows an 'Unhandled error' traceback)")
    assert (r1.status_code, r2.status_code) == (502, 502)
