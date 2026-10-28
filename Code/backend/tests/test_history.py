import io
import json
import os
import time
import zipfile

import pytest

from chatbot.history import ConversationStore, export, export_command

ADDRESS = "12, MG Road, Bengaluru 560001"


def store(tmp_path):
    return ConversationStore(tmp_path / "conversations", retention_days=1)


def seed(s, cid):
    s.append(cid, [
        {"role": "user", "content": "My number is 98765 43210. What is the delivery address?"},
        {"role": "assistant", "content": f"Address: {ADDRESS} [1]", "revealed": ["address"],
         "citations": [{"id": 1, "title": "orders.md", "section": "Order 7", "snippet": f"Address: {ADDRESS}, Phone: 9876501234"}],
         "scope": ["orders.md"]},
        {"role": "user", "content": "And later?"},
        {"role": "assistant", "content": f"It was delivered to {ADDRESS}. Call 9876501234."},
    ], scope=["orders.md"])


def test_store_round_trip_and_scope(tmp_path):
    s, cid = store(tmp_path), ConversationStore.new_id()
    assert s.load(cid) is None and s.scope(cid) == []
    seed(s, cid)
    conversation = s.load(cid)
    assert [m["role"] for m in conversation["messages"]] == ["user", "assistant", "user", "assistant"]
    assert all(m["time"] for m in conversation["messages"])
    assert s.scope(cid) == ["orders.md"]
    assert conversation["messages"][1]["citations"][0]["title"] == "orders.md"
    assert s.delete(cid) and s.load(cid) is None and not s.delete(cid)


def test_store_keeps_only_what_was_revealed(tmp_path):
    s, cid = store(tmp_path), ConversationStore.new_id()
    seed(s, cid)
    raw = (tmp_path / "conversations" / f"{cid}.json").read_text()
    assert "98765 43210" not in raw  # the user's own message is masked
    assert ADDRESS in raw  # revealed in that answer
    assert "9876501234" not in raw  # never asked for
    later = s.load(cid)["messages"][3]["content"]
    assert ADDRESS not in later  # a reveal applies to its own answer only


def test_ids_are_validated(tmp_path):
    s = store(tmp_path)
    for bad in (None, "", "../../etc/passwd", "short", "x" * 65):
        assert not s.valid_id(bad) and s.load(bad) is None
    with pytest.raises(ValueError):
        s.append("../escape", [{"role": "user", "content": "x"}], [])
    assert len(ConversationStore.new_id()) >= 22


def test_expired_conversations_are_deleted(tmp_path):
    s, old, new = store(tmp_path), ConversationStore.new_id(), ConversationStore.new_id()
    s.append(old, [{"role": "user", "content": "hi"}], [])
    path = tmp_path / "conversations" / f"{old}.json"
    os.utime(path, (time.time() - 3 * 86400,) * 2)
    s._pruned = 0
    s.append(new, [{"role": "user", "content": "hi"}], [])
    assert not path.exists() and s.load(new)


@pytest.mark.parametrize("fmt", ["txt", "json", "docx"])
def test_exports_respect_masking(tmp_path, fmt):
    s, cid = store(tmp_path), ConversationStore.new_id()
    seed(s, cid)
    content, media_type, filename = export(s.load(cid), fmt)
    if fmt == "docx":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(z.namelist())
            text = z.read("word/document.xml").decode()
    else:
        text = content.decode()
    assert filename.startswith("chat-history-") and filename.endswith(f".{fmt}")
    assert ADDRESS in text  # revealed in the conversation
    assert "9876501234" not in text and "98765 43210" not in text  # never revealed
    assert "orders.md" in text


def test_json_export_structure(tmp_path):
    s, cid = store(tmp_path), ConversationStore.new_id()
    seed(s, cid)
    data = json.loads(export(s.load(cid), "json")[0])
    first = data["messages"][1]
    assert set(first) == {"time", "role", "content", "citations", "scope"}
    assert first["scope"] == ["orders.md"] and first["citations"][0]["section"] == "Order 7"


@pytest.mark.parametrize("message, fmt", [
    ("download chat history", "txt"), ("Export chat", "txt"), ("Please export the conversation as JSON.", "json"),
    ("download my chat history as word", "docx"), ("export chat history in docx", "docx"),
    ("How do I export my invoices?", None), ("What does the warranty say about chat support?", None),
    ("Can you download the warranty policy?", None),
])
def test_export_command(message, fmt):
    assert export_command(message) == fmt
