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
from chatbot.privacy import CATEGORIES, MaskingFilter, mask, parse_categories
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
