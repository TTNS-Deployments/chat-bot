"""Summary requests: short by default, detailed only when asked. Runs before retrieval.

A short summary is 2-3 sentences or 3-5 bullets (about 60-80 words). Three things enforce it: the
prompt says so, the model call is capped at SUMMARY_MAX_TOKENS, and `trim_summary` cuts what is
still too long at a sentence or bullet boundary. Short summaries end with MORE_DETAIL; answering
"expand", "more detail" or "yes" to that line asks for the detailed summary.
"""
from __future__ import annotations

import re
from typing import Sequence

SHORT, DETAILED = "short", "detailed"
MORE_DETAIL = "Want more detail?"
SHORT_WORDS = 90  # hard limit for a short summary of one document (the prompt asks for 60-80)
WORDS_PER_DOCUMENT = 35  # one labelled line per document when several are summarized
TOKENS_PER_DOCUMENT = 40
MAX_DOCUMENTS = 5  # with more documents and none named, the bot asks which one to summarize

_SUMMARY = re.compile(r"\b(?:summar(?:y|ies|ise|ize|ised|ized|ising|izing)|sum\s+(?:it|this|that|them)\s+up|gist|"
                      r"tl;?\s?dr|overview|recap)\b", re.I)
_DETAILED = re.compile(r"\b(?:detailed|full|complete|comprehensive|thorough|long(?:er)?|in[- ]depth|in\s+detail|"
                       r"expand(?:ed)?|elaborate|more\s+details?|section[- ]by[- ]section)\b", re.I)
_YES = re.compile(r"^\s*(?:yes|yeah|yep|sure|ok(?:ay)?|please)(?:\s+please)?\s*[.!]*\s*$", re.I)
_OFFER = re.compile(r"\n*\s*" + re.escape(MORE_DETAIL) + r"\s*$", re.I)
_GENERIC = frozenset("""a an the me us please can could would you i we give provide write make show tell do quick
    short brief detailed full complete long longer in depth more detail details of for on about this that it its them all
    every each document documents doc docs file files pdf report key main points it up sum and""".split())
_SENTENCE = re.compile(r"(?<=[.!?\]])\s+(?=[A-Z0-9\"“(*•-])")


def summary_request(message: str, history: Sequence[dict] = ()) -> str:
    """SHORT or DETAILED for a summary request, "" otherwise."""
    if _SUMMARY.search(message):
        return DETAILED if _DETAILED.search(message) else SHORT
    last = next((m.get("content", "") for m in reversed(history) if m.get("role") == "assistant"), "")
    if _OFFER.search(last) and (_DETAILED.search(message) or _YES.match(message)):
        return DETAILED  # a follow-up to "Want more detail?"
    return ""


def summary_query(query: str, documents: Sequence[str] = ()) -> str:
    """The search text for a summary. "Summarize" alone says nothing about the content, so when the
    question has no other words, the documents' names are searched instead."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _GENERIC and not _SUMMARY.fullmatch(w)
             and w not in ("tl", "dr", "tldr")]
    if words or not documents:
        return query
    return " ".join(re.sub(r"[-_.]+", " ", d.rsplit(".", 1)[0]) for d in documents)


def strip_offer(text: str) -> str:
    """The answer without a trailing "Want more detail?" (the application adds it)."""
    return _OFFER.sub("", text).rstrip()


def trim_summary(text: str, max_words: int) -> str:
    """Keep whole lines and sentences until `max_words` is reached (at least one sentence is kept)."""
    if len(text.split()) <= max_words:
        return text
    lines: list[str] = []
    used = 0
    for line in text.splitlines():
        kept = []
        for sentence in _SENTENCE.split(line):
            words = len(sentence.split())
            if used and used + words > max_words:
                break
            kept.append(sentence)
            used += words
        if kept:
            lines.append(" ".join(kept))
        if len(kept) < len(_SENTENCE.split(line)) or used >= max_words:
            break
    return "\n".join(lines).strip()
