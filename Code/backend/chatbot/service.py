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
from .privacy import hidden_note, mask, requested_reveals
from .prompts import Citation, build_messages, extract_citations, select_passages
from .retrieval import AzureSearchRetriever, LocalRetriever, Retriever, build_search_query
from .scope import resolve_scope
from .search import SearchClient, ensure_search_resources

log = logging.getLogger(__name__)


REVEAL_DISABLED_NOTE = "Showing personal or customer information is turned off for this deployment."


@dataclass(frozen=True)
class ChatAnswer:
    answer: str
    citations: list[Citation]
    scope: tuple[str, ...] = ()  # the documents searched; empty means all
    clarification: bool = False  # the answer is a question back to the user; nothing was searched
    revealed: frozenset[str] = frozenset()  # privacy categories shown because the user asked


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
               previous_scope: Sequence[str] = ()) -> ChatAnswer:
        """Answer `question`. `selected` is the document choice in the page (None: no choice sent, [] all
        documents); `previous_scope` is the scope of the conversation so far."""
        scope = resolve_scope(question, self.catalog.names(), selected, previous_scope)
        if scope.clarify:
            log.info("Asked which document is meant (scope unclear)")
            return ChatAnswer(answer=scope.clarify, citations=[], scope=tuple(previous_scope), clarification=True)

        asked = requested_reveals(question)
        reveal = asked if self.settings.allow_sensitive_reveal else frozenset()
        query = build_search_query(question, history)
        found = self.retriever.retrieve(query, self.settings.search_top_k, scope.documents)
        passages = select_passages(found, self.settings.max_source_chars)
        if scope.documents and not passages:
            # Never fall back to other documents: say so and let the user widen the search.
            names = ", ".join(scope.documents)
            text = (f"I could not find this in {names}. I searched only that document, as asked. "
                    f"To look in every document, say “search all documents”.")
            return ChatAnswer(answer=text, citations=[], scope=scope.documents)

        messages = build_messages(question, history, passages, self.settings.max_history_turns, scope.documents, reveal)
        text = self.model.complete(messages)
        if not text:
            text = "I could not produce an answer. Please try rephrasing the question."
        masked = mask(text, reveal)
        citations = [replace(c, snippet=mask(c.snippet, reveal).text) for c in extract_citations(text, passages)]
        notes = [hidden_note(masked.hidden)]
        if asked and not reveal:
            notes.append(REVEAL_DISABLED_NOTE)
        answer = "\n\n".join([masked.text, *filter(None, notes)])
        log.info("Answered a question: %d history messages, %d sources, %d cited, scope %s, revealed %s",
                 len(history), len(passages), len(citations), f"{len(scope.documents)} document(s)"
                 if scope.documents else "all", ",".join(sorted(reveal)) or "nothing")
        return ChatAnswer(answer=answer, citations=citations, scope=scope.documents, revealed=reveal)


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
