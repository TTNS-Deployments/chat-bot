import logging

import pytest

from chatbot.privacy import HIDDEN, HIDDEN_COUNT, MaskingFilter, hidden_note, mask, requested_reveals

RECORD = """Customer details:
- **Name:** Asha Rao
- Address: 12, MG Road, Bengaluru 560001 [2]
- Phone: +91 98765 43210
- Email: asha.rao@example.com
- Account number: 123456789012
Mr. Vikram Singh called on 2026-10-01. We serve 12,500 active customers."""


def test_everything_sensitive_is_masked_by_default():
    m = mask(RECORD)
    assert "Asha Rao" not in m.text and "Vikram" not in m.text and "MG Road" not in m.text
    assert "98765" not in m.text and "asha.rao@" not in m.text and "12345678" not in m.text and "12,500" not in m.text
    assert "- **Name:** [hidden]" in m.text
    assert "Address: [hidden] [2]" in m.text  # the citation marker stays
    assert "Phone: ********3210" in m.text  # last four digits only
    assert "Account number: ********9012" in m.text
    assert f"We serve {HIDDEN_COUNT} active customers." in m.text
    assert "2026-10-01" in m.text  # dates are not phone numbers
    assert m.hidden == {"name", "address", "phone", "email", "id", "count"}


def test_ordinary_content_is_untouched():
    text = ("Refunds take 5 to 7 days for customers [1]. The visit fee is 350 rupees. "
            "Phone: the toll-free number printed on the invoice. Email: replies within one working day.")
    m = mask(text)
    assert m.text == text and not m.hidden


def test_reveal_shows_only_the_requested_category():
    m = mask(RECORD, {"address"})
    assert "12, MG Road, Bengaluru 560001" in m.text
    assert "Asha Rao" not in m.text and "98765" not in m.text and "12,500" not in m.text
    assert "address" not in m.hidden


@pytest.mark.parametrize("text, hidden_value", [
    ("| Customer | Phone | City |\n|---|---|---|\n| Asha Rao | 9876543210 | Pune |", "Asha Rao"),
    ("PAN ABCDE1234F", "ABCDE1234F"),
    ("customer ID CUST-00912", "CUST-00912"),
    ("Aadhaar 1234 5678 9012", "1234 5678"),
    ("Card 4111 1111 1111 1111 expires soon", "4111 1111"),
    ("The customer's name is Priya Nair.", "Priya Nair"),
    ("The number of customers is 12,500.", "12,500"),
    ("Contact support@example.com", "support@example.com"),
])
def test_other_formats_are_masked(text, hidden_value):
    masked = mask(text).text
    assert hidden_value not in masked
    assert "[hidden" in masked or "****" in masked


def test_table_keeps_columns_that_are_not_personal():
    assert "| Pune |" in mask("| Customer | Phone | City |\n|---|---|---|\n| Asha Rao | 9876543210 | Pune |").text


def test_masking_is_idempotent():
    once = mask(RECORD).text
    assert mask(once).text == once


@pytest.mark.parametrize("message, expected", [
    ("Show the customer address", {"address"}),
    ("How many customers are there?", {"count"}),
    ("What's the email address of the account manager?", {"email"}),
    ("Give me the account number", {"id"}),
    ("Who is the account holder?", {"name"}),
    ("What is the phone number and email?", {"phone", "email"}),
    ("Summarize the warranty policy", set()),
    ("Don't show the phone number; what is the refund time?", set()),
    ("What is the file name?", set()),
    ("customer address", set()),  # not a request
])
def test_reveal_only_on_explicit_request(message, expected):
    assert requested_reveals(message) == expected


def test_hidden_note_names_what_was_hidden():
    assert hidden_note(set()) == ""
    note = hidden_note({"phone", "address"})
    assert "addresses and phone numbers" in note and "show the customer address" in note


def test_logs_are_masked(caplog):
    logger = logging.getLogger("chatbot.test-masking")
    handler = logging.Handler()
    records = []
    handler.emit = records.append
    handler.addFilter(MaskingFilter())
    logger.addHandler(handler)
    try:
        logger.warning("Lookup for %s failed", "asha.rao@example.com")
    finally:
        logger.removeHandler(handler)
    assert records[0].getMessage() == f"Lookup for {HIDDEN} failed"
