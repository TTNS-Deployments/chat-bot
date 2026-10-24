"""Tests against real uvicorn processes started with the documented command, plus repo hygiene checks."""
from __future__ import annotations

import concurrent.futures as cf
import re
import shutil
import statistics
import subprocess
import time

import httpx
import pytest

from conftest import PY, REPO, BACKEND, start_server


def test_BE_001_existing_suite(record):
    cmd = [str(PY), "-m", "pytest", "Code/backend", "-q", "-p", "no:cacheprovider"]
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=600)
    tail = p.stdout.strip().splitlines()[-1]
    (REPO / "qa" / "evidence").mkdir(exist_ok=True)
    (REPO / "qa" / "evidence" / "BE-001_existing_pytest.txt").write_text(p.stdout[-5000:] + p.stderr[-2000:])
    record(actual=f"exit {p.returncode}: {tail}", evidence="make test  ==  .venv/bin/python -m pytest Code/backend -q")
    assert p.returncode == 0


def test_BE_002_starts_with_documented_config(local_server, record):
    h = httpx.get(f"{local_server.url}/healthz").json()
    log = local_server.log()
    bad = [l for l in log.splitlines() if re.search(r"ERROR|Traceback|WARNING", l)]
    record(actual=f"healthy after {local_server.startup_s}s: {h}; error/warning log lines: {bad[:3]}", evidence=local_server.cmd)
    assert local_server.started and h["status"] == "ok" and not bad


def test_BE_003_azure_mode_missing_settings(qa_tmp, record):
    srv = start_server(qa_tmp, "missing-config", wait_s=15, CHATBOT_MODE="azure")
    try:
        srv.proc.wait(15)
    except subprocess.TimeoutExpired:
        pass
    code = srv.proc.poll()
    log = srv.log()
    line = next((l for l in log.splitlines() if l.startswith("RuntimeError:") and "settings are missing" in l), "")
    record(actual=f"exit code={code}; message: {line.strip()[:250]}; traceback printed={'Traceback' in log}", evidence=srv.cmd)
    srv.stop()
    assert code not in (None, 0) and "AZURE_OPENAI_ENDPOINT" in line and "AZURE_SEARCH_ENDPOINT" in line


def test_BE_004_invalid_config_values(qa_tmp, record):
    srv = start_server(qa_tmp, "invalid-values", CHATBOT_MODE="bogus", RATE_LIMIT_PER_MINUTE="abc", MAX_UPLOAD_MB="lots",
                       SEARCH_TOP_K="-5", MAX_HISTORY_TURNS="x", ALLOW_DOCUMENT_UPLOAD="maybe")
    try:
        cfg = httpx.get(f"{srv.url}/api/config").json() if srv.started else {}
        chat = httpx.post(f"{srv.url}/api/chat", json={"message": "warranty"}).status_code if srv.started else None
        record(actual=f"started={srv.started}; config={cfg}; chat={chat}; invalid values silently replaced by defaults (no warning in log: "
                      f"{'warn' not in srv.log().lower()})", evidence=srv.cmd)
        assert srv.started and chat == 200
    finally:
        srv.stop()


def test_BE_005_unrecognised_auth_mode_fails_closed(qa_tmp, record):
    srv = start_server(qa_tmp, "auth-typo", wait_s=15, AUTH_MODE="pasword", DEMO_PASSWORD="qa-test-only-pw")
    try:
        if not srv.started:
            try:
                srv.proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
            line = next((l for l in srv.log().splitlines() if l.startswith("RuntimeError:")), "")
            record(actual=f"app refused to start: exit code={srv.proc.poll()}; {line.strip()[:200]}", evidence=srv.cmd)
            assert srv.proc.poll() not in (None, 0) and "AUTH_MODE" in line
            return
        cfg = httpx.get(f"{srv.url}/api/config").json()
        chat = httpx.post(f"{srv.url}/api/chat", json={"message": "warranty"}).status_code
        page = httpx.get(f"{srv.url}/", follow_redirects=False).status_code
        record(actual=f"started={srv.started}; authMode reported={cfg.get('authMode')!r}; unauthenticated chat={chat}; GET / = {page} "
                      f"(chat page and API open without sign-in, Sign out button hidden)", evidence=srv.cmd)
        assert not srv.started or chat == 401, "AUTH_MODE typo silently disables sign-in (fails open)"
    finally:
        srv.stop()


def test_BE_052_pii_and_secrets_not_logged(local_server, password_server, demo_password, record):
    pii = ["priya.sharma@example.com", "4111 1111 1111 1111", "+91 98765 43210", "ABCDE1234F"]
    msg = f"My email is {pii[0]}, card {pii[1]}, phone {pii[2]}, PAN {pii[3]}. What does the warranty cover?"
    r = httpx.post(f"{local_server.url}/api/chat", json={"message": msg, "history": [{"role": "user", "content": msg}, {"role": "assistant", "content": "ok"}]})
    httpx.post(f"{password_server.url}/api/login", json={"password": "qa-wrong-password-should-not-be-logged"})
    httpx.post(f"{password_server.url}/api/login", json={"password": demo_password})
    time.sleep(0.5)
    logs = local_server.log() + password_server.log()
    found = [p for p in pii + ["qa-wrong-password-should-not-be-logged", demo_password] if p in logs]
    sample = [l for l in local_server.log().splitlines() if "Answered" in l][-1:]
    record(actual=f"chat {r.status_code}; sensitive strings found in server logs: {len(found)}; example log line: {sample}")
    assert r.status_code == 200 and not found


SECRET_PATTERNS = {
    "OpenAI key": r"sk-[A-Za-z0-9_-]{20,}", "AWS key": r"AKIA[0-9A-Z]{16}", "Storage key": r"AccountKey=[A-Za-z0-9+/=]{20,}",
    "Private key": r"BEGIN [A-Z ]*PRIVATE KEY", "GitHub token": r"gh[pousr]_[A-Za-z0-9]{30,}", "Slack token": r"xox[baprs]-[A-Za-z0-9-]+",
    "SAS signature": r"sig=[A-Za-z0-9%]{30,}", "JWT": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.",
    "Azure key (32 hex)": r"(?i)(api[-_]?key|key)[\"' ]*[:=][\"' ]*[0-9a-f]{32}\b",
    "Assigned password/secret": r"(?i)(password|secret|api_?key|token)[\"']?\s*[:=]\s*[\"'][^\"'\s<>{}$]{8,}[\"']",
}


def test_BE_053_no_hardcoded_secrets(record):
    files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split()
    hits = []
    for f in files:
        if f.endswith((".pdf", ".png", ".svg", ".zip", ".xlsx")) or not (REPO / f).is_file():
            continue
        text = (REPO / f).read_text(errors="ignore")
        for name, pat in SECRET_PATTERNS.items():
            for m in re.finditer(pat, text):
                if re.search(r"[<>\[]|steps\(|your|choose|example|placeholder", m.group(0), re.I):
                    continue  # documentation placeholder or ARM template expression, not a value
                line = text.count("\n", 0, m.start()) + 1
                hits.append((f, line, name))
    fixture_dirs = ("Code/backend/tests/", "qa/")  # unit tests and this QA suite use dummy values only
    real = [h for h in hits if not h[0].startswith(fixture_dirs)]
    record(actual=f"{len(files)} tracked files scanned; matches outside unit tests: {real}; matches in unit-test fixtures (dummy values): "
                  f"{[(f, l) for f, l, _ in hits if f.startswith(fixture_dirs)]}")
    assert not real


def test_BE_054_env_ignored(record):
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=REPO).returncode == 0
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=REPO, capture_output=True).returncode == 0
    history = subprocess.run(["git", "log", "--all", "--oneline", "--", ".env"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    record(actual=f".env ignored={ignored}; tracked={tracked}; ever committed={bool(history)}")
    assert ignored and not tracked and not history


def test_BE_055_sequential_latency(local_server, record):
    times = []
    with httpx.Client(base_url=local_server.url, timeout=30) as c:
        for q in ["What does the warranty cover?", "How do I return a product?", "When is customer support available?"] * 4:
            t0 = time.perf_counter()
            assert c.post("/api/chat", json={"message": q}).status_code == 200
            times.append(time.perf_counter() - t0)
    record(actual=f"12 sequential requests (mock LLM): median {statistics.median(times) * 1000:.1f} ms, max {max(times) * 1000:.1f} ms")
    assert max(times) < 2.0


def test_BE_056_concurrency(local_server, record):
    def one(i):
        t0 = time.perf_counter()
        r = httpx.post(f"{local_server.url}/api/chat", json={"message": f"How do I return a product? #{i}"}, timeout=30)
        return r.status_code, time.perf_counter() - t0, bool(r.json().get("answer"))

    for n in (10, 50):
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(n) as ex:
            res = list(ex.map(one, range(n)))
        wall = time.perf_counter() - t0
        codes = [r[0] for r in res]
        record(actual=f"{n} simultaneous: statuses {sorted(set(codes))}, all answered={all(r[2] for r in res)}, wall {wall:.2f}s, slowest {max(r[1] for r in res):.2f}s")
        assert codes == [200] * n and all(r[2] for r in res)
    assert local_server.proc.poll() is None


def test_BE_061_cli_ask(qa_tmp, record):
    env = {"CHATBOT_MODE": "local", "LOCAL_DATA_DIR": str(qa_tmp / "cli"), "PATH": "/usr/bin:/bin"}
    p = subprocess.run([str(PY), "-m", "chatbot", "ask", "What does the warranty cover?"], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=60)
    record(actual=f"exit {p.returncode}: {p.stdout.strip()[:200]!r}", evidence='cd Code/backend && python -m chatbot ask "What does the warranty cover?"')
    assert p.returncode == 0 and "[1]" in p.stdout and "warranty-policy.md" in p.stdout


@pytest.mark.skipif(not shutil.which("docker"), reason="docker not installed")
def test_BE_062_docker_image(record):
    tag = "qa-ai-based-chatbot:test"
    b = subprocess.run(["docker", "build", "-q", "-t", tag, "."], cwd=REPO, capture_output=True, text=True, timeout=900)
    if b.returncode != 0:
        record(actual=f"docker build exit {b.returncode}: {b.stderr[-300:]}")
        assert b.returncode == 0
    port = __import__("conftest").free_port()
    run = subprocess.run(["docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:8000", tag], capture_output=True, text=True)
    cid = run.stdout.strip()
    try:
        ok, body, chat = False, "", None
        for _ in range(60):
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1)
                ok, body = r.status_code == 200, r.text
                if ok:
                    chat = httpx.post(f"http://127.0.0.1:{port}/api/chat", json={"message": "What does the warranty cover?"}, timeout=10).status_code
                    break
            except httpx.HTTPError:
                time.sleep(1)
        logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True).stdout[-300:] if cid else run.stderr
        record(actual=f"build ok; container healthz={ok} {body}; chat={chat}", evidence=f"docker build -t {tag} . && docker run -p {port}:8000 {tag}")
        assert ok and chat == 200, logs
    finally:
        if cid:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
