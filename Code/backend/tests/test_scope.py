import pytest

from chatbot.scope import match_reference, resolve_scope

DOCS = ["customer-support.md", "returns-and-refunds.md", "warranty-policy.md",
        "Bank Statement March.pdf", "Bank Statement April.pdf"]


@pytest.mark.parametrize("message, expected", [
    ("What does the warranty cover? Only search in the warranty policy.", ("warranty-policy.md",)),
    ("Specifically from the returns document, how long do refunds take?", ("returns-and-refunds.md",)),
    ("In warranty-policy.md, what is covered?", ("warranty-policy.md",)),
    ("What is the pickup fee in the returns-and-refunds file?", ("returns-and-refunds.md",)),
    ("Only in the March statement, what was the closing balance?", ("Bank Statement March.pdf",)),
    ("According to the warranty policy, are seals covered?", ("warranty-policy.md",)),
    ("Only use the warranty and returns files", ("warranty-policy.md", "returns-and-refunds.md")),
    ("Look only at the document called warranty policy.", ("warranty-policy.md",)),
])
def test_message_names_the_document(message, expected):
    scope = resolve_scope(message, DOCS)
    assert scope.documents == expected and scope.source == "message" and not scope.clarify


@pytest.mark.parametrize("message", [
    "When is customer support available?",  # a topic, not a document
    "How do I return a product?",
    "According to the manager, are seals covered?",  # weak cue, no matching document
    "Only answer briefly: what is the fee?",
])
def test_no_scope_without_a_document_reference(message):
    scope = resolve_scope(message, DOCS)
    assert scope.is_all and not scope.clarify


def test_ambiguous_reference_asks_which_document():
    scope = resolve_scope("Check the bank statement only", DOCS)
    assert scope.is_all and "Bank Statement March.pdf" in scope.clarify and "Bank Statement April.pdf" in scope.clarify
    assert "Which one do you mean?" in scope.clarify and "1. Bank Statement March.pdf" in scope.clarify
    assert scope.choices == ("Bank Statement March.pdf", "Bank Statement April.pdf")


def test_unknown_document_asks_and_lists_documents():
    scope = resolve_scope("Only from the pricing document, what is the fee?", DOCS)
    assert scope.is_all and "pricing" in scope.clarify and "warranty-policy.md" in scope.clarify


def test_this_document_without_a_selection_asks():
    assert "Which document" in resolve_scope("What is in this document?", DOCS).clarify


def test_this_file_uses_the_selection_or_the_current_scope():
    assert resolve_scope("just this file: what is the fee?", DOCS, selected=["customer-support.md"]).documents == (
        "customer-support.md",)
    assert resolve_scope("and in this document?", DOCS, previous=["warranty-policy.md"]).documents == ("warranty-policy.md",)


def test_scope_persists_for_follow_ups_until_changed():
    first = resolve_scope("Only in the warranty policy: what is covered?", DOCS)
    follow_up = resolve_scope("and seals?", DOCS, previous=first.documents)
    assert follow_up.documents == ("warranty-policy.md",) and follow_up.source == "previous"
    switched = resolve_scope("Now only in the returns document: pickup fee?", DOCS, previous=follow_up.documents)
    assert switched.documents == ("returns-and-refunds.md",)
    widened = resolve_scope("Search all documents for the pickup fee", DOCS, previous=switched.documents)
    assert widened.is_all and widened.source == "reset"


def test_page_selection():
    assert resolve_scope("pickup fee?", DOCS, selected=["returns-and-refunds.md"]).documents == ("returns-and-refunds.md",)
    # The page says "All documents" explicitly: the previous scope no longer applies.
    assert resolve_scope("pickup fee?", DOCS, selected=[], previous=["warranty-policy.md"]).is_all
    # A name in the message wins over the page selection.
    assert resolve_scope("only in warranty-policy.md: seals?", DOCS, selected=["customer-support.md"]).documents == (
        "warranty-policy.md",)
    assert "not available" in resolve_scope("fee?", DOCS, selected=["deleted.pdf"]).clarify


def test_deleted_document_in_previous_scope_asks():
    assert "not available" in resolve_scope("fee?", DOCS, previous=["deleted.pdf"]).clarify


def test_match_reference():
    assert match_reference("refund", DOCS) == ["returns-and-refunds.md"]
    assert match_reference("warranty-policy.md", DOCS) == ["warranty-policy.md"]
    assert match_reference("bank statement", DOCS) == ["Bank Statement March.pdf", "Bank Statement April.pdf"]
    assert match_reference("pricing", DOCS) == []
