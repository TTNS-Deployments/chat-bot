"""Which documents a question is about. Runs before retrieval.

A document's ID is its file name: it is unique in the documents container (an upload with the same
name replaces the file), and the indexer stores it on every chunk as the filterable `title` field.
A scope is a tuple of such names; the empty tuple means "all documents". Retrieval filters on it in
the search query itself, so other documents' chunks are never retrieved.

How a scope is chosen, first match wins:
1. "search all documents" (and similar) clears the scope.
2. The message names documents: a full file name ("warranty-policy.md"), or a reference after a
   scoping phrase ("only in the warranty policy", "specifically from the returns document", "in
   the X file"). A reference that matches no document, or several, makes the bot ask. Weaker cues
   ("according to the X", "summarize the X") count only when they match exactly one document.
3. "this document" / "just this file" means the document selected in the page, or else the current scope.
4. The document selected in the page (API callers only; the chat page has no selector).
5. The scope of the previous question (scopes persist until changed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .config import ALLOWED_EXTENSIONS

_EXTENSIONS = {e.lstrip(".") for e in ALLOWED_EXTENSIONS}
_DOC_WORDS = (r"(?:documents?|docs?|files?|statements?|pdfs?|reports?|sheets?|spreadsheets?|manuals?|boqs?|quotations?|"
              r"quotes?|invoices?|contracts?|estimates?|proposals?|tenders?|agreements?)")
_REF = r"(?:the\s+)?(?P<ref>[^,.?!;:\n]{1,80}?)"
_END = r"(?=\s*(?:[,.?!;:\n]|$))"

_RESET = re.compile(r"\b(?:search|use|check|look (?:in|at|through)|across|from|in|over)\s+(?:all|every|each)\s+"
                    r"(?:of\s+the\s+|the\s+)?(?:\w+\s+)?" + _DOC_WORDS + r"|\ball\s+(?:the\s+)?" + _DOC_WORDS + r"\b|"
                    r"\b(?:clear|reset|remove)\s+(?:the\s+)?(?:document\s+)?(?:scope|filter)\b", re.I)
_DEICTIC = re.compile(r"\b(?:this|that|the\s+(?:selected|current|same|above|open))\s+(?:one|" + _DOC_WORDS + r")\b", re.I)
# Strong cues: the user clearly wants one document. A reference that matches nothing makes the bot ask.
_STRONG = [
    re.compile(r"\b(?:only|specifically|solely|exclusively|just|strictly)\s+(?:search\s+|look\s+|answer\s+)?"
               r"(?:in|from|within|inside|using|use|search|check|on|based on|refer to|look at)\s+" + _REF
               + r"(?:\s+" + _DOC_WORDS + r"\b|" + _END + r"|\s+only\b)", re.I),
    re.compile(r"\b(?:in|from|within|inside|of|using|use|search|check|per|according to|based on)\s+" + _REF
               + r"\s+" + _DOC_WORDS + r"\b", re.I),
    re.compile(r"\b(?:in|from|within|inside|using|use|search|check)\s+" + _REF + r"\s+only\b", re.I),
    re.compile(r"\b" + _DOC_WORDS + r"\s+(?:called|named|titled)\s+[\"“']?" + _REF + r"[\"”']?" + _END, re.I),
]
# Weak cues: "according to the warranty policy", "summarize the returns policy". Used only when the
# reference matches a document.
_WEAK = [
    re.compile(r"\b(?:according to|as per|per)\s+" + _REF + _END, re.I),
    re.compile(r"\b(?:summar(?:ise|ize)|recap|tl;?\s?dr(?:\s+of)?|(?:summary|gist|overview|recap)\s+of)\s+" + _REF + _END, re.I),
]
_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bor\b|\bplus\b)\s*", re.I)
_FILLER = frozenset("a an the this that these those my our your its of in on for to from with only just and or one".split())
_STOP = _FILLER | frozenset("document documents doc docs file files statement statements pdf pdfs report reports "
                            "sheet sheets spreadsheet spreadsheets manual manuals boq boqs quotation quotations quote "
                            "quotes invoice invoices contract contracts estimate estimates proposal proposals tender "
                            "tenders agreement agreements".split())


@dataclass(frozen=True)
class Scope:
    documents: tuple[str, ...] = ()  # empty: all documents
    clarify: str = ""  # when set, ask the user this instead of answering
    source: str = "all"  # all | message | reset | selection | previous | choice
    choices: tuple[str, ...] = ()  # documents offered in the clarifying question

    @property
    def is_all(self) -> bool:
        return not self.documents


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _EXTENSIONS]


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _same_word(a: str, b: str) -> bool:
    """Equal, or one is a prefix of the other (refund/refunds, warranty/warranties)."""
    if a == b:
        return True
    a, b = (a, b) if len(a) <= len(b) else (b, a)
    return len(a) >= 4 and (b.startswith(a) or b.startswith(a[:-1] + "ie"))


def match_reference(reference: str, documents: Sequence[str]) -> list[str]:
    """Documents whose name contains every word of the reference ("returns" → returns-and-refunds.md)."""
    ref = reference.strip().lower()
    exact = [d for d in documents if ref in (d.lower(), _stem(d).lower())]
    if exact:
        return exact
    wanted = _wanted(reference)
    if not wanted:
        return []
    return [d for d in documents if all(any(_same_word(w, t) for t in _words(_stem(d))) for w in wanted)]


def _wanted(reference: str) -> list[str]:
    """The words that identify a document: "the March statement" → march. When only generic words are
    left ("the statement"), those count, so documents named like that still match."""
    words = [w for w in _words(reference) if w not in _FILLER]
    return [w for w in words if w not in _STOP] or words


def _partial(reference: str, documents: Sequence[str]) -> list[str]:
    wanted = _wanted(reference)
    return [d for d in documents if any(_same_word(w, t) for w in wanted for t in _words(_stem(d)))]


def _named(message: str, documents: Sequence[str]) -> list[str]:
    """Documents named by their file name, or by a hyphenated name such as returns-and-refunds."""
    text = message.lower()
    found = []
    for d in documents:
        stem = _stem(d).lower()
        candidates = [d.lower()] + ([stem] if re.search(r"[-_]", stem) else [])
        if any(re.search(rf"(?<![\w.-]){re.escape(c)}(?![\w-])", text) for c in candidates):
            found.append(d)
    return found


def _list(names: Sequence[str], conjunction: str = "or") -> str:
    return f"{', '.join(names[:-1])} {conjunction} {names[-1]}" if len(names) > 1 else names[0]


def resolve_scope(message: str, documents: Sequence[str], selected: Sequence[str] | None = None,
                  previous: Sequence[str] = ()) -> Scope:
    """Choose the documents to search for `message` (see the module docstring)."""
    available = list(dict.fromkeys(documents))
    if _RESET.search(message):
        return Scope(source="reset")

    chosen = _named(message, available)
    unresolved: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    for pattern in _STRONG:
        for m in pattern.finditer(message):
            ref = m.group("ref")
            if _DEICTIC.fullmatch(f"{ref} document") or ref.strip().lower() in ("this", "that", "it", "same", "current",
                                                                                 "selected", "above"):
                continue
            for part in [p for p in _SPLIT.split(ref) if p.strip()]:
                if any(d in chosen for d in _named(part, available)):
                    continue
                matches = match_reference(part, available)
                if len(matches) == 1:
                    chosen.append(matches[0])
                elif matches:
                    ambiguous.append((part.strip(), matches))
                elif _wanted(part):
                    unresolved.append(part.strip())
    if not chosen and not ambiguous and not unresolved:
        for m in (m for pattern in _WEAK for m in pattern.finditer(message)):
            matches = match_reference(m.group("ref"), available)
            if len(matches) == 1:
                chosen.append(matches[0])
    chosen = list(dict.fromkeys(chosen))

    if ambiguous and not chosen:
        ref, matches = ambiguous[0]
        return Scope(clarify=f"More than one document matches “{ref}”. Which one do you mean?\n\n{numbered(matches)}",
                     choices=tuple(matches))
    if unresolved and not chosen:
        ref = unresolved[0]
        close = _partial(ref, available)
        if close:
            return Scope(clarify=f"I could not find a document called “{ref}”. Did you mean one of these?\n\n"
                                 f"{numbered(close)}", choices=tuple(close))
        listing = _list(available, "and") if available else "none yet"
        return Scope(clarify=f"I could not find a document called “{ref}”. The available documents are: {listing}. "
                             f"Which one should I search?")
    if chosen:
        return Scope(tuple(chosen), source="message")

    if _DEICTIC.search(message):
        current = [d for d in (selected or previous or ()) if d in available]
        if current:
            return Scope(tuple(current), source="selection" if selected else "previous")
        return Scope(clarify="Which document do you mean? Name it in your question.")

    if selected:
        missing = [d for d in selected if d not in available]
        if missing:
            return Scope(clarify=f"The document {missing[0]} is not available any more. Which document should I search?")
        return Scope(tuple(dict.fromkeys(selected)), source="selection")
    if selected is None and previous:
        kept = tuple(d for d in previous if d in available)
        if kept:
            return Scope(kept, source="previous")
        return Scope(clarify=f"The document {previous[0]} is not available any more. Which document should I search?")
    return Scope()


# --------------------------------------------------------------------------------------
# Replies to "which document do you mean?"
# --------------------------------------------------------------------------------------
# "one" is not a number here: "the Delhi one" names a document, it does not mean the first.
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3,
             "fourth": 4, "4th": 4, "four": 4, "fifth": 5, "5th": 5, "five": 5}
_ALL_OF_THEM = re.compile(r"\b(?:all|both|each|every|compare|comparison|all of them)\b", re.I)
_NUMBER = re.compile(r"^\s*(?:option|number|no\.?|#|document|doc|file)?\s*(\d{1,2})\s*[.)]?\s*$", re.I)


def resolve_choice(reply: str, choices: Sequence[str]) -> list[str]:
    """Which of the offered documents a reply picks: "2", "the second one", "Mumbai", a file name, or
    "all of them". Returns [] when the reply does not pick any (then it is treated as a new question)."""
    choices = list(choices)
    if not choices:
        return []
    named = _named(reply, choices)
    if named:
        return named
    number = _NUMBER.match(reply)
    if number and 1 <= int(number.group(1)) <= len(choices):
        return [choices[int(number.group(1)) - 1]]
    words = _words(reply)
    if len(words) <= 6:
        if "last" in words:
            return [choices[-1]]
        for w in words:
            if w in _ORDINALS and _ORDINALS[w] <= len(choices) and not any(w in _words(_stem(c)) for c in choices):
                return [choices[_ORDINALS[w] - 1]]
    if _ALL_OF_THEM.search(reply):
        return choices
    matches = match_reference(reply, choices)
    if len(matches) == 1:
        return matches
    # Distinctive words: a word of the reply that appears in exactly one document name ("mumbai").
    wanted = [w for w in _wanted(reply) if w not in _STOP]
    hits = {c for c in choices for w in wanted if any(_same_word(w, t) for t in _words(_stem(c)))}
    distinctive = [c for c in choices if c in hits and
                   any(sum(any(_same_word(w, t) for t in _words(_stem(o))) for o in choices) == 1
                       and any(_same_word(w, t) for t in _words(_stem(c))) for w in wanted)]
    return distinctive if len(distinctive) == 1 else []


def numbered(choices: Sequence[str]) -> str:
    return "\n".join(f"{i}. {name}" for i, name in enumerate(choices, start=1))
