"""REDACTED_CATEGORIES: which kinds of personal information are hidden is customizable."""
import json
import logging
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot import privacy
from chatbot.config import Settings, settings_from_env
from chatbot.documents import LocalDocumentStore
from chatbot.history import ConversationStore
from chatbot.main import create_app
from chatbot.history import export
from chatbot.privacy import CATEGORIES, MaskingFilter, hidden_note, mask, parse_categories, parse_never_show
from chatbot.prompts import privacy_note
from chatbot.retrieval import LocalRetriever
from chatbot.service import ChatService, Components

DEMO = ("name", "address", "phone", "id", "count")
TEXT = "Contact asha.rao@example.com or call +91 98765 43210."


def test_parse_categories():
    assert parse_categories(None) == CATEGORIES and parse_categories("  ") == CATEGORIES
    assert parse_categories("none") == ()
    assert parse_categories("phone, EMAIL") == ("phone", "email")
    assert parse_categories("phone,bogus") == ("phone",)
    assert parse_categories("bogus") == CATEGORIES  # a typo must not switch hiding off
    assert settings_from_env({"REDACTED_CATEGORIES": "phone"}).redacted_categories == ("phone",)
    assert settings_from_env({}).redacted_categories == CATEGORIES


def test_default_hides_email_and_phone():
    m = mask(TEXT)
    assert "asha.rao" not in m.text and "98765" not in m.text and m.hidden == {"email", "phone"}


def test_email_shown_and_phone_hidden_when_configured():
    privacy.configure(DEMO)
    m = mask(TEXT)
    assert "asha.rao@example.com" in m.text and "98765" not in m.text and "******3210" in m.text
    assert m.hidden == {"phone"}


@pytest.mark.parametrize("email", ["9876543210@example.com", "cust12345@example.com", "ID4567@example.com"])
def test_a_visible_email_is_not_damaged_by_the_number_rules(email):
    privacy.configure(DEMO)
    assert mask(f"Write to {email} today.").text == f"Write to {email} today."


def test_email_column_in_a_table_stays_visible():
    privacy.configure(DEMO)
    text = mask("| Email | Phone |\n|---|---|\n| a.b@example.com | 9876543210 |").text
    assert "a.b@example.com" in text and "9876543210" not in text


def test_every_category_can_be_turned_off():
    privacy.configure(())
    assert mask(TEXT).text == TEXT


def test_logs_stay_strict_whatever_the_setting():
    privacy.configure(())
    record = logging.LogRecord("x", logging.INFO, "f", 1, TEXT, None, None)
    MaskingFilter().filter(record)
    assert "asha.rao" not in record.getMessage() and "98765" not in record.getMessage()


def test_model_is_told_the_same_list():
    privacy.configure(DEMO)
    note = privacy_note(())
    assert "phone numbers" in note and "email" not in note
    assert "email" not in privacy_note({"phone"}).split("Hide:")[1] and "Show on request: phone numbers." in privacy_note({"phone"})
    privacy.configure(())
    assert "Hide: nothing." in privacy_note(())


class Echo:
    def __init__(self):
        self.prompts = []

    def complete(self, messages, max_tokens=None):
        self.prompts.append(messages[-1]["content"])
        return f"{TEXT} [1]"


def test_end_to_end_with_the_demo_setting(tmp_path):
    (tmp_path / "contacts.md").write_text(f"# Contacts\n{TEXT}")
    settings = Settings(mode="local", rate_limit_per_minute=0, redacted_categories=DEMO, allow_sensitive_reveal=False)
    model = Echo()
    store = ConversationStore(Path(tempfile.mkdtemp(prefix="chatbot-test-")))
    client = TestClient(create_app(settings, Components(chat=ChatService(settings, LocalRetriever([tmp_path]), model),
                                                        documents=LocalDocumentStore(tmp_path), conversations=store),
                                   start_search_setup=False))
    body = client.post("/api/chat", json={"message": "What is the contact email?"}).json()
    assert "asha.rao@example.com" in body["answer"] and "98765" not in body["answer"]
    assert "phone numbers" in body["answer"]  # the note says what was hidden
    assert "email addresses" not in body["answer"]
    assert "turned off" not in body["answer"]  # email needs no reveal, so no "reveal is off" note
    assert "asha.rao@example.com" in body["citations"][0]["snippet"]
    stored = json.dumps(store.load(body["conversationId"]))
    assert "asha.rao@example.com" in stored and "98765" not in stored


# ---- phone numbers strictly not visible (NEVER_SHOW_CATEGORIES) ------------------------------
STRICT = {"categories": DEMO, "never_show": ("phone",)}
PHONES = ["+91 98765 43210", "9876543210", "(022) 2345-6789", "1800-123-4567", "98765-43210"]


def test_parse_never_show():
    assert parse_never_show(None) == () and parse_never_show("none") == () and parse_never_show("") == ()
    assert parse_never_show("PHONE, bogus") == ("phone",)
    assert settings_from_env({"NEVER_SHOW_CATEGORIES": "phone"}).never_show_categories == ("phone",)


@pytest.mark.parametrize("phone", PHONES)
def test_never_show_hides_the_whole_number_even_when_revealed(phone):
    privacy.configure(**STRICT)
    digits = "".join(c for c in phone if c.isdigit())
    for reveal in (frozenset(), frozenset({"phone"})):
        text = mask(f"Call {phone} today.", reveal).text
        assert "[hidden]" in text and digits[-4:] not in text, text


def test_never_show_wins_over_the_redaction_list_and_the_caller():
    privacy.configure(("email",), never_show=("phone",))
    assert "98765" not in mask(TEXT).text
    assert "98765" not in mask(TEXT, categories=()).text


def test_labelled_short_numbers_are_hidden_but_prose_is_not():
    privacy.configure(**STRICT)
    assert "555-1234" not in mask("Phone: 555-1234").text
    assert "4321" not in mask("Mobile no: 54321").text
    assert "| 555 1234 |" not in mask("| Phone |\n|---|\n| 555 1234 |").text
    prose = "Phone: the toll-free number printed on the invoice."
    assert mask(prose).text == prose


def test_a_phone_like_number_next_to_account_stays_hidden_when_ids_are_revealed():
    privacy.configure(**STRICT)
    assert "9876543210" not in mask("Account manager on 9876543210", frozenset({"id"})).text


def test_note_does_not_invite_asking_for_a_never_show_item():
    privacy.configure(**STRICT)
    assert hidden_note({"phone"}) == "Some personal or customer information (phone numbers) is hidden and cannot be shown."
    note = hidden_note({"phone", "address"})
    assert "show the customer address" in note and "show the phone" not in note and "Phone numbers cannot be shown" in note


def test_model_is_told_never_to_show():
    privacy.configure(**STRICT)
    assert "Never show, even if asked: phone numbers." in privacy_note(())


def strict_app(tmp_path, model):
    (tmp_path / "contacts.md").write_text(f"# Contacts\nSupport desk. {TEXT}")
    settings = Settings(mode="local", rate_limit_per_minute=0, redacted_categories=DEMO, never_show_categories=("phone",))
    store = ConversationStore(Path(tempfile.mkdtemp(prefix="chatbot-test-")))
    client = TestClient(create_app(settings, Components(chat=ChatService(settings, LocalRetriever([tmp_path]), model),
                                                        documents=LocalDocumentStore(tmp_path), conversations=store),
                                   start_search_setup=False))
    return client, store


@pytest.mark.parametrize("question", ["What is the phone number?", "Show the phone number of the support desk",
                                      "Please reveal the mobile number", "Give me the contact number and email"])
def test_asking_never_reveals_and_the_model_never_receives_it(tmp_path, question):
    model = Echo()  # a model that leaks: it repeats the number whatever it is told
    client, store = strict_app(tmp_path, model)
    body = client.post("/api/chat", json={"message": question}).json()
    assert "98765" not in body["answer"] and "3210" not in body["answer"]
    assert "cannot be shown" in body["answer"] and "show the phone" not in body["answer"].lower()
    assert "98765" not in model.prompts[-1] and "3210" not in model.prompts[-1]
    assert all("98765" not in c["snippet"] and "3210" not in c["snippet"] for c in body["citations"])
    stored = json.dumps(store.load(body["conversationId"]))
    assert "98765" not in stored and "3210" not in stored


def test_downloads_never_contain_it_even_from_an_older_revealed_message(tmp_path):
    client, store = strict_app(tmp_path, Echo())
    store.append("a" * 32, [{"role": "assistant", "content": "x"}], [])  # the store exists
    conversation = store.load("a" * 32)
    conversation["messages"][0].update(content="Call +91 98765 43210", revealed=["phone"])
    store._write("a" * 32, conversation)
    for fmt in ("txt", "json"):
        data, _, _ = export(store.load("a" * 32), fmt)
        assert b"98765" not in data and b"3210" not in data


# ---- amounts and codes are not phone numbers --------------------------------------------------
@pytest.mark.parametrize("text", [
    "Total price: INR 41250000", "Grand total Rs. 298100000.00", "Amount: ₹ 70500000", "Total 12345678.50",
    "Total amount 1234567890", "Item code 10020030", "BOQ ref 2024-001-0045", "Quantity 12500000 kg",
    "Tender no. 45009871234", "4,12,50,000", "$ 1,250,000.00", "Invoice 9876543210", "The rate is 12345678",
    "Payable: INR 9876543210", "1234567890 nos",
])
def test_amounts_and_codes_stay_visible(text):
    privacy.configure(**STRICT)
    assert mask(text).text == text


def test_amount_columns_stay_visible_but_phone_columns_do_not():
    privacy.configure(**STRICT)
    table = "| Item | Qty | Rate | Amount |\n|---|---|---|---|\n| Cement | 1200 | 410 | 9876543210 |"
    assert mask(table).text == table
    text = mask("| Vendor | Phone |\n|---|---|\n| Acme | 9876543210 |").text
    assert "9876543210" not in text and "Acme" in text
    unlabelled = mask("| Vendor | Reach |\n|---|---|\n| Acme | 9876543210 |").text
    assert "9876543210" not in unlabelled


@pytest.mark.parametrize("text", [
    "Call 9876543210 for the total", "Rate enquiries: 9876543210", "Helpline for price queries 9876543210",
    "Contact: 22345678", "Phone 23456789", "Call 555-1234", "Vendor mobile no. 98765 43210", "+91 22 2345 6789",
    "Sales desk 98765-43210", "9876543210",
])
def test_phone_numbers_near_business_words_stay_hidden(text):
    privacy.configure(**STRICT)
    masked = mask(text).text
    digits = "".join(c for c in text if c.isdigit())
    assert digits[-4:] not in masked, masked
