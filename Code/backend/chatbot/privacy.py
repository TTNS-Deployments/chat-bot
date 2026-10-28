"""Hides personal and customer-identifying information unless the user asks for it.

Every answer, source snippet, stored message, export and log line goes through `mask()`. The chat
model is also told to hide these values, but this layer is what enforces it.

Categories (what a user can ask to see, one at a time):
    name      people's names
    address   postal addresses
    phone     phone numbers (shown as ******1234)
    email     email addresses
    id        ID, account, card and similar numbers (long numbers shown as ******1234)
    count     customer counts ("12,500 customers")

Detection is rule based (regular expressions, labelled fields such as "Address: …", and table
columns with such headings). It needs no extra packages, and it errs on the side of hiding.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

CATEGORIES = ("name", "address", "phone", "email", "id", "count")
LABELS = {"name": "names", "address": "addresses", "phone": "phone numbers", "email": "email addresses",
          "id": "ID and account numbers", "count": "customer counts"}

HIDDEN = "[hidden]"
HIDDEN_COUNT = "[hidden, ask to view]"


@dataclass(frozen=True)
class Masked:
    text: str
    hidden: frozenset[str] = field(default_factory=frozenset)  # categories that were hidden


# --------------------------------------------------------------------------------------
# Which categories did the user explicitly ask to see?
# --------------------------------------------------------------------------------------
_ASK = re.compile(r"\b(show|reveal|display|give|tell|list|what|what's|whats|which|who|share|provide|unmask|unhide|"
                  r"view|see|include|need|want|print|read out|how many)\b|\?", re.I)
_NEGATION = re.compile(r"\b(don'?t|do not|without|hide|hidden|no|never|exclude|excluding|except|not|mask)\b\W*"
                       r"(?:\w+\W+){0,3}$", re.I)
_ASK_FOR = {
    "phone": re.compile(r"\b(phone|mobile|telephone|cell|whatsapp|contact number|helpline|toll[- ]free)\b", re.I),
    "email": re.compile(r"\b(e-?mail|mail id)\b", re.I),
    "address": re.compile(r"(?<!e-mail )(?<!email )(?<!ip )(?<!web )\baddress(es)?\b", re.I),
    "name": re.compile(r"(?<!file )(?<!document )(?<!product )(?<!company )(?<!model )(?<!plan )(?<!brand )"
                       r"\bnames?\b|\bwho (is|are|was|were)\b", re.I),
    "id": re.compile(r"\b(ids?|identifier|account (number|no)|a/c|card (number|no)|aadhaa?r|pan|passport|ssn|"
                     r"social security|licen[cs]e number|gstin|customer number|account numbers)\b", re.I),
    "count": re.compile(r"\bhow many\b.{0,30}\b(customers|clients|subscribers|users|accounts)\b|"
                        r"\b(number|count|total)\s+of\s+(\w+\s+)?(customers|clients|subscribers|users|accounts)\b|"
                        r"\b(customer|client|subscriber|user)\s+(count|base|numbers)\b", re.I),
}


def requested_reveals(message: str) -> frozenset[str]:
    """Categories the message explicitly asks to see, for example "show the customer address".

    A category counts only when the message is a request (a question or a verb such as show/give)
    and the category is not negated ("don't show the phone number").
    """
    if not _ASK.search(message):
        return frozenset()
    wanted = set()
    for category, pattern in _ASK_FOR.items():
        for m in pattern.finditer(message):
            if not _NEGATION.search(message[:m.start()]):
                wanted.add(category)
                break
    return frozenset(wanted)


# --------------------------------------------------------------------------------------
# Detection and masking
# --------------------------------------------------------------------------------------
def _last4(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return "*" * max(len(digits) - 4, 2) + digits[-4:]


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Digits with optional + ( ) - . and spaces; classified by context and length below.
_NUMBER = re.compile(r"(?<![\w/@*])\+?\(?\d[\d\s().-]{5,22}\d(?![\w/])")
_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$|^\d{1,2}[-.]\d{1,2}[-.]\d{2,4}$")
_ID_CONTEXT = re.compile(r"\b(account|acct|a/c|card|iban|id|aadhaa?r|pan|passport|ssn|customer no|ref(erence)?)\b"
                         r"[^\n]{0,25}$", re.I)
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_ID_TOKEN = re.compile(r"\b(?:CUST|CID|ACC|ACCT|UID|ID)[-_#]?\d{3,}\b", re.I)
_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt|Sri|Mx)\.?\s+[A-Z][a-z'’-]+(?:\s+[A-Z][a-z'’-]+){0,2}")
_INLINE_NAME = re.compile(r"\b((?:customer|client|subscriber|account holder|caller|user)(?:'s|’s)?\s+name\s*"
                          r"(?:is|was|:|-)\s*)([A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+){0,3})")
# Street addresses written without a label: "12, MG Road, Bengaluru 560001", "221B Baker Street".
_STREET = re.compile(r"\b\d{1,5}[A-Za-z]?(?:/\d+)?,?\s+(?:[A-Z][\w'.-]*\s+){0,4}(?:Road|Rd|Street|St|Lane|Ln|Avenue|Ave|"
                     r"Nagar|Marg|Colony|Sector|Block|Cross|Main|Layout|Boulevard|Blvd|Drive|Way|Place|Highway|"
                     r"Apartments?|Society|Chowk|Bazaar|Enclave|Vihar)\b\.?(?:,\s*[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)*)*"
                     r"(?:[\s,-]+\d{5,6}\b)?")
_COUNT_NOUNS = r"(?:customers|clients|subscribers|users|account holders|accounts)"
_COUNT_ADJ = r"(?:(?:active|total|registered|enterprise|business|new|existing|paying|unique|monthly|retail|corporate|" \
             r"happy|loyal|current)\s+){0,2}"
_COUNT_AMOUNT = r"\d(?:[\d,.]*\d)?(?:\s*(?:k|m|mn|lakh|lakhs|crore|crores|million|thousand|billion))?\+?"
_COUNT_BEFORE = re.compile(rf"(?<![\w.,])(?:over\s+|about\s+|nearly\s+|around\s+)?({_COUNT_AMOUNT})(\s+{_COUNT_ADJ}{_COUNT_NOUNS}\b)", re.I)
_COUNT_AFTER = re.compile(rf"(\b(?:number|count|total)\s+of\s+{_COUNT_ADJ}{_COUNT_NOUNS}|\b(?:customer|client|subscriber)\s+"
                          rf"(?:count|base)|\btotal\s+{_COUNT_NOUNS})(\s*(?:is|was|are|were|stands at|of|:|=|-)?\s*"
                          rf"(?:over\s+|about\s+|nearly\s+|around\s+)?)({_COUNT_AMOUNT})", re.I)

# Labelled fields: "Customer address: 12 MG Road, Pune", "- **Name:** Asha Rao", "Account no. 1234…".
_FIELD_LABELS = {
    "name": r"(?:customer|client|subscriber|account holder|contact|full|first|last|nominee|caller)?\s*name|"
            r"(?:(?:account|relationship|branch|sales|customer|client)\s+)?(?:manager|representative|executive|officer)|"
            r"contact person|account holder|nominee",
    "address": r"(?:customer|client|billing|shipping|delivery|home|residential|registered|postal|mailing|permanent|"
               r"current)?\s*address",
    "id": r"(?:customer|client|subscriber|account|card|national|tax)\s*(?:id|number|no\.?|#)|aadhaa?r(?:\s*(?:number|no\.?))?|"
          r"pan(?:\s*(?:number|no\.?))?|passport(?:\s*(?:number|no\.?))?|ssn|gstin|id\s*(?:number|no\.?)?",
    "count": r"(?:number|count|total)\s+of\s+customers|customer\s+count|total\s+customers",
}
_FIELD = re.compile(
    r"(?im)(?:^|(?<=[.;]\s)|(?<=[.;]\s\s))(?P<lead>[ \t]*(?:[-*•][ \t]+|\d+[.)][ \t]+)?(?:\*\*)?)(?P<label>"
    + "|".join(f"(?P<{c}>{p})" for c, p in _FIELD_LABELS.items())
    + r")(?P<sep>(?:\*\*)?[ \t]*(?::|=|–|-|\bis\b)(?:\*\*)?[ \t]*)(?P<value>[^\n|;]+?)(?=\.\s|\.?$|[\n|;])")
_TABLE_HEADER = {c: re.compile(rf"^\s*(?:{p})\s*$", re.I) for c, p in _FIELD_LABELS.items()}
_TABLE_HEADER["name"] = re.compile(rf"^\s*(?:{_FIELD_LABELS['name']}|customer|client|subscriber|account holder)\s*$", re.I)
_TABLE_HEADER["phone"] = re.compile(r"^\s*(?:\w+\s+)?(?:phone|mobile|telephone|contact)(?:\s*(?:number|no\.?))?\s*$", re.I)
_TABLE_HEADER["email"] = re.compile(r"^\s*(?:\w+\s+)?e-?mail(?:\s*address)?\s*$", re.I)


def _looks_like_value(category: str, value: str) -> bool:
    """Field rules only hide values that look like data, not prose such as "Phone: see the invoice"."""
    v = value.strip()
    if not v or v.startswith("[hidden") or v.startswith("**"):
        return False
    if category == "name":
        return bool(re.match(r"[A-Z][\w.'’-]+(?:\s+[A-Z][\w.'’-]+){0,4}\s*[.,]?\s*$", v))
    if category == "address":
        return bool(re.search(r"\d|,", v))
    return bool(re.search(r"\d", v)) if category in ("count",) else len(v) >= 3


class _Masker:
    def __init__(self, reveal: frozenset[str]):
        self.reveal = reveal
        self.hidden: set[str] = set()

    def hides(self, category: str) -> bool:
        if category in self.reveal:
            return False
        self.hidden.add(category)
        return True

    def value(self, category: str, value: str) -> str:
        if category in ("phone", "id") and len(re.sub(r"\D", "", value)) >= 6:
            return _last4(value)
        return HIDDEN_COUNT if category == "count" else HIDDEN

    # ---- passes ----
    def tables(self, text: str) -> str:
        lines = text.split("\n")
        columns: dict[int, str] = {}
        for i, line in enumerate(lines):
            if not line.lstrip().startswith("|"):
                columns = {}
                continue
            cells = line.split("|")
            if not columns:
                columns = {j: c for j, cell in enumerate(cells) for c, pattern in _TABLE_HEADER.items()
                           if pattern.match(cell.replace("*", ""))}
                if not columns:
                    columns = {-1: ""}  # a table without sensitive columns
                continue
            if all(set(cell.strip()) <= set("-: ") for cell in cells):
                continue  # the |---|---| separator row
            for j, category in columns.items():
                if 0 <= j < len(cells) and cells[j].strip() and not cells[j].strip().startswith("[hidden") \
                        and self.hides(category):
                    cells[j] = f" {self.value(category, cells[j].strip())} "
            lines[i] = "|".join(cells)
        return "\n".join(lines)

    def fields(self, text: str) -> str:
        def replace(m: re.Match) -> str:
            category = next(c for c in _FIELD_LABELS if m.group(c))
            value = m.group("value")
            if not _looks_like_value(category, value) or not self.hides(category):
                return m.group(0)
            trailing = re.search(r"\s*(\[\d{1,2}\])*\s*$", value).group(0)  # keep citation markers
            return f"{m.group('lead')}{m.group('label')}{m.group('sep')}{self.value(category, value)}{trailing}"
        return _FIELD.sub(replace, text)

    def patterns(self, text: str) -> str:
        text = _EMAIL.sub(lambda m: HIDDEN if self.hides("email") else m.group(0), text)
        text = _STREET.sub(lambda m: HIDDEN if self.hides("address") else m.group(0), text)
        text = _PAN.sub(lambda m: HIDDEN if self.hides("id") else m.group(0), text)
        text = _ID_TOKEN.sub(lambda m: HIDDEN if self.hides("id") else m.group(0), text)
        text = _NUMBER.sub(lambda m: self._number(m, text), text)
        text = _HONORIFIC.sub(lambda m: HIDDEN if self.hides("name") else m.group(0), text)
        text = _INLINE_NAME.sub(lambda m: m.group(1) + HIDDEN if self.hides("name") else m.group(0), text)
        text = _COUNT_AFTER.sub(lambda m: m.group(1) + m.group(2) + HIDDEN_COUNT if self.hides("count") else m.group(0), text)
        text = _COUNT_BEFORE.sub(lambda m: HIDDEN_COUNT + m.group(2) if self.hides("count") else m.group(0), text)
        return text

    def _number(self, m: re.Match, text: str) -> str:
        raw = m.group(0)
        stripped = raw.strip(" .-()")
        digits = re.sub(r"\D", "", stripped)
        if _DATE.match(stripped) or len(digits) < 8:
            return raw
        if re.fullmatch(r"\d{4}\s\d{4}\s\d{4}", stripped) or _ID_CONTEXT.search(text[:m.start()]):
            category = "id"
        elif len(digits) <= 13:
            category = "phone"
        else:
            category = "id"  # card and long account numbers
        if not self.hides(category):
            return raw
        lead = raw[:len(raw) - len(raw.lstrip(" ("))]
        tail = raw[len(raw.rstrip(" .-)")):]
        return f"{lead}{_last4(stripped)}{tail}"


def mask(text: str, reveal: frozenset[str] | set[str] = frozenset()) -> Masked:
    """Hide sensitive values in `text`, except the categories in `reveal`."""
    if not text:
        return Masked(text or "")
    masker = _Masker(frozenset(reveal))
    text = masker.patterns(masker.fields(masker.tables(text)))
    return Masked(text, frozenset(masker.hidden))


def hidden_note(hidden: frozenset[str] | set[str]) -> str:
    """The line added under an answer when something was hidden."""
    if not hidden:
        return ""
    kinds = [LABELS[c] for c in CATEGORIES if c in hidden]
    listed = kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + " and " + kinds[-1]
    return (f"Some personal or customer information ({listed}) is hidden. "
            f"Ask for a specific item to see it, for example “show the {_EXAMPLE[next(c for c in CATEGORIES if c in hidden)]}”.")


_EXAMPLE = {"name": "customer name", "address": "customer address", "phone": "phone number", "email": "email address",
            "id": "account number", "count": "number of customers"}


class MaskingFilter(logging.Filter):
    """Masks log messages (with no reveals) before any handler writes them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a malformed record is the logging module's problem, not ours
            return True
        masked = mask(message).text
        if masked != message:
            record.msg, record.args = masked, None
        return True
