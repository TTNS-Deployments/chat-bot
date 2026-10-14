"""Errors the API turns into clear HTTP responses."""
from __future__ import annotations


class ChatbotError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class UpstreamBusy(ChatbotError):
    status_code = 503
    code = "busy"


class UpstreamError(ChatbotError):
    status_code = 502
    code = "upstream_error"


class ContentFiltered(ChatbotError):
    status_code = 400
    code = "content_filtered"


class NotConfigured(ChatbotError):
    status_code = 503
    code = "not_configured"
