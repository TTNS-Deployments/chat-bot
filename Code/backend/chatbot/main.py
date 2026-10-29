"""HTTP API and chat page.

    GET    /healthz                 health probe (App Service health check)
    GET    /api/config              title, subtitle and features for the page
    POST   /api/login, /api/logout  demo sign-in with a shared password (AUTH_MODE=password)
    POST   /api/chat                ask a question (with the conversation so far)
    GET    /api/document-names      names of the searchable documents
    GET    /api/history             the stored conversation                 (X-Conversation-Id)
    GET    /api/history/export      download it: ?format=txt|json|docx     (X-Conversation-Id)
    DELETE /api/history             forget the conversation                 (X-Conversation-Id)
    GET    /api/documents           list documents and indexing status
    POST   /api/documents           upload a document (no key needed; behind sign-in when it is on)
    DELETE /api/documents/{name}    remove a document                      (X-Upload-Key, API only)
    GET    /                        the chat page (Code/frontend)
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import SUBTITLE, TITLE, __version__
from .auth import COOKIE_NAME, PUBLIC_PATHS, PasswordGate
from .config import FRONTEND_DIR, Settings, get_settings
from .documents import safe_name
from .errors import ChatbotError, NotConfigured, UpstreamError
from .history import EXPORT_FORMATS, ConversationStore, export, export_command, now_iso
from .privacy import MaskingFilter
from .service import Components, SearchSetup, build_components

log = logging.getLogger("chatbot")


# --------------------------------------------------------------------------------------
# Request and response models
# --------------------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000, description="The user's question.")
    history: list[ChatMessage] = Field(default_factory=list, max_length=40,
                                       description="Earlier messages of this conversation, oldest first.")
    conversationId: str | None = Field(default=None, max_length=64,
                                       description="From an earlier response; omit to start a stored conversation.")
    documents: list[str] | None = Field(default=None, max_length=20,
                                        description="Documents selected in the page: [] for all documents; omit to "
                                                    "keep the conversation's current scope.")

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message must not be blank")
        return value.strip()


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class CitationOut(BaseModel):
    id: int
    title: str
    snippet: str
    section: str = ""


class ChatAction(BaseModel):
    type: Literal["download"]
    format: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[CitationOut]
    conversationId: str | None = None
    scope: list[str] = Field(default_factory=list, description="Documents searched for this answer; empty means all.")
    action: ChatAction | None = Field(default=None, description="Set when the message asked to download the chat history.")


# --------------------------------------------------------------------------------------
# Rate limiting (per client IP, per instance)
# --------------------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_minute: int, max_clients: int = 10000):
        self.per_minute = per_minute
        self.max_clients = max_clients
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, client: str, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._hits) >= self.max_clients and client not in self._hits:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
            hits = self._hits.setdefault(client, deque())
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            return True


def client_ip(request: Request, trust_forwarded_for: bool = False) -> str:
    """The address rate limits and the sign-in lockout are keyed on.

    X-Forwarded-For is only used behind a trusted proxy (App Service, or TRUST_FORWARDED_FOR=true), and
    then only its last entry: that one is added by the proxy itself, while earlier entries come from the
    client and can be forged.
    """
    forwarded = request.headers.get("x-forwarded-for", "") if trust_forwarded_for else ""
    if forwarded.strip(", "):
        last = forwarded.rstrip(", ").split(",")[-1].strip()
        # App Service appends the port to IPv4 addresses (1.2.3.4:5678).
        return last.rsplit(":", 1)[0] if last.count(":") == 1 else last
    return request.client.host if request.client else "unknown"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _configure_telemetry() -> None:
    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor

            configure_azure_monitor(logger_name="chatbot")
        except Exception as exc:  # telemetry must never stop the app
            log.warning("Application Insights is not configured: %s", exc)


# --------------------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------------------
def create_app(settings: Settings | None = None, components: Components | None = None,
               start_search_setup: bool = True) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _configure_telemetry()
    for handler in logging.getLogger().handlers:  # personal information stays out of the logs
        if not any(isinstance(f, MaskingFilter) for f in handler.filters):
            handler.addFilter(MaskingFilter())

    if settings.auth_mode not in ("none", "password"):
        raise RuntimeError(f"AUTH_MODE must be 'none' or 'password', not {settings.auth_mode!r}")
    if settings.is_azure and settings.missing_azure_settings():
        missing = ", ".join(settings.missing_azure_settings())
        raise RuntimeError(f"CHATBOT_MODE=azure but these settings are missing: {missing}")

    components = components or build_components(settings)
    conversations = components.conversations or ConversationStore(settings.chat_history_dir, settings.chat_history_days)
    setup = SearchSetup(components.search)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    gate = PasswordGate(settings.demo_password) if settings.auth_mode == "password" else None

    def caller(request: Request) -> str:
        return client_ip(request, settings.trust_forwarded_for)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_search_setup:
            setup.start()
        yield

    app = FastAPI(title=TITLE, description=SUBTITLE, version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.components = components
    app.state.search_setup = setup

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        path = request.url.path
        if gate is None or path in PUBLIC_PATHS or gate.valid(request.cookies.get(COOKIE_NAME)):
            return await call_next(request)
        if path.startswith("/api/"):
            return _error(401, "sign_in_required", "Please sign in.")
        return RedirectResponse("/login.html", status_code=303)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith(".html") or path.startswith("/api/"):
            # Pages and answers must not be served from the browser cache (for example after sign-out).
            response.headers.setdefault("Cache-Control", "no-store")
        else:
            # Scripts and styles are revalidated (ETag) so a new release never runs old app.js against new HTML.
            response.headers.setdefault("Cache-Control", "no-cache")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/api/docs"):
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
        return response

    @app.exception_handler(ChatbotError)
    async def chatbot_error(_: Request, exc: ChatbotError):
        if isinstance(exc, UpstreamError):
            log.error("Upstream error: %s", exc.message)
            return _error(exc.status_code, exc.code, "The service could not answer right now. Please try again shortly.")
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid value')}" if where else first.get("msg", "Invalid request")
        return _error(422, "invalid_request", message)

    @app.exception_handler(Exception)
    async def unexpected_error(_: Request, exc: Exception):
        log.exception("Unhandled error", exc_info=exc)
        return _error(500, "internal_error", "Something went wrong. Please try again.")

    # ---- health and configuration ----
    @app.get("/healthz", tags=["health"])
    def healthz():
        return {"status": "ok", "version": __version__, "mode": settings.mode,
                "searchSetup": setup.state, **({"searchSetupDetail": setup.detail} if setup.state == "failed" else {})}

    @app.get("/api/config", tags=["chat"])
    def config():
        return {"title": TITLE, "subtitle": SUBTITLE, "mode": settings.mode, "authMode": settings.auth_mode,
                "uploadEnabled": settings.allow_document_upload, "maxUploadMb": settings.max_upload_mb}

    # ---- demo sign-in ----
    @app.post("/api/login", tags=["sign-in"])
    def login(body: LoginRequest, request: Request):
        if gate is None:
            return _error(404, "not_enabled", "Sign-in is not enabled for this deployment.")
        if not gate.configured:
            raise NotConfigured("Sign-in is not configured yet: the demo password is missing or not readable from Key Vault.")
        client = caller(request)
        if gate.locked(client):
            return _error(429, "locked", "Too many wrong passwords. Try again in 15 minutes.")
        if not gate.check(client, body.password):
            return _error(403, "invalid_password", "The password is not correct.")
        response = JSONResponse({"signedIn": True})
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(COOKIE_NAME, gate.issue(), max_age=gate.session_seconds, httponly=True,
                            secure=https, samesite="lax", path="/")
        return response

    @app.post("/api/logout", tags=["sign-in"])
    def logout(request: Request):
        if gate is not None:
            gate.revoke(request.cookies.get(COOKIE_NAME))
        response = JSONResponse({"signedIn": False})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # ---- chat ----
    @app.post("/api/chat", response_model=ChatResponse, tags=["chat"],
              responses={422: {"description": "Invalid request"}, 429: {"description": "Too many requests"},
                         502: {"description": "Upstream failure"}, 503: {"description": "Service busy"}})
    def chat(body: ChatRequest, request: Request):
        if not limiter.allow(caller(request)):
            return _error(429, "rate_limited", "Too many questions in a short time. Please wait a minute.")
        conversation_id = body.conversationId if conversations.valid_id(body.conversationId) else conversations.new_id()
        previous_scope = conversations.scope(conversation_id)
        asked = now_iso()

        if (fmt := export_command(body.message)) is not None:
            text = f"Your chat history is being downloaded as a .{fmt} file. Personal information stays hidden unless you asked to see it."
            conversations.append(conversation_id, [
                {"role": "user", "content": body.message, "time": asked},
                {"role": "assistant", "content": text}], previous_scope)
            return ChatResponse(answer=text, citations=[], conversationId=conversation_id, scope=previous_scope,
                                action=ChatAction(type="download", format=fmt))

        result = components.chat.answer(body.message, [m.model_dump() for m in body.history],
                                        selected=body.documents, previous_scope=previous_scope)
        citations = [CitationOut(id=c.id, title=c.title, snippet=c.snippet, section=c.section) for c in result.citations]
        conversations.append(conversation_id, [
            {"role": "user", "content": body.message, "time": asked},
            {"role": "assistant", "content": result.answer, "citations": [c.model_dump() for c in citations],
             "scope": list(result.scope), "revealed": sorted(result.revealed)}], result.scope)
        return ChatResponse(answer=result.answer, citations=citations, conversationId=conversation_id,
                            scope=list(result.scope))

    @app.get("/api/document-names", tags=["chat"])
    def document_names():
        return {"documents": components.chat.catalog.names()}

    # ---- chat history ----
    def stored_conversation(conversation_id: str | None) -> dict | JSONResponse:
        conversation = conversations.load(conversation_id)
        if conversation is None:
            return _error(404, "no_history", "There is no saved chat history for this conversation yet.")
        return conversation

    @app.get("/api/history", tags=["chat history"])
    def history(x_conversation_id: str | None = Header(default=None)):
        conversation = stored_conversation(x_conversation_id)
        if isinstance(conversation, JSONResponse):
            return conversation
        return JSONResponse(json.loads(export(conversation, "json")[0]))

    @app.get("/api/history/export", tags=["chat history"])
    def export_history(format: str = "txt", x_conversation_id: str | None = Header(default=None)):
        fmt = format.lower().lstrip(".")
        if fmt not in EXPORT_FORMATS:
            return _error(422, "invalid_format", f"Choose one of: {', '.join(EXPORT_FORMATS)}.")
        conversation = stored_conversation(x_conversation_id)
        if isinstance(conversation, JSONResponse):
            return conversation
        content, media_type, filename = export(conversation, fmt)
        return Response(content, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.delete("/api/history", tags=["chat history"])
    def delete_history(x_conversation_id: str | None = Header(default=None)):
        return {"deleted": conversations.delete(x_conversation_id)}

    # ---- documents ----
    # Adding documents needs no key: anyone who can use the chat can add documents (after sign-in when
    # AUTH_MODE=password). The web app writes to Storage with its managed identity, and the search
    # service creates embeddings with its own, so no key ever reaches the browser.
    def check_upload_enabled() -> JSONResponse | None:
        if not settings.allow_document_upload:
            return _error(403, "upload_disabled", "Adding documents is turned off for this deployment.")
        return None

    def check_delete_key(provided: str | None) -> JSONResponse | None:
        """Deleting stays an administrator task (the page has no delete button): it needs UPLOAD_API_KEY."""
        if (denied := check_upload_enabled()) is not None:
            return denied
        expected = settings.upload_api_key
        if not expected or expected.startswith("@Microsoft.KeyVault("):
            if settings.is_azure:
                raise NotConfigured("The administrator key is not available yet (Key Vault reference not resolved). "
                                    "Restart the web app in a few minutes.")
            return None  # local mode without a key
        if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
            return _error(401, "invalid_key", "Deleting documents needs the administrator key (X-Upload-Key header).")
        return None

    @app.get("/api/documents", tags=["documents"])
    def list_documents():
        if (denied := check_upload_enabled()) is not None:
            return denied
        docs = [{"name": d.name, "size": d.size, "lastModified": d.last_modified} for d in components.documents.list()]
        indexer = components.search.indexer_status() if components.search else {"status": "local"}
        return {"documents": docs, "indexer": indexer}

    @app.post("/api/documents", status_code=201, tags=["documents"])
    async def upload_document(request: Request, file: UploadFile = File(...)):
        if (denied := check_upload_enabled()) is not None:
            return denied
        if not limiter.allow(caller(request)):
            return _error(429, "rate_limited", "Too many requests in a short time. Please wait a minute.")
        try:
            name = safe_name(file.filename or "")
        except ValueError as exc:
            return _error(415, "unsupported_file", str(exc))
        limit = settings.max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            return _error(413, "file_too_large", f"The file is larger than {settings.max_upload_mb} MB.")
        if not data:
            return _error(422, "empty_file", "The file is empty.")
        try:
            stored = components.documents.save(name, data, file.content_type)
        except Exception as exc:  # storage unreachable, missing role assignment and the like
            log.error("Could not store document %s: %s", name, exc)
            return _error(502, "upload_failed", "The document could not be stored right now. Please try again in a few minutes.")
        components.chat.catalog.invalidate()
        indexing = "local"
        if components.search:
            try:
                indexing = "started" if components.search.run_indexer() else "queued"
            except ChatbotError as exc:  # the scheduled indexer run picks the file up anyway
                log.warning("Stored %s but could not start the indexer: %s", name, exc.message)
                indexing = "scheduled"
        log.info("Stored document %s (%d bytes); indexing %s", stored.name, stored.size, indexing)
        return {"name": stored.name, "size": stored.size, "indexing": indexing}

    @app.delete("/api/documents/{name}", tags=["documents"])
    def delete_document(name: str, x_upload_key: str | None = Header(default=None)):
        if (denied := check_delete_key(x_upload_key)) is not None:
            return denied
        try:
            name = safe_name(name)
        except ValueError as exc:
            return _error(422, "invalid_name", str(exc))
        if not components.documents.delete(name):
            return _error(404, "not_found", f"No document named {name}.")
        components.chat.catalog.invalidate()
        if components.search:
            components.search.run_indexer()  # removes the deleted file's chunks (soft-delete detection)
        return {"deleted": name}

    if FRONTEND_DIR.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
    return app


app = create_app()
