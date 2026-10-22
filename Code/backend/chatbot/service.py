"""Wires retrieval, prompt building and the chat model together."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

from .config import SAMPLE_DOCS_DIR, Settings
from .documents import BlobDocumentStore, DocumentStore, LocalDocumentStore
from .errors import ChatbotError
from .llm import AzureChatModel, ChatModel, LocalChatModel
from .prompts import Citation, build_messages, extract_citations, select_passages
from .retrieval import AzureSearchRetriever, LocalRetriever, Retriever, build_search_query
from .search import SearchClient, ensure_search_resources

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatAnswer:
    answer: str
    citations: list[Citation]


class ChatService:
    def __init__(self, settings: Settings, retriever: Retriever, model: ChatModel):
        self.settings = settings
        self.retriever = retriever
        self.model = model

    def answer(self, question: str, history: Sequence[dict]) -> ChatAnswer:
        query = build_search_query(question, history)
        passages = select_passages(self.retriever.retrieve(query, self.settings.search_top_k), self.settings.max_source_chars)
        messages = build_messages(question, history, passages, self.settings.max_history_turns)
        text = self.model.complete(messages)
        if not text:
            text = "I could not produce an answer. Please try rephrasing the question."
        log.info("Answered a question: %d history messages, %d sources, %d cited",
                 len(history), len(passages), len(extract_citations(text, passages)))
        return ChatAnswer(answer=text, citations=extract_citations(text, passages))


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


def build_components(settings: Settings) -> Components:
    if settings.is_azure:
        from .azure_auth import SEARCH_SCOPE, bearer_token

        search = SearchClient(settings, lambda: bearer_token(SEARCH_SCOPE))
        chat = ChatService(settings, AzureSearchRetriever(search), AzureChatModel(settings))
        return Components(chat=chat, documents=BlobDocumentStore(settings), search=search)

    uploads = settings.local_data_dir / "documents"
    chat = ChatService(settings, LocalRetriever([SAMPLE_DOCS_DIR, uploads]), LocalChatModel())
    return Components(chat=chat, documents=LocalDocumentStore(uploads))
