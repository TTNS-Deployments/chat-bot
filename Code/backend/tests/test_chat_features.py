"""End to end through the API: document scoping, masking with explicit reveal, and history export."""
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.config import Settings
from chatbot.documents import LocalDocumentStore
from chatbot.history import ConversationStore
from chatbot.main import create_app
from chatbot.retrieval import LocalRetriever
from chatbot.service import ChatService, Components

DOCS = {
    "q1-statement.md": "# Q1 statement\n## Accounts\nThe refund rate in Q1 was 4 percent. Account manager: Asha Rao. "
                       "Phone: 98765 43210. We served 12,500 active customers in Q1.",
    "q2-statement.md": "# Q2 statement\n## Accounts\nThe refund rate in Q2 was 9 percent. Customer address: "
                       "12, MG Road, Bengaluru 560001.",
}


class Recorder:
    """Repeats the sources back with their citation numbers, as a model that leaks everything would."""

    def __init__(self):
        self.prompts = []

    def complete(self, messages):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        sources = prompt.split("\n\nScope: ")[0].split("\n\n[")
        return " ".join(f"{part.split(chr(10), 1)[1]} [{n}]" for n, part in enumerate(sources, start=1) if "\n" in part)


@pytest.fixture()
def app(tmp_path):
    for name, text in DOCS.items():
        (tmp_path / name).write_text(text)
    settings = Settings(mode="local", rate_limit_per_minute=0)
    retriever, model = LocalRetriever([tmp_path]), Recorder()
    components = Components(chat=ChatService(settings, retriever, model), documents=LocalDocumentStore(tmp_path),
                            conversations=ConversationStore(Path(tempfile.mkdtemp(prefix="chatbot-test-"))))
    return TestClient(create_app(settings, components, start_search_setup=False)), model


def ask(client, message, cid=None, **extra):
    r = client.post("/api/chat", json={"message": message, "conversationId": cid, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_scoped_search_returns_only_the_target_document(app):
    client, model = app
    body = ask(client, "Only in the Q2 statement: what was the refund rate?")
    assert body["scope"] == ["q2-statement.md"]
    assert {c["title"] for c in body["citations"]} == {"q2-statement.md"}
    assert "q1-statement" not in model.prompts[-1] and "4 percent" not in body["answer"]
    assert "Scope: only q2-statement.md" in model.prompts[-1]


def test_scope_persists_on_follow_ups_and_resets_on_request(app):
    client, model = app
    cid = ask(client, "Only in the Q2 statement: what was the refund rate?")["conversationId"]
    follow_up = ask(client, "and the refund rate trend?", cid)
    assert follow_up["scope"] == ["q2-statement.md"] and "q1-statement" not in model.prompts[-1]
    widened = ask(client, "Search all documents: what was the refund rate?", cid)
    assert widened["scope"] == [] and {c["title"] for c in widened["citations"]} == {"q1-statement.md", "q2-statement.md"}


def test_scoped_document_without_the_answer_says_so_without_fallback(app):
    client, model = app
    body = ask(client, "Warranty period?", documents=["q2-statement.md"])
    assert body["citations"] == [] and "could not find this in q2-statement.md" in body["answer"]
    assert "search all documents" in body["answer"] and model.prompts == []


def test_unclear_document_asks_instead_of_answering(app):
    client, model = app
    body = ask(client, "Only in the statement document: what was the refund rate?")
    assert "Which one do you mean?" in body["answer"] and model.prompts == []
    body = ask(client, "Only from the budget file: what was the refund rate?")
    assert "could not find a document called" in body["answer"]


def test_page_selection_scopes_the_search(app):
    client, _ = app
    body = ask(client, "What was the refund rate?", documents=["q1-statement.md"])
    assert body["scope"] == ["q1-statement.md"] and {c["title"] for c in body["citations"]} == {"q1-statement.md"}
    assert client.get("/api/document-names").json() == {"documents": ["q1-statement.md", "q2-statement.md"]}


def test_unscoped_search_labels_every_source(app):
    client, model = app
    body = ask(client, "What was the refund rate?")
    assert body["scope"] == []
    assert "[1] Document: " in model.prompts[-1] and "[2] Document: " in model.prompts[-1]
    assert "Scope: all documents. Say which document each fact comes from." in model.prompts[-1]
    assert [(c["title"], c["section"]) for c in body["citations"]] == [
        ("q1-statement.md", "Q1 statement"), ("q2-statement.md", "Q2 statement")]


def test_masking_is_on_by_default(app):
    client, model = app
    body = ask(client, "What was the refund rate in the Q1 and Q2 statements, all documents?")
    answer = body["answer"]
    for secret in ("Asha Rao", "98765 43210", "12,500", "MG Road"):
        assert secret not in answer
        assert all(secret not in c["snippet"] for c in body["citations"])
    assert "******3210" in answer and "[hidden" in answer
    assert "4 percent" in answer and "9 percent" in answer  # the rest of the answer is normal
    assert "is hidden. Ask for a specific item" in answer
    assert "Show on request: nothing" in model.prompts[-1]


def test_reveal_only_what_was_asked_and_only_for_that_request(app):
    client, model = app
    cid = ask(client, "Search all documents. What is the customer address?")["conversationId"]
    revealed = ask(client, "Show the customer address from all documents", cid)
    assert "12, MG Road, Bengaluru 560001" in revealed["answer"]
    assert "Asha Rao" not in revealed["answer"] and "98765 43210" not in revealed["answer"]
    assert "Show on request: addresses." in model.prompts[-1]
    later = ask(client, "Summarize all documents for me", cid)
    assert "MG Road" not in later["answer"]


def test_reveal_can_be_turned_off(tmp_path):
    (tmp_path / "a.md").write_text("Customer address: 12, MG Road, Bengaluru 560001.")
    settings = Settings(mode="local", rate_limit_per_minute=0, allow_sensitive_reveal=False)
    components = Components(chat=ChatService(settings, LocalRetriever([tmp_path]), Recorder()),
                            documents=LocalDocumentStore(tmp_path), conversations=ConversationStore(tmp_path / "c"))
    client = TestClient(create_app(settings, components, start_search_setup=False))
    body = ask(client, "Show the customer address")
    assert "MG Road" not in body["answer"] and "turned off" in body["answer"]


def test_export_button_and_chat_command_respect_masking(app):
    client, _ = app
    cid = ask(client, "Show the customer address from all documents")["conversationId"]
    ask(client, "What was the refund rate across all documents?", cid)
    headers = {"X-Conversation-Id": cid}
    for fmt in ("txt", "json", "docx"):
        r = client.get(f"/api/history/export?format={fmt}", headers=headers)
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    text = client.get("/api/history/export?format=txt", headers=headers).text
    assert "12, MG Road, Bengaluru 560001" in text  # explicitly revealed in this conversation
    assert "Asha Rao" not in text and "98765 43210" not in text and "12,500" not in text
    assert text.count("12, MG Road") == 1  # the second answer masked it again

    command = ask(client, "download chat history as json", cid)
    assert command["action"] == {"type": "download", "format": "json"} and command["citations"] == []
    data = client.get("/api/history", headers=headers).json()
    assert data["messages"][-2]["content"] == "download chat history as json"


def test_history_is_never_exported_without_a_request(app):
    client, _ = app
    body = ask(client, "What was the refund rate?")
    assert body["action"] is None


def test_history_endpoints_validate_input(app):
    client, _ = app
    assert client.get("/api/history/export").status_code == 404
    assert client.get("/api/history/export", headers={"X-Conversation-Id": "../../x"}).status_code == 404
    cid = ask(client, "What was the refund rate?")["conversationId"]
    assert client.get("/api/history/export?format=exe", headers={"X-Conversation-Id": cid}).status_code == 422
    assert client.delete("/api/history", headers={"X-Conversation-Id": cid}).json() == {"deleted": True}
    assert client.get("/api/history", headers={"X-Conversation-Id": cid}).status_code == 404
