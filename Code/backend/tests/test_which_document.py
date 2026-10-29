"""Several similar documents (three BOQs): vague questions get a question back, the reply picks the
document, and the answer comes from that document only."""
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.config import Settings
from chatbot.documents import LocalDocumentStore
from chatbot.history import ConversationStore
from chatbot.main import create_app
from chatbot.prompts import SYSTEM_PROMPT, build_messages, scope_note
from chatbot.retrieval import LocalRetriever, Passage
from chatbot.scope import resolve_choice, resolve_scope
from chatbot.service import ChatService, Components

BOQS = {
    "BOQ-Mumbai-Tower-A.md": "# BOQ Mumbai Tower A\nTotal price: INR 4,12,50,000 (excluding GST). Cement rate: INR 410 per bag.",
    "BOQ-Pune-Phase-2.md": "# BOQ Pune Phase 2\nTotal price: INR 2,98,10,000 (excluding GST). Cement rate: INR 395 per bag.",
    "BOQ-Delhi-Metro.md": "# BOQ Delhi Metro\nTotal price: INR 7,05,00,000 (including GST). Cement rate: INR 420 per bag.",
}
NAMES = list(BOQS)


class Model:
    """Behaves like the instructions ask: question back when several documents are offered for one figure,
    otherwise quote the first source with its citation."""

    def __init__(self):
        self.prompts = []

    def complete(self, messages, max_tokens=None):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if "Follow WHICH DOCUMENT." in prompt and "compare" not in prompt.split("Question:")[-1].lower():
            return "CLARIFY: Which BOQ do you mean? The total price differs per project."
        first = prompt.split("[1] Document: ", 1)[1].split("\n\n", 1)[0]
        return " ".join(first.split("\n")[1:]) + " [1]"


@pytest.fixture()
def app(tmp_path):
    for name, text in BOQS.items():
        (tmp_path / name).write_text(text)
    settings = Settings(mode="local", rate_limit_per_minute=0)
    model = Model()
    components = Components(chat=ChatService(settings, LocalRetriever([tmp_path]), model),
                            documents=LocalDocumentStore(tmp_path),
                            conversations=ConversationStore(Path(tempfile.mkdtemp(prefix="chatbot-test-"))))
    return TestClient(create_app(settings, components, start_search_setup=False)), model


def ask(client, message, cid=None, history=()):
    r = client.post("/api/chat", json={"message": message, "conversationId": cid, "history": list(history)})
    assert r.status_code == 200, r.text
    return r.json()


def conversation(client, *messages):
    """Ask several messages in one conversation, sending the history like the chat page does."""
    cid, history, bodies = None, [], []
    for m in messages:
        body = ask(client, m, cid, history)
        cid = body["conversationId"]
        history += [{"role": "user", "content": m}, {"role": "assistant", "content": body["answer"]}]
        bodies.append(body)
    return bodies


def test_vague_question_over_similar_documents_asks_back(app):
    client, model = app
    body = ask(client, "What is the total price?")
    assert body["citations"] == [] and body["scope"] == []
    assert body["answer"].startswith("Which BOQ do you mean?")
    assert sorted(body["choices"]) == sorted(NAMES)
    for i, name in enumerate(body["choices"], start=1):
        assert f"{i}. {name}" in body["answer"]
    assert "Reply with the number or the name." in body["answer"]


@pytest.mark.parametrize("reply, expected", [("Pune", "BOQ-Pune-Phase-2.md"), ("the delhi one", "BOQ-Delhi-Metro.md"),
                                             ("BOQ-Mumbai-Tower-A.md", "BOQ-Mumbai-Tower-A.md")])
def test_reply_picks_the_document_and_answers_from_it_only(app, reply, expected):
    client, model = app
    _, answer = conversation(client, "What is the total price?", reply)
    assert answer["scope"] == [expected] and answer["choices"] == []
    assert {c["title"] for c in answer["citations"]} == {expected}
    assert "Total price" in answer["answer"]
    others = [n for n in NAMES if n != expected]
    assert all(o not in model.prompts[-1] for o in others)
    assert "total price" in model.prompts[-1].lower()


def test_numbered_reply_follows_the_listed_order(app):
    client, _ = app
    first, answer = conversation(client, "What is the total price?", "2")
    assert answer["scope"] == [first["choices"][1]]


def test_follow_ups_stay_on_the_chosen_document(app):
    client, model = app
    *_, follow_up = conversation(client, "What is the total price?", "Pune", "and the cement rate?")
    assert follow_up["scope"] == ["BOQ-Pune-Phase-2.md"]
    assert "395" in follow_up["answer"] and "Mumbai" not in model.prompts[-1]


def test_unclear_short_reply_asks_again_with_the_same_list(app):
    client, model = app
    first, again = conversation(client, "What is the total price?", "the boq")
    assert again["choices"] == first["choices"] and "Please choose one" in again["answer"]
    assert len(model.prompts) == 1  # no new search or model call for the re-ask


def test_a_new_full_question_is_not_taken_as_a_reply(app):
    client, model = app
    _, other = conversation(client, "What is the total price?", "What is the cement rate in the Mumbai BOQ?")
    assert other["scope"] == ["BOQ-Mumbai-Tower-A.md"] and "410" in other["answer"]


def test_naming_the_document_answers_directly(app):
    client, model = app
    body = ask(client, "What is the total price in the Pune BOQ?")
    assert body["scope"] == ["BOQ-Pune-Phase-2.md"] and body["choices"] == []
    assert "2,98,10,000" in body["answer"] and "WHICH DOCUMENT" not in model.prompts[-1]


def test_all_of_them_answers_each_document_separately(app):
    client, model = app
    _, answer = conversation(client, "What is the total price?", "all of them")
    assert sorted(answer["scope"]) == sorted(NAMES)
    assert "Answer for each of these documents separately" in model.prompts[-1]


def test_reply_matching_examples():
    choices = ["BOQ-Mumbai-Tower-A.pdf", "BOQ-Pune-Phase-2.pdf", "BOQ-Delhi-Metro.xlsx"]
    assert resolve_choice("option 3", choices) == ["BOQ-Delhi-Metro.xlsx"]
    assert resolve_choice("the second one", choices) == ["BOQ-Pune-Phase-2.pdf"]
    assert resolve_choice("last one", choices) == ["BOQ-Delhi-Metro.xlsx"]
    assert resolve_choice("the delhi one", choices) == ["BOQ-Delhi-Metro.xlsx"]
    assert resolve_choice("Phase 2", choices) == ["BOQ-Pune-Phase-2.pdf"]
    assert resolve_choice("compare all of them", choices) == choices
    assert resolve_choice("boq", choices) == [] and resolve_choice("one", choices) == []
    assert resolve_choice("what about the cement rate?", choices) == []


def test_boq_is_a_document_word():
    scope = resolve_scope("What is the total in the Mumbai BOQ?", NAMES)
    assert scope.documents == ("BOQ-Mumbai-Tower-A.md",)


def test_prompt_rules_for_precise_answers_and_asking_back():
    assert "CLARIFY: " in SYSTEM_PROMPT and "do not round" in SYSTEM_PROMPT and "1-3 sentences" in SYSTEM_PROMPT
    passages = [Passage("a", "A.md", "x"), Passage("b", "B.md", "y"), Passage("c", "A.md", "z")]
    prompt = build_messages("Total?", [], passages, 3)[-1]["content"]
    assert "These sources come from 2 documents: A.md, B.md. Follow WHICH DOCUMENT." in prompt
    single = build_messages("Total?", [], passages[:1], 3)[-1]["content"]
    assert "Scope: all documents." in single and "WHICH DOCUMENT" not in single
    assert "separately" not in scope_note(("A.md",)) and "separately" in scope_note(("A.md", "B.md"))
