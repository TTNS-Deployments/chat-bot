"""Retrieval: find the passages that ground an answer."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from .search import CHUNK_CHARS, CHUNK_OVERLAP, SearchClient

FOLLOW_UP_WORDS = 6


@dataclass(frozen=True)
class Passage:
    id: str
    title: str
    content: str
    score: float = 0.0


class Retriever(Protocol):
    def retrieve(self, query: str, top: int) -> list[Passage]: ...


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

    def __init__(self, client: SearchClient):
        self.client = client

    def retrieve(self, query: str, top: int) -> list[Passage]:
        passages = []
        for doc in self.client.search(query, top):
            content = (doc.get("chunk") or "").strip()
            if not content:
                continue
            passages.append(Passage(
                id=doc.get("chunk_id", ""),
                title=doc.get("title") or "Untitled document",
                content=content,
                score=float(doc.get("@search.rerankerScore") or doc.get("@search.score") or 0.0),
            ))
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

    def _passages(self) -> list[Passage]:
        passages = []
        for folder in self.folders:
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if path.suffix.lower() not in LOCAL_EXTENSIONS or not path.is_file():
                    continue
                for i, chunk in enumerate(split_text(path.read_text(encoding="utf-8", errors="replace"))):
                    passages.append(Passage(id=f"{path.name}-{i}", title=path.name, content=chunk))
        return passages

    def retrieve(self, query: str, top: int) -> list[Passage]:
        wanted = set(_terms(query))
        if not wanted:
            return []
        scored = []
        for p in self._passages():
            words = _terms(p.content) + _terms(p.title)
            hits = sum(1 for w in words if w in wanted)
            coverage = len(wanted.intersection(words))
            if coverage:
                scored.append((coverage * 10 + hits, p))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [Passage(p.id, p.title, p.content, float(score)) for score, p in scored[:top]]
