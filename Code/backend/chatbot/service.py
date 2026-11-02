"""Wires retrieval, prompt building and the chat model together."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable, Sequence

from .config import SAMPLE_DOCS_DIR, Settings
from .documents import BlobDocumentStore, DocumentStore, LocalDocumentStore
from .errors import ChatbotError
from .llm import AzureChatModel, ChatModel, LocalChatModel
from .history import ConversationStore
from .privacy import HIDDEN, hidden_note, mask, never_note, never_show_categories, redacted_categories, requested_reveals
from .prompts import (CLARIFY_PREFIX, Citation, build_messages, extract_citations, is_not_found, parse_clarification,
                      select_passages, source_documents, strip_citations)
from .retrieval import AzureSearchRetriever, LocalRetriever, Retriever, build_search_query, diversify
from .scope import Scope, numbered, resolve_labelled_choice, resolve_scope
from . import summary as summaries
from .search import SearchClient, ensure_search_resources

log = logging.getLogger(__name__)


REVEAL_DISABLED_NOTE = "Showing personal or customer information is turned off for this deployment."
MAX_CHOICES = 5
CHOOSE_HINT = "Reply with the number or the name."
_SHORT_REPLY_WORDS = 4


def _clarification(question: str, choices: Sequence[str]) -> str:
    return f"{question.strip()}\n\n{numbered(choices)}\n\n{CHOOSE_HINT}"


def _last_user_message(history: Sequence[dict]) -> str:
    return next((m.get("content", "") for m in reversed(history) if m.get("role") == "user"), "")


@dataclass(frozen=True)
class ChatAnswer:
    answer: str
    citations: list[Citation]
    scope: tuple[str, ...] = ()  # the documents searched; empty means all
    clarification: bool = False  # the answer is a question back to the user
    revealed: frozenset[str] = frozenset()  # privacy categories shown because the user asked
    choices: tuple[str, ...] = ()  # documents the user is asked to choose from (when clarification)
    choice_labels: tuple[str, ...] = ()  # how those documents were shown (company or project)


class DocumentCatalog:
    """The names of the searchable documents, cached briefly (listing blobs on every question is slow)."""

    def __init__(self, fetch: Callable[[], list[str]], ttl_seconds: float = 60.0):
        self._fetch = fetch
        self.ttl_seconds = ttl_seconds
        self._names: list[str] = []
        self._fetched = float("-inf")
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        with self._lock:
            if time.monotonic() - self._fetched >= self.ttl_seconds:
                self._names, self._fetched = self._fetch(), time.monotonic()
            return list(self._names)

    def invalidate(self) -> None:
        with self._lock:
            self._fetched = float("-inf")


class ChatService:
    def __init__(self, settings: Settings, retriever: Retriever, model: ChatModel):
        self.settings = settings
        self.retriever = retriever
        self.model = model
        self.catalog = DocumentCatalog(retriever.document_names)

    def answer(self, question: str, history: Sequence[dict], selected: Sequence[str] | None = None,
               previous_scope: Sequence[str] = (), choices: Sequence[str] = (),
               choice_labels: Sequence[str] = ()) -> ChatAnswer:
        """Answer `question`. `selected` is the document choice sent by an API caller (None: no choice sent,
        [] all documents); `previous_scope` is the scope of the conversation so far; `choices` are the
        documents the previous answer asked the user to choose from, shown as `choice_labels`."""
        scope = resolve_scope(question, self.catalog.names(), selected, previous_scope)
        if scope.clarify:
            log.info("Asked which document is meant (scope unclear)")
            return ChatAnswer(answer=scope.clarify, citations=[], scope=tuple(previous_scope), clarification=True,
                              choices=scope.choices)

        # The previous answer asked "which document?": this message may be the reply.
        query_prefix = ""
        if choices and scope.source not in ("message", "reset"):
            available = set(self.catalog.names())
            pairs = list(zip(choices, choice_labels)) if len(choice_labels) == len(choices) else [(c, c) for c in choices]
            pairs = [(c, label) for c, label in pairs if c in available]
            offered, labels = [c for c, _ in pairs], [label for _, label in pairs]
            picked = resolve_labelled_choice(question, offered, labels)
            if picked:
                scope = Scope(tuple(picked), source="choice")
                query_prefix = _last_user_message(history)  # the question the user is answering
            elif offered and len(question.split()) <= _SHORT_REPLY_WORDS and not question.rstrip().endswith("?"):
                return ChatAnswer(answer=_clarification("I did not catch which one you mean. Please choose one:", labels),
                                  citations=[], scope=tuple(previous_scope), clarification=True, choices=tuple(offered),
                                  choice_labels=tuple(labels))

        kind = summaries.summary_request(question, history)
        each: list[str] = []  # an unscoped short summary covers each document separately
        if kind == summaries.SHORT and not scope.documents:
            names = self.catalog.names()
            if len(names) > summaries.MAX_DOCUMENTS:
                return ChatAnswer(answer=f"Which document should I summarize? The available documents are: "
                                         f"{', '.join(names)}. Name it in your question.",
                                  citations=[], clarification=True)
            each = names if len(names) > 1 else []

        asked = requested_reveals(question) & redacted_categories()  # a category not hidden needs no asking
        never = never_show_categories()
        reveal = (asked - never) if self.settings.allow_sensitive_reveal else frozenset()
        query = f"{query_prefix} {question}".strip() if query_prefix else build_search_query(question, history)
        if each:
            per_document = max(1, self.settings.search_top_k // len(each))
            found = [p for name in each
                     for p in self.retriever.retrieve(summaries.summary_query(query, [name]), per_document, (name,))]
            each = list(dict.fromkeys(p.title for p in found))
        else:
            if kind:
                query = summaries.summary_query(query, scope.documents or self.catalog.names()[:1])
            top = self.settings.search_top_k
            if scope.documents or kind:
                found = self.retriever.retrieve(query, top, scope.documents)
            else:  # look wider, so that every competing document can reach the model
                found = diversify(self.retriever.retrieve(query, top * 3, ()), top)
        passages = select_passages(found, self.settings.max_source_chars)
        removed: set[str] = set()
        if never:  # the model must not receive these at all
            cleaned = []
            for p in passages:
                m = mask(p.content, categories=never)
                removed |= m.hidden & never
                cleaned.append(replace(p, content=m.text))
            passages = cleaned
        if scope.documents and not passages:
            # Never fall back to other documents: say so and let the user widen the search.
            names = ", ".join(scope.documents)
            text = (f"I could not find this in {names}. I searched only that document, as asked. "
                    f"To look in every document, say “search all documents”.")
            return ChatAnswer(answer=text, citations=[], scope=scope.documents)

        documents = max(1, len(each))
        messages = build_messages(question, history, passages, self.settings.max_history_turns, scope.documents, reveal,
                                  kind, documents)
        short = kind == summaries.SHORT and bool(passages)
        cap = self.settings.summary_max_tokens + summaries.TOKENS_PER_DOCUMENT * (documents - 1) if short else None
        text = self.model.complete(messages, max_tokens=cap)
        if text and text.lstrip().upper().startswith(CLARIFY_PREFIX):
            return self._ask_back(text, passages, scope, previous_scope)
        if is_not_found(text):
            if scope.source == "previous":
                # An earlier choice must not trap later questions about something else: search everything.
                return self.answer(question, history, selected=[])
            text = strip_citations(text)  # nothing was found, so nothing supports the sentence
        if short and text:
            limit = summaries.SHORT_WORDS if documents == 1 else summaries.WORDS_PER_DOCUMENT * documents
            text = summaries.trim_summary(summaries.strip_offer(text), limit)
        if not text:
            text = "I could not produce an answer. Please try rephrasing the question."
        masked = mask(text, reveal)
        citations = [replace(c, snippet=mask(c.snippet, reveal).text) for c in extract_citations(text, passages)]
        hidden = masked.hidden | (frozenset(removed) if HIDDEN in masked.text else frozenset())
        notes = [hidden_note(hidden)]
        blocked = asked & never
        if blocked and not hidden & blocked:
            notes.append(never_note(blocked))
        if asked - never and not reveal:
            notes.append(REVEAL_DISABLED_NOTE)
        if short:
            notes.append(summaries.MORE_DETAIL)
        answer = "\n\n".join([masked.text, *filter(None, notes)])
        log.info("Answered a question: %d history messages, %d sources, %d cited, scope %s, revealed %s, summary %s",
                 len(history), len(passages), len(citations), f"{len(scope.documents)} document(s)"
                 if scope.documents else "all", ",".join(sorted(reveal)) or "nothing", kind or "no")
        return ChatAnswer(answer=answer, citations=citations, scope=scope.documents, revealed=reveal)


    def _ask_back(self, text: str, passages: Sequence, scope: Scope, previous_scope: Sequence[str]) -> ChatAnswer:
        """Show the model's question. Options are named by company or project; the source number each
        one cites says which document it means, and the conversation remembers that for the reply."""
        ask, options = parse_clarification(text)
        ask = mask(ask or "Which one do you mean?").text
        valid = [(mask(label).text, passages[n - 1].title) for label, n in options if 1 <= n <= len(passages)]
        by_document: dict[str, str] = {}
        for label, title in valid:
            by_document.setdefault(title, label)
        if not scope.documents and len(by_document) > 1:
            documents = list(by_document)[:MAX_CHOICES]
            labels = [by_document[d] for d in documents]
        elif not scope.documents and not valid and len(source_documents(passages)) > 1:
            documents = source_documents(passages)[:MAX_CHOICES]  # the model named no options
            labels = documents
        else:
            # The choice is inside one document (for example two BOQs in one workbook). Stay on it: the
            # reply is read together with this question, by the model.
            shown = list(dict.fromkeys(label for label, _ in valid))
            kept = tuple(scope.documents or by_document or previous_scope)
            return ChatAnswer(answer=_clarification(ask, shown) if shown else ask, citations=[], scope=kept,
                              clarification=True)
        log.info("Asked which one is meant (%d candidate documents)", len(documents))
        return ChatAnswer(answer=_clarification(ask, labels), citations=[], scope=tuple(previous_scope),
                          clarification=True, choices=tuple(documents), choice_labels=tuple(labels))


@dataclass
class SearchSetup:
    """Creates the search index and indexer at startup, retrying while role assignments propagate."""

    client: SearchClient | None
    state: str = "not-applicable"
    detail: str = ""
    _thread: threading.Thread | None = field(default=None, repr=False)

    def start(self, attempts: int = 10, delay_seconds: float = 30.0) -> None:
        if self.client is None:
            return
        self.state = "pending"
        self._thread = threading.Thread(target=self._run, args=(attempts, delay_seconds), daemon=True, name="search-setup")
        self._thread.start()

    def _run(self, attempts: int, delay_seconds: float) -> None:
        for attempt in range(1, attempts + 1):
            try:
                ensure_search_resources(self.client)
                self.state, self.detail = "ready", ""
                # Index files that arrived while the app was down (for example the demo's sample
                # documents, uploaded by the deploy script before the restart).
                try:
                    self.client.run_indexer()
                except ChatbotError as exc:
                    log.warning("Could not start the indexer after setup: %s", exc.message)
                return
            except ChatbotError as exc:
                self.detail = exc.message
            except Exception as exc:  # credential errors and the like
                self.detail = str(exc)
            log.warning("Search setup attempt %d/%d failed: %s", attempt, attempts, self.detail)
            if attempt < attempts:
                time.sleep(delay_seconds)
        self.state = "failed"


@dataclass
class Components:
    chat: ChatService
    documents: DocumentStore
    search: SearchClient | None = None
    conversations: ConversationStore | None = None


def build_components(settings: Settings) -> Components:
    conversations = ConversationStore(settings.chat_history_dir, settings.chat_history_days)
    if settings.is_azure:
        from .azure_auth import SEARCH_SCOPE, bearer_token

        search = SearchClient(settings, lambda: bearer_token(SEARCH_SCOPE))
        documents = BlobDocumentStore(settings)
        chat = ChatService(settings, AzureSearchRetriever(search, documents), AzureChatModel(settings))
        return Components(chat=chat, documents=documents, search=search, conversations=conversations)

    uploads = settings.local_data_dir / "documents"
    chat = ChatService(settings, LocalRetriever([SAMPLE_DOCS_DIR, uploads]), LocalChatModel())
    return Components(chat=chat, documents=LocalDocumentStore(uploads), conversations=conversations)
