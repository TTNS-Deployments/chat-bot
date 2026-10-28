"""Retrieval: find the passages that ground an answer."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from .documents import DocumentStore
from .search import CHUNK_CHARS, CHUNK_OVERLAP, SearchClient

FOLLOW_UP_WORDS = 6


@dataclass(frozen=True)
class Passage:
    id: str
    title: str  # the document's file name, which is also its ID
    content: str
    score: float = 0.0
    section: str = ""  # nearest heading, when the document has headings


class Retriever(Protocol):
    def retrieve(self, query: str, top: int, documents: Sequence[str] = ()) -> list[Passage]:
        """The best passages for `query`. A non-empty `documents` restricts the search to those documents."""
        ...

    def document_names(self) -> list[str]: ...


_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)


def first_heading(text: str) -> str:
    m = _HEADING_LINE.search(text)
    return m.group(1).strip() if m else ""


def build_search_query(question: str, history: Sequence[dict], max_chars: int = 500) -> str:
    """The text sent to search for this turn.

    Short follow-ups ("and for the premium model?") say little on their own, so they are searched
    together with the previous user question.
    """
    query = " ".join(question.split())
    if len(query.split()) < FOLLOW_UP_WORDS:
        previous = next((m["content"] for m in reversed(history) if m.get("role") == "user"), "")
        if previous:
            query = f"{' '.join(previous.split())} {query}"
    return query[-max_chars:] if len(query) > max_chars else query


class AzureSearchRetriever:
    """Hybrid (keyword + vector) search, with semantic reranking when it is configured."""

    def __init__(self, client: SearchClient, documents: DocumentStore | None = None):
        self.client = client
        self.documents = documents

    def document_names(self) -> list[str]:
        return [d.name for d in self.documents.list()] if self.documents else []

    def retrieve(self, query: str, top: int, documents: Sequence[str] = ()) -> list[Passage]:
        passages = []
        for doc in self.client.search(query, top, documents=documents):
            content = (doc.get("chunk") or "").strip()
            if not content:
                continue
            passages.append(Passage(
                id=doc.get("chunk_id", ""),
                title=doc.get("title") or "Untitled document",
                content=content,
                score=float(doc.get("@search.rerankerScore") or doc.get("@search.score") or 0.0),
                section=first_heading(content),
            ))
        if documents:  # the search filter already did this; never let another document through
            passages = [p for p in passages if p.title in documents]
        return passages


# --------------------------------------------------------------------------------------
# Local mode: keyword scoring over text files, so the app runs without Azure.
# --------------------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by can do does for from how i in is it my of on or our the this to "
                  "was what when where which who why will with you your".split())
LOCAL_EXTENSIONS = (".md", ".txt")


def _terms(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


def split_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        chunks.append(text[start:start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


class LocalRetriever:
    def __init__(self, folders: Iterable[Path]):
        self.folders = list(folders)

    def _paths(self) -> dict[str, Path]:
        """Searchable files by name. A later folder (uploads) replaces a same-named earlier file."""
        paths = {}
        for folder in self.folders:
            if folder.is_dir():
                for path in sorted(folder.iterdir()):
                    if path.suffix.lower() in LOCAL_EXTENSIONS and path.is_file():
                        paths[path.name] = path
        return paths

    def document_names(self) -> list[str]:
        return sorted(self._paths())

    def _passages(self, documents: Sequence[str] = ()) -> list[Passage]:
        passages = []
        for name, path in self._paths().items():
            if documents and name not in documents:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            step = CHUNK_CHARS - CHUNK_OVERLAP
            for i, chunk in enumerate(split_text(text)):
                # The heading in force where the chunk starts, or the first one inside it.
                before = _HEADING_LINE.findall(text[:i * step])
                section = before[-1].strip() if before else first_heading(chunk)
                passages.append(Passage(id=f"{name}-{i}", title=name, content=chunk, section=section))
        return passages

    def retrieve(self, query: str, top: int, documents: Sequence[str] = ()) -> list[Passage]:
        wanted = set(_terms(query))
        if not wanted:
            return []
        scored = []
        for p in self._passages(documents):
            words = _terms(p.content) + _terms(p.title)
            hits = sum(1 for w in words if w in wanted)
            coverage = len(wanted.intersection(words))
            if coverage:
                scored.append((coverage * 10 + hits, p))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [Passage(p.id, p.title, p.content, float(score), p.section) for score, p in scored[:top]]
