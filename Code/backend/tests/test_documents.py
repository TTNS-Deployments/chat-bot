import pytest

from chatbot.config import settings_from_env
from chatbot.documents import LocalDocumentStore, safe_name


@pytest.mark.parametrize("raw, expected", [
    ("report.PDF", "report.pdf"),
    ("../../etc/passwd.txt", "passwd.txt"),
    ("C:\\Users\\me\\Q3 plan (v2).docx", "Q3 plan (v2).docx"),
    ("na<me>?.md", "na-me.md"),
])
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "noext", ".md", "virus.exe", "archive.zip"])
def test_safe_name_rejects(raw):
    with pytest.raises(ValueError):
        safe_name(raw)


def test_local_store_round_trip(tmp_path):
    store = LocalDocumentStore(tmp_path / "docs")
    assert store.list() == []
    store.save("a.txt", b"hello", "text/plain")
    assert [(d.name, d.size) for d in store.list()] == [("a.txt", 5)]
    assert store.delete("a.txt") and not store.delete("a.txt")


def test_settings_from_env():
    s = settings_from_env({"AZURE_OPENAI_ENDPOINT": "https://x.openai.azure.com/", "SEARCH_TOP_K": "99",
                           "ALLOW_DOCUMENT_UPLOAD": "False", "CHAT_TEMPERATURE": "", "RATE_LIMIT_PER_MINUTE": "bad"})
    assert s.mode == "azure" and s.openai_endpoint == "https://x.openai.azure.com"
    assert s.search_top_k == 20 and s.allow_document_upload is False
    assert s.chat_temperature == 0.2 and s.rate_limit_per_minute == 20
    assert settings_from_env({}).mode == "local"
    assert settings_from_env({"CHATBOT_MODE": "AZURE"}).missing_azure_settings()
