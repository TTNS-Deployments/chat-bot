"""Shared QA fixtures: in-process apps, live uvicorn servers, result recording.

Every test function is named test_<LAYER>_<NNN>_..., e.g. test_BE_009_chat_valid -> test ID BE-009.
Outcomes plus the `record(...)` notes are written to qa/results/<module>.json and used to build the
report. A skip whose reason starts with "BLOCKED" is reported as Blocked; other skips as Not Tested.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "Code" / "backend"
PY = REPO / ".venv" / "bin" / "python"
RESULTS = REPO / "qa" / "results"
sys.path.insert(0, str(BACKEND))

# Never let a developer's real Azure settings leak into a test server.
_SCRUB = ("AZURE_", "CHATBOT_", "AUTH_MODE", "DEMO_PASSWORD", "UPLOAD_API_KEY", "APPLICATIONINSIGHTS",
          "RATE_LIMIT", "MAX_UPLOAD", "ALLOW_DOCUMENT", "LOCAL_DATA_DIR", "OPENAI")


def test_id(name: str) -> str:
    m = re.match(r"test_([A-Z]{2})_(\d{3})", name)
    return f"{m[1]}-{m[2]}" if m else name


# ------------------------------------------------------------------ result recording
_results: dict[str, dict[str, dict]] = {}
_RANK = {"Pass": 0, "Not Tested": 1, "Blocked": 2, "Fail": 3}


@pytest.fixture
def record(request):
    """record(actual="...", evidence="...") – attach observations to the current test."""
    def _record(**kw):
        for k, v in kw.items():
            request.node.user_properties.append((k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)))
    return _record


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    tid = test_id(item.name)
    mod = _results.setdefault(Path(item.fspath).stem, {})
    entry = mod.setdefault(tid, {"id": tid, "tests": [], "status": "Pass", "props": {}, "message": "", "duration_s": 0.0})
    if item.nodeid not in entry["tests"]:
        entry["tests"].append(item.nodeid)
    status = None
    if rep.skipped:
        reason = rep.longrepr[2] if isinstance(rep.longrepr, tuple) else str(rep.longrepr)
        reason = reason.removeprefix("Skipped: ")
        status = "Blocked" if reason.startswith("BLOCKED") else "Not Tested"
        entry["message"] = reason
    elif rep.failed:
        status = "Fail"
        crash = getattr(rep.longrepr, "reprcrash", None)
        msg = crash.message if crash else str(rep.longrepr)
        entry["message"] = (("[setup error] " if rep.when != "call" else "") + msg)[:1500]
    elif rep.when == "call":
        status = "Pass"
    if rep.when == "call":
        entry["duration_s"] += round(rep.duration, 3)
    if status and _RANK[status] >= _RANK[entry["status"]]:
        entry["status"] = status
    for k, v in item.user_properties:
        entry["props"][k] = v if k not in entry["props"] else entry["props"][k] + " | " + v
    item.user_properties.clear()
    return rep


def pytest_sessionfinish(session, exitstatus):
    RESULTS.mkdir(parents=True, exist_ok=True)
    for module, entries in _results.items():
        (RESULTS / f"{module}.json").write_text(json.dumps(list(entries.values()), indent=2, ensure_ascii=False))


# ------------------------------------------------------------------ in-process apps
def local_settings(data_dir: Path, **env):
    from chatbot.config import settings_from_env

    base = {"CHATBOT_MODE": "local", "LOCAL_DATA_DIR": str(data_dir), "RATE_LIMIT_PER_MINUTE": "0"}
    base.update({k: str(v) for k, v in env.items()})
    return settings_from_env(base)


def make_client(data_dir: Path, components=None, **env):
    from fastapi.testclient import TestClient

    from chatbot.main import create_app

    app = create_app(local_settings(data_dir, **env), components=components, start_search_setup=False)
    return TestClient(app, raise_server_exceptions=False)


class CapturingModel:
    """Stand-in chat model that records the messages the backend would send to the LLM."""

    def __init__(self, reply="Refunds take 5 days [1]."):
        self.reply = reply
        self.calls: list[list[dict]] = []

    def complete(self, messages):
        self.calls.append([dict(m) for m in messages])
        return self.reply


class StaticRetriever:
    def __init__(self):
        self.queries: list[str] = []

    def retrieve(self, query, top):
        from chatbot.retrieval import Passage

        self.queries.append(query)
        return [Passage("c1", "policy.md", "Refunds take 5 days."), Passage("c2", "faq.md", "Support is open 9-5.")]


def components_with(model, data_dir: Path, retriever=None, settings=None):
    from chatbot.documents import LocalDocumentStore
    from chatbot.service import ChatService, Components

    settings = settings or local_settings(data_dir)
    return Components(chat=ChatService(settings, retriever or StaticRetriever(), model),
                      documents=LocalDocumentStore(data_dir / "documents"))


# ------------------------------------------------------------------ live servers
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Server:
    url: str
    proc: subprocess.Popen
    log_path: Path
    started: bool
    startup_s: float
    cmd: str

    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def start_server(tmp: Path, name: str, wait_s: float = 25.0, **env) -> Server:
    """Start the app exactly as documented: `cd Code/backend && python -m uvicorn chatbot.main:app --port N`."""
    port = free_port()
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB)}
    full_env.update({"CHATBOT_MODE": "local", "LOCAL_DATA_DIR": str(tmp / f"{name}-data"), "PYTHONUNBUFFERED": "1"})
    full_env.update({k: str(v) for k, v in env.items()})
    log_path = tmp / f"{name}.log"
    cmd = [str(PY), "-m", "uvicorn", "chatbot.main:app", "--host", "127.0.0.1", "--port", str(port)]
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=BACKEND, env=full_env, stdout=log_path.open("w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    started = False
    while time.monotonic() - t0 < wait_s and proc.poll() is None:
        try:
            if httpx.get(f"{url}/healthz", timeout=1).status_code == 200:
                started = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    shown_env = " ".join(f"{k}={'<test-value>' if k in ('DEMO_PASSWORD', 'UPLOAD_API_KEY') else v}" for k, v in env.items())
    return Server(url, proc, log_path, started, round(time.monotonic() - t0, 2),
                  f"cd Code/backend && {shown_env} .venv/bin/python -m uvicorn chatbot.main:app --port {port}".strip())


@pytest.fixture(scope="session")
def qa_tmp(tmp_path_factory):
    return tmp_path_factory.mktemp("qa")


@pytest.fixture(scope="session")
def local_server(qa_tmp):
    """Local (mock LLM) mode, rate limit off so UI/perf tests are not throttled."""
    srv = start_server(qa_tmp, "local", RATE_LIMIT_PER_MINUTE=0)
    assert srv.started, f"server did not start:\n{srv.log()[-2000:]}"
    yield srv
    srv.stop()


@pytest.fixture(scope="session")
def demo_password():
    return "qa-" + secrets.token_urlsafe(12)  # test-only value, never printed


@pytest.fixture(scope="session")
def password_server(qa_tmp, demo_password):
    srv = start_server(qa_tmp, "password", AUTH_MODE="password", DEMO_PASSWORD=demo_password, RATE_LIMIT_PER_MINUTE=0)
    assert srv.started, f"server did not start:\n{srv.log()[-2000:]}"
    yield srv
    srv.stop()
