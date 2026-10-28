"""Summary requests: detection, length limits in the model call and the answer, and document scope."""
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.config import Settings
from chatbot.documents import LocalDocumentStore
from chatbot.history import ConversationStore
from chatbot.llm import REASONING_ALLOWANCE, drop_unfinished
from chatbot.main import create_app
from chatbot.prompts import SYSTEM_PROMPT, build_messages
from chatbot.retrieval import LocalRetriever, Passage
from chatbot.service import ChatService, Components
from chatbot.summary import DETAILED, MORE_DETAIL, SHORT, strip_offer, summary_query, summary_request, trim_summary
from test_llm import bad_request, model, response

LONG = " ".join(f"Sentence number {i} explains one more detail of the policy at length [1]." for i in range(30))


@pytest.mark.parametrize("message, kind", [
    ("Summarize the warranty policy", SHORT),
    ("Can you give me a summary?", SHORT),
    ("give me the gist", SHORT),
    ("tl;dr", SHORT),
    ("TLDR of the returns document", SHORT),
    ("An overview of support, please", SHORT),
    ("Give me a detailed summary", DETAILED),
    ("full summary of the returns document", DETAILED),
    ("Summarize it in depth", DETAILED),
    ("What does the warranty cover?", ""),
    ("Explain in depth how refunds work", ""),
])
def test_detects_summary_requests(message, kind):
    assert summary_request(message) == kind


@pytest.mark.parametrize("message", ["expand", "Yes please", "more detail", "explain in depth"])
def test_follow_up_to_the_offer_asks_for_the_detailed_summary(message):
    history = [{"role": "user", "content": "summarize"}, {"role": "assistant", "content": f"Short. [1]\n\n{MORE_DETAIL}"}]
    assert summary_request(message, history) == DETAILED
    assert summary_request(message, history[:1] + [{"role": "assistant", "content": "An answer."}]) == ""


def test_trim_summary_keeps_whole_sentences_and_bullets():
    trimmed = trim_summary(LONG, 90)
    assert len(trimmed.split()) <= 90 and trimmed.endswith("[1].")
    bullets = "\n".join(f"- Point {i} has several words in it to count [1]." for i in range(12))
    kept = trim_summary(bullets, 40).splitlines()
    assert 3 <= len(kept) < 12 and all(line.startswith("- Point") for line in kept)
    assert trim_summary("Short answer [1].", 90) == "Short answer [1]."
    assert strip_offer(f"Short.\n\n{MORE_DETAIL}") == "Short."


def test_summary_query_falls_back_to_the_document_names():
    assert summary_query("Give me a quick summary", ["warranty-policy.md"]) == "warranty policy"
    assert summary_query("tl;dr", ["a.md", "b_notes.txt"]) == "a b notes"
    assert summary_query("Summarize the refund rules", ["warranty-policy.md"]) == "Summarize the refund rules"
    assert summary_query("summarize", []) == "summarize"


def test_drop_unfinished_cuts_at_the_last_sentence():
    assert drop_unfinished("First point [1]. Second point is cut of") == "First point [1]."
    assert drop_unfinished("- one [1]\n- two is cu") == "- one [1]"


def test_prompt_states_the_summary_rules():
    assert "SUMMARIES" in SYSTEM_PROMPT and "60-80 words" in SYSTEM_PROMPT and "Do not mix documents" in SYSTEM_PROMPT
    passages = [Passage("1", "a.md", "x")]
    short = build_messages("summarize", [], passages, 3, ("a.md",), summary=SHORT)[-1]["content"]
    assert "Summary: short" in short and "60-80 words" in short and "No introduction" in short
    several = build_messages("summarize", [], passages, 3, summary=SHORT, documents=3)[-1]["content"]
    assert "one line per document" in several
    detailed = build_messages("summarize fully", [], passages, 3, summary=DETAILED)[-1]["content"]
    assert "Summary: detailed" in detailed
    assert "Summary:" not in build_messages("what?", [], passages, 3)[-1]["content"]


def test_capped_call_on_a_reasoning_model():
    m, calls = model(response("Short [1]."), response("Full"))
    assert m.complete([], max_tokens=150) == "Short [1]."
    assert calls.calls[0]["max_completion_tokens"] == 150 + REASONING_ALLOWANCE
    assert calls.calls[0]["reasoning_effort"] == "minimal"
    m.complete([])
    assert calls.calls[1]["max_completion_tokens"] == 2000 and calls.calls[1]["reasoning_effort"] == "low"


def test_capped_call_on_other_models_and_fallbacks():
    m, calls = model(response("Short [1]. And then", finish="length"), chat_reasoning_effort="none")
    assert m.complete([], max_tokens=150) == "Short [1]."
    assert calls.calls[0]["max_completion_tokens"] == 150 and "reasoning_effort" not in calls.calls[0]
    m, calls = model(bad_request("Unsupported value: 'reasoning_effort' does not support 'minimal'"), response("ok"),
                     response("again"))
    assert m.complete([], max_tokens=150) == "ok"
    assert calls.calls[1]["reasoning_effort"] == "low"
    m.complete([], max_tokens=150)
    assert calls.calls[2]["reasoning_effort"] == "low"  # remembered


# ---- through the API ----
DOCS = {
    "warranty-policy.md": "# Warranty\nThe warranty lasts two years. Contact: Asha Rao, phone 98765 43210.",
    "returns-and-refunds.md": "# Returns\nReturns are accepted within 30 days. Refunds take 5 days.",
    "support-hours.md": "# Support\nSupport is open 9 to 6 on weekdays.",
}


class Model:
    def __init__(self, reply=LONG):
        self.reply, self.calls = reply, []

    def complete(self, messages, max_tokens=None):
        self.calls.append({"prompt": messages[-1]["content"], "max_tokens": max_tokens})
        return self.reply


def client_for(tmp_path, docs=DOCS, reply=LONG):
    for name, text in docs.items():
        (tmp_path / name).write_text(text)
    settings = Settings(mode="local", rate_limit_per_minute=0)
    retriever, chat_model = LocalRetriever([tmp_path]), Model(reply)
    components = Components(chat=ChatService(settings, retriever, chat_model), documents=LocalDocumentStore(tmp_path),
                            conversations=ConversationStore(Path(tempfile.mkdtemp(prefix="chatbot-test-"))))
    return TestClient(create_app(settings, components, start_search_setup=False)), chat_model


def ask(client, message, **extra):
    r = client.post("/api/chat", json={"message": message, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_short_summary_of_a_named_document(tmp_path):
    client, chat_model = client_for(tmp_path)
    body = ask(client, "Summarize the warranty policy")
    assert body["scope"] == ["warranty-policy.md"]
    assert chat_model.calls[-1]["max_tokens"] == 150
    text, _, offer = body["answer"].rpartition("\n\n")
    assert offer == MORE_DETAIL and body["answer"].count(MORE_DETAIL) == 1
    assert len(text.split()) <= 90
    assert "Document: warranty-policy.md" in chat_model.calls[-1]["prompt"]
    assert "returns-and-refunds.md" not in chat_model.calls[-1]["prompt"]


def test_detailed_summary_is_not_capped(tmp_path):
    client, chat_model = client_for(tmp_path)
    body = ask(client, "Give me a detailed summary of the warranty policy")
    assert chat_model.calls[-1]["max_tokens"] is None and "Summary: detailed" in chat_model.calls[-1]["prompt"]
    assert MORE_DETAIL not in body["answer"] and len(body["answer"].split()) > 90


def test_expand_after_a_short_summary(tmp_path):
    client, chat_model = client_for(tmp_path)
    first = ask(client, "Summarize the warranty policy")
    history = [{"role": "user", "content": "Summarize the warranty policy"},
               {"role": "assistant", "content": first["answer"]}]
    ask(client, "expand", history=history, conversationId=first["conversationId"])
    assert chat_model.calls[-1]["max_tokens"] is None and "Summary: detailed" in chat_model.calls[-1]["prompt"]


def test_unscoped_summary_covers_each_document_separately(tmp_path):
    client, chat_model = client_for(tmp_path)
    body = ask(client, "Give me a summary")
    prompt = chat_model.calls[-1]["prompt"]
    assert body["scope"] == []
    assert "one line per document" in prompt and "3 documents" in prompt
    assert all(f"Document: {name}" in prompt for name in DOCS)
    assert chat_model.calls[-1]["max_tokens"] == 150 + 2 * 40


def test_unscoped_summary_with_many_documents_asks_which_one(tmp_path):
    docs = {f"doc-{i}.md": f"# Doc {i}\nText {i}." for i in range(6)}
    client, chat_model = client_for(tmp_path, docs)
    body = ask(client, "summarize")
    assert body["answer"].startswith("Which document should I summarize?") and "doc-5.md" in body["answer"]
    assert chat_model.calls == []


def test_summary_keeps_personal_information_hidden(tmp_path):
    client, _ = client_for(tmp_path, reply="The warranty lasts two years. Contact person: Asha Rao. Phone: 98765 43210 [1].")
    body = ask(client, "Summarize the warranty policy")
    assert "98765 43210" not in body["answer"] and "Asha Rao" not in body["answer"]
    shown = ask(client, "Summarize the warranty policy and show the phone number")
    assert "98765 43210" in shown["answer"] and "Asha Rao" not in shown["answer"]


def test_ordinary_questions_are_not_capped(tmp_path):
    client, chat_model = client_for(tmp_path, reply="Two years [1].")
    body = ask(client, "How long is the warranty?")
    assert chat_model.calls[-1]["max_tokens"] is None and MORE_DETAIL not in body["answer"]
