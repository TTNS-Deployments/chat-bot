"""When the bot asks back, how it words that, and what happens when it should not ask."""
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from chatbot.config import Settings
from chatbot.documents import LocalDocumentStore
from chatbot.history import ConversationStore
from chatbot.main import create_app
from chatbot.prompts import SYSTEM_PROMPT, is_not_found, parse_clarification, select_passages, strip_citations
from chatbot.retrieval import Passage, diversify
from chatbot.service import ChatService, Components


class Retriever:
    """Returns the passages of the requested documents, best first."""

    def __init__(self, passages):
        self.passages = passages
        self.requests = []

    def document_names(self):
        return list(dict.fromkeys(p.title for p in self.passages))

    def retrieve(self, query, top, documents=()):
        self.requests.append((query, top, tuple(documents)))
        return [p for p in self.passages if not documents or p.title in documents][:top]


class Scripted:
    """Replies with each scripted text in turn, and records the prompts."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, messages, max_tokens=None):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0)


MISHRA = [Passage(f"m{i}", "Azure IAAS _Mishra_BOQ.xlsx", f"Mishra line {i}. Total INR 100{i}") for i in range(8)]
LUPIN = [Passage("l0", "Lupin Project Plan.xlsx", "Lupin BOQ. Total INR 200")]
POLICY = [Passage("p0", "warranty-policy.md", "The warranty lasts two years.")]


def app(passages, *replies):
    settings = Settings(mode="local", rate_limit_per_minute=0)
    retriever, model = Retriever(passages), Scripted(*replies)
    components = Components(chat=ChatService(settings, retriever, model), documents=LocalDocumentStore(Path("/nonexistent")),
                            conversations=ConversationStore(Path(tempfile.mkdtemp(prefix="chatbot-test-"))))
    return TestClient(create_app(settings, components, start_search_setup=False)), retriever, model


def talk(client, *messages):
    cid, history, out = None, [], []
    for m in messages:
        r = client.post("/api/chat", json={"message": m, "conversationId": cid, "history": history})
        assert r.status_code == 200, r.text
        body = r.json()
        cid = body["conversationId"]
        history += [{"role": "user", "content": m}, {"role": "assistant", "content": body["answer"]}]
        out.append(body)
    return out


ASK = "CLARIFY: Which company's BOQ do you mean? [1]\n- Mishra [1]\n- Lupin [2]"


def test_options_are_named_by_company_not_file_name():
    client, _, _ = app(LUPIN + MISHRA, "CLARIFY: Which company's BOQ do you mean?\n- Lupin [1]\n- Mishra [2]")
    body = talk(client, "What is the total price?")[0]
    assert body["answer"].startswith("Which company's BOQ do you mean?\n\n1. Lupin\n2. Mishra")
    assert ".xlsx" not in body["answer"] and "[1]" not in body["answer"]
    assert body["citations"] == [] and body["choices"] == ["Lupin Project Plan.xlsx", "Azure IAAS _Mishra_BOQ.xlsx"]


def test_reply_with_the_company_name_answers_from_that_document_only():
    client, retriever, model = app(LUPIN + MISHRA, "CLARIFY: Which company?\n- Lupin [1]\n- Mishra [2]", "INR 1000 [1]")
    _, answer = talk(client, "What is the total price?", "mishra")
    assert answer["scope"] == ["Azure IAAS _Mishra_BOQ.xlsx"] and answer["answer"].startswith("INR 1000")
    assert "Lupin" not in model.prompts[-1]
    assert retriever.requests[-1][2] == ("Azure IAAS _Mishra_BOQ.xlsx",) and "total price" in retriever.requests[-1][0]


def test_number_and_unknown_replies():
    client, _, _ = app(LUPIN + MISHRA, "CLARIFY: Which company?\n- Lupin [1]\n- Mishra [2]", "INR 200 [1]")
    first, again, answer = talk(client, "What is the total price?", "acme", "1")
    assert "Please choose one" in again["answer"] and "1. Lupin" in again["answer"]
    assert answer["scope"] == ["Lupin Project Plan.xlsx"]


def test_several_boqs_inside_one_file_stay_on_that_file():
    client, _, model = app(MISHRA + POLICY, "CLARIFY: Which BOQ do you mean?\n- Mishra_BOQ1 [1]\n- Mishra_BOQ2 [3]", "INR 1000 [1]")
    first, answer = talk(client, "What is the total price?", "Mishra_BOQ1")
    assert "1. Mishra_BOQ1\n2. Mishra_BOQ2" in first["answer"] and first["choices"] == []
    assert first["scope"] == ["Azure IAAS _Mishra_BOQ.xlsx"]
    assert answer["scope"] == ["Azure IAAS _Mishra_BOQ.xlsx"] and "warranty" not in model.prompts[-1]


def test_a_clear_question_is_answered_without_asking():
    client, _, _ = app(POLICY + MISHRA, "Two years [1].")
    body = talk(client, "How long is the warranty?")[0]
    assert body["answer"].startswith("Two years") and body["choices"] == [] and body["scope"] == []


def test_every_competing_document_reaches_the_model():
    client, _, model = app(MISHRA + LUPIN, "CLARIFY: Which company?\n- Mishra [1]\n- Lupin [2]")
    talk(client, "What is the total price?")
    assert "Lupin Project Plan.xlsx" in model.prompts[0] and "Azure IAAS _Mishra_BOQ.xlsx" in model.prompts[0]


def test_diversify():
    a = [Passage(f"a{i}", "A", "x") for i in range(10)]
    b, c = Passage("b", "B", "x"), Passage("c", "C", "x")
    chosen = diversify(a + [b, c], 5)
    assert [p.id for p in chosen] == ["a0", "b", "c", "a1", "a2"]
    assert [p.id for p in diversify(a, 3)] == ["a0", "a1", "a2"]
    assert len(diversify([Passage(str(i), str(i), "x") for i in range(9)], 5)) <= 5


def test_not_found_answers_cite_nothing():
    client, _, _ = app(POLICY, "I could not find this in the available documents [1].")
    body = talk(client, "What is the capital of France?")[0]
    assert body["citations"] == [] and "[1]" not in body["answer"] and is_not_found(body["answer"])


def test_an_earlier_choice_does_not_trap_a_new_topic():
    client, retriever, model = app(LUPIN + MISHRA + POLICY, "CLARIFY: Which company?\n- Lupin [1]\n- Mishra [2]",
                                   "INR 200 [1]", "I could not find this.", "Two years [1].")
    *_, later = talk(client, "What is the total price?", "lupin", "How long is the warranty on appliances?")
    assert later["answer"].startswith("Two years") and later["scope"] == []
    assert retriever.requests[-1][2] == ()


def test_an_explicitly_named_document_never_falls_back():
    client, retriever, _ = app(LUPIN + POLICY, "I could not find this in Lupin.")
    body = talk(client, "Only in the Lupin project plan: how long is the warranty?")[0]
    assert body["scope"] == ["Lupin Project Plan.xlsx"] and retriever.requests[-1][2] == ("Lupin Project Plan.xlsx",)
    assert len(retriever.requests) == 1


def test_excel_currency_format_is_cleaned():
    text = select_passages([Passage("a", "A", "Total [$₹]14,894,060.09 and [$USD-409]5.00")], 1000)[0].content
    assert text == "Total ₹14,894,060.09 and USD5.00"


def test_parse_clarification():
    assert parse_clarification("CLARIFY: Which? [1]\n- Mishra [1]\n* Lupin [3]\nnoise") == ("Which?", [("Mishra", 1), ("Lupin", 3)])
    assert parse_clarification("CLARIFY:") == ("", [])
    assert strip_citations("Which [1][2]?") == "Which?"


def test_prompt_says_not_to_ask_needlessly():
    assert "Never ask which document just because" in SYSTEM_PROMPT
    assert "Users do not know document names" in SYSTEM_PROMPT and "by company or project" in SYSTEM_PROMPT
