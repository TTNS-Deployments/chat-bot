"""Demo sign-in with one shared password (AUTH_MODE=password).

For demos where a Microsoft Entra ID app registration is not available. A correct password sets a
signed session cookie. The signing key is derived from the password itself, so every worker and
instance accepts the same cookie without extra configuration, and changing the password signs
everyone out. If no password is configured, sign-in is refused: the gate never fails open.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

COOKIE_NAME = "chatbot_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60

# Reachable without a session: the health probe and everything the sign-in page needs.
PUBLIC_PATHS = frozenset({"/healthz", "/login.html", "/login.js", "/app.css", "/favicon.svg",
                          "/api/login", "/api/logout", "/api/config"})


class PasswordGate:
    def __init__(self, password: str, max_failures: int = MAX_FAILURES, lockout_seconds: int = LOCKOUT_SECONDS,
                 session_seconds: int = SESSION_SECONDS):
        self._password = password
        self._key = hashlib.sha256(f"chatbot-session:{password}".encode()).digest()
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self.session_seconds = session_seconds
        self._failures: dict[str, deque] = {}
        self._revoked: dict[str, int] = {}  # signature -> expiry, for tokens signed out before they expire
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._password)

    # ---- password check with lockout (per client IP, per worker) ----
    def _recent(self, client: str, now: float) -> deque:
        failures = self._failures.setdefault(client, deque())
        while failures and now - failures[0] >= self.lockout_seconds:
            failures.popleft()
        return failures

    def locked(self, client: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            return len(self._recent(client, now)) >= self.max_failures

    def check(self, client: str, password: str, now: float | None = None) -> bool:
        """True for the right password. Wrong ones count towards the lockout."""
        now = time.monotonic() if now is None else now
        if self.configured and hmac.compare_digest(password.encode(), self._password.encode()):
            with self._lock:
                self._failures.pop(client, None)
            return True
        with self._lock:
            if len(self._failures) > 10000:
                self._failures = {k: v for k, v in self._failures.items() if v and now - v[-1] < self.lockout_seconds}
            self._recent(client, now).append(now)
        return False

    # ---- session cookie: "<expiry>.<nonce>.<signature>" ----
    # The nonce makes every sign-in a distinct token, so signing out revokes only that session.
    # Revocation is kept in memory, per worker: with several workers or instances a signed-out token
    # stays valid on the others until it expires (at most SESSION_SECONDS).
    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self, now: float | None = None) -> str:
        expiry = str(int((time.time() if now is None else now) + self.session_seconds))
        payload = f"{expiry}.{secrets.token_hex(8)}"
        return f"{payload}.{self._sign(payload)}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not token or not self.configured or token.count(".") != 2:
            return False
        expiry, _, signature = token.split(".")
        if not expiry.isdigit() or int(expiry) < (time.time() if now is None else now):
            return False
        if not hmac.compare_digest(signature, self._sign(token.rsplit(".", 1)[0])):
            return False
        with self._lock:
            return signature not in self._revoked

    def revoke(self, token: str | None, now: float | None = None) -> None:
        """Sign a session out on the server (called by /api/logout)."""
        if not self.valid(token, now):
            return
        now = time.time() if now is None else now
        expiry, _, signature = token.split(".")
        with self._lock:
            self._revoked = {s: e for s, e in self._revoked.items() if e >= now}  # forget expired entries
            self._revoked[signature] = int(expiry)
