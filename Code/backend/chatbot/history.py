"""Conversation history: stored per conversation, exported on request as .txt, .json or .docx.

Each conversation is one JSON file named after its random ID, in a folder every worker can reach
(on App Service, /home is shared by all workers and instances). Messages are stored as the user saw
them: answers are already masked, and user messages are masked here, so the store never holds
personal information that was not explicitly revealed. Exports mask again with each message's own
reveals, so a value stays hidden unless it was revealed in that conversation.

Conversations untouched for `retention_days` are deleted.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from xml.sax.saxutils import escape

from .privacy import mask

log = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
EXPORT_FORMATS = {"txt": "text/plain; charset=utf-8", "json": "application/json",
                  "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_FORMAT_WORDS = {"txt": "txt", "text": "txt", "json": "json", "docx": "docx", "word": "docx"}
_EXPORT_COMMAND = re.compile(
    r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:download|export|save|get|give me)\s+(?:the\s+|my\s+|this\s+|our\s+)?"
    r"(?:chat|conversation)(?:\s+(?:history|log|transcript))?\b(?P<rest>.{0,40})$", re.I)


def export_command(message: str) -> str | None:
    """The export format ("txt", "json" or "docx") when the message asks to download the chat history."""
    m = _EXPORT_COMMAND.match(message.strip().rstrip(".!?"))
    if not m:
        return None
    word = re.search(r"\b(txt|text|json|docx|word)\b", m.group("rest"), re.I)
    return _FORMAT_WORDS[word.group(1).lower()] if word else "txt"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConversationStore:
    def __init__(self, folder: Path, retention_days: int = 7):
        self.folder = Path(folder)
        self.retention_seconds = retention_days * 86400
        self._lock = threading.Lock()
        self._pruned = 0.0

    @staticmethod
    def new_id() -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def valid_id(conversation_id: str | None) -> bool:
        return bool(conversation_id and _ID.match(conversation_id))

    def _path(self, conversation_id: str) -> Path:
        if not self.valid_id(conversation_id):
            raise ValueError("invalid conversation id")
        return self.folder / f"{conversation_id}.json"

    def load(self, conversation_id: str | None) -> dict | None:
        if not self.valid_id(conversation_id):
            return None
        try:
            return json.loads(self._path(conversation_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def append(self, conversation_id: str, messages: Sequence[dict], scope: Sequence[str],
               choices: Sequence[str] = ()) -> dict:
        """Add messages ({role, content, citations?, scope?, revealed?}) and remember the current scope.
        `choices` are the documents the bot just asked the user to choose from (cleared otherwise)."""
        with self._lock:
            conversation = self.load(conversation_id) or {"id": conversation_id, "created": now_iso(), "messages": []}
            for m in messages:
                revealed = sorted(m.get("revealed") or []) if m["role"] == "assistant" else []
                conversation["messages"].append({
                    "time": m.get("time") or now_iso(),
                    "role": m["role"],
                    "content": mask(m["content"], revealed).text,
                    "citations": [{**c, "snippet": mask(c.get("snippet", ""), revealed).text}
                                  for c in m.get("citations") or []],
                    "scope": list(m.get("scope") or []),
                    "revealed": revealed,
                })
            conversation["scope"] = list(scope)
            conversation["choices"] = list(choices)
            conversation["updated"] = now_iso()
            self._write(conversation_id, conversation)
        self._prune()
        return conversation

    def scope(self, conversation_id: str | None) -> list[str]:
        return list((self.load(conversation_id) or {}).get("scope") or [])

    def choices(self, conversation_id: str | None) -> list[str]:
        """Documents the bot asked the user to choose between in its last answer (empty if it did not ask)."""
        return list((self.load(conversation_id) or {}).get("choices") or [])

    def delete(self, conversation_id: str | None) -> bool:
        if not self.valid_id(conversation_id):
            return False
        try:
            self._path(conversation_id).unlink()
            return True
        except FileNotFoundError:
            return False

    def _write(self, conversation_id: str, conversation: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(conversation_id)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(conversation, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _prune(self) -> None:
        """Delete expired conversations, at most once an hour per worker."""
        now = time.time()
        if now - self._pruned < 3600:
            return
        self._pruned = now
        for path in self.folder.glob("*.json"):
            try:
                if now - path.stat().st_mtime > self.retention_seconds:
                    path.unlink()
            except OSError as exc:
                log.warning("Could not delete an expired conversation: %s", exc)


# --------------------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------------------
def _masked_messages(conversation: dict) -> list[dict]:
    """Messages masked again with their own reveals: anything not revealed in the conversation stays hidden."""
    out = []
    for m in conversation.get("messages", []):
        revealed = (m.get("revealed") or []) if m.get("role") == "assistant" else []
        out.append({**m, "content": mask(m.get("content", ""), revealed).text,
                    "citations": [{**c, "snippet": mask(c.get("snippet", ""), revealed).text}
                                  for c in m.get("citations") or []]})
    return out


def _source_line(c: dict) -> str:
    return f"[{c.get('id')}] {c.get('title', '')}" + (f" – {c['section']}" if c.get("section") else "")


def _blocks(conversation: dict) -> list[tuple[str, str]]:
    """(style, text) pairs shared by the text and Word exports. Styles: title, meta, heading, body, source."""
    blocks = [("title", "Chat history"),
              ("meta", f"Conversation started {conversation.get('created', '')} (UTC). Exported {now_iso()} (UTC)."),
              ("meta", "Personal information is hidden unless it was explicitly asked for in this conversation.")]
    for m in _masked_messages(conversation):
        who = "You" if m.get("role") == "user" else "Assistant"
        scope = f" · searched only {', '.join(m['scope'])}" if m.get("role") == "assistant" and m.get("scope") else ""
        blocks.append(("heading", f"{who} · {m.get('time', '')}{scope}"))
        blocks.extend(("body", line) for line in m.get("content", "").split("\n"))
        if m.get("citations"):
            blocks.append(("source", "Sources:"))
            blocks.extend(("source", _source_line(c)) for c in m["citations"])
    return blocks


def export_txt(conversation: dict) -> bytes:
    lines = []
    for style, text in _blocks(conversation):
        if style == "heading":
            lines += ["", text, "-" * min(len(text), 60)]
        elif style == "title":
            lines += [text, "=" * len(text)]
        elif style == "source":
            lines.append(f"  {text}")
        else:
            lines.append(text)
    return ("\n".join(lines).strip() + "\n").encode("utf-8")


def export_json(conversation: dict) -> bytes:
    data = {"id": conversation.get("id"), "created": conversation.get("created"), "exported": now_iso(),
            "messages": [{k: m.get(k) for k in ("time", "role", "content", "citations", "scope")}
                         for m in _masked_messages(conversation)]}
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_RUN = {"title": '<w:rPr><w:b/><w:sz w:val="32"/></w:rPr>', "heading": '<w:rPr><w:b/></w:rPr>',
        "meta": '<w:rPr><w:i/><w:color w:val="666666"/></w:rPr>', "source": '<w:rPr><w:color w:val="555555"/><w:sz w:val="18"/></w:rPr>',
        "body": ""}


def export_docx(conversation: dict) -> bytes:
    """A minimal Word document (WordprocessingML), built without extra packages."""
    paragraphs = []
    for style, text in _blocks(conversation):
        spacing = '<w:pPr><w:spacing w:before="240"/></w:pPr>' if style == "heading" else ""
        t = escape(_XML_INVALID.sub("", text))
        paragraphs.append(f'<w:p>{spacing}<w:r>{_RUN[style]}<w:t xml:space="preserve">{t}</w:t></w:r></w:p>')
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                + "".join(paragraphs) + "</w:body></w:document>")
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" '
                     'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buffer.getvalue()


EXPORTERS = {"txt": export_txt, "json": export_json, "docx": export_docx}


def export(conversation: dict, fmt: str) -> tuple[bytes, str, str]:
    """(content, media type, file name) of the conversation in `fmt`."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return EXPORTERS[fmt](conversation), EXPORT_FORMATS[fmt], f"chat-history-{stamp}.{fmt}"
