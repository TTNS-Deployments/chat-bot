from fastapi.testclient import TestClient

from chatbot.auth import COOKIE_NAME, PasswordGate
from chatbot.config import Settings
from chatbot.main import create_app
from chatbot.retrieval import Passage
from chatbot.service import ChatService, Components
from chatbot.documents import LocalDocumentStore


class OneSource:
    def retrieve(self, query, top):
        return [Passage("c1", "a.md", "Text.")]


class Echo:
    def complete(self, messages):
        return "Answer [1]."


def client(password="Demo-Pass-123", **settings):
    s = Settings(mode="local", auth_mode="password", demo_password=password, rate_limit_per_minute=0, **settings)
    components = Components(chat=ChatService(s, OneSource(), Echo()), documents=LocalDocumentStore(__import__("pathlib").Path("/nonexistent")))
    return TestClient(create_app(s, components, start_search_setup=False), base_url="https://testserver")


def test_pages_and_api_need_sign_in_but_health_does_not():
    c = client()
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/config").json()["authMode"] == "password"
    assert c.get("/login.html").status_code == 200
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login.html"
    r = c.post("/api/chat", json={"message": "hi"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "sign_in_required"


def test_right_password_signs_in_and_out():
    c = client()
    r = c.post("/api/login", json={"password": "Demo-Pass-123"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 200
    assert c.get("/").status_code == 200
    c.post("/api/logout")
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 401


def test_wrong_passwords_are_refused_then_locked():
    c = client()
    codes = [c.post("/api/login", json={"password": "wrong"}).status_code for _ in range(5)]
    assert codes == [403] * 5
    r = c.post("/api/login", json={"password": "Demo-Pass-123"})
    assert r.status_code == 429  # locked even with the right password


def test_missing_password_never_fails_open():
    for password in ("", "@Microsoft.KeyVault(VaultName=kv;SecretName=demo-password)"):
        from chatbot.config import settings_from_env
        s = settings_from_env({"AUTH_MODE": "password", "DEMO_PASSWORD": password})
        c = client(password=s.demo_password)
        r = c.post("/api/login", json={"password": "anything"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "not_configured"
        assert c.post("/api/chat", json={"message": "hi"}).status_code == 401
        assert c.post("/api/login", json={"password": ""}).status_code == 422


def test_forged_or_expired_cookies_are_rejected():
    gate = PasswordGate("secret", session_seconds=60)
    token = gate.issue(now=1000)
    assert gate.valid(token, now=1030)
    assert not gate.valid(token, now=1061)
    expiry, nonce, sig = token.split(".")
    assert not gate.valid(f"{int(expiry) + 999}.{nonce}.{sig}", now=1030)
    assert not PasswordGate("other").valid(token, now=1030)
    assert not gate.valid("garbage", now=1030) and not gate.valid(None)
    c = client()
    c.cookies.set(COOKIE_NAME, "9999999999.deadbeef")
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 401


def test_lockout_expires():
    gate = PasswordGate("secret", max_failures=2, lockout_seconds=900)
    gate.check("ip", "x", now=0)
    gate.check("ip", "y", now=1)
    assert gate.locked("ip", now=2) and not gate.locked("other", now=2)
    assert not gate.locked("ip", now=902)


def test_no_gate_when_auth_mode_none():
    s = Settings(mode="local", rate_limit_per_minute=0)
    c = TestClient(create_app(s, Components(chat=ChatService(s, OneSource(), Echo()), documents=LocalDocumentStore(__import__("pathlib").Path("/x"))), start_search_setup=False))
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 200
    assert c.post("/api/login", json={"password": "x"}).status_code == 404


def test_pages_and_answers_are_not_cached():
    c = client()
    assert c.get("/login.html").headers["cache-control"] == "no-store"
    assert c.post("/api/chat", json={"message": "hi"}).headers["cache-control"] == "no-store"


def test_sign_in_page_assets_are_public():
    c = client()
    for path in ("/login.html", "/login.js", "/app.css", "/favicon.svg", "/ttbs-logo.png"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 200, path
    assert c.get("/ttbs-logo.png").headers["content-type"] == "image/png"
