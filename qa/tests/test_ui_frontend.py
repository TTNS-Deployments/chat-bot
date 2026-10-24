"""UI (user-visible behaviour) and front-end technical checks, driven with Playwright + headless Chromium
against a real uvicorn server in local (mock LLM) mode. Some tests replace only the /api/chat response
(route.fulfill) to control what the "bot" says, delay it, or simulate backend failures."""
from __future__ import annotations

import json
import re
import subprocess

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

from conftest import PY, REPO

AXE = REPO / "qa" / "node_modules" / "axe-core" / "axe.min.js"
INTENTIONAL = re.compile(r"/api/(chat|login|documents)")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


class Tab:
    """A page plus everything it logged: console messages, page errors, failed requests, /api/chat calls."""

    def __init__(self, ctx, base):
        self.ctx, self.base = ctx, base
        self.page = ctx.new_page()
        self.console, self.errors, self.bad_responses, self.failed, self.chat_requests, self.dialogs = [], [], [], [], [], []
        self.page.on("console", lambda m: self.console.append((m.type, m.text)) if m.type in ("error", "warning") else None)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("response", lambda r: self.bad_responses.append((r.status, r.url)) if r.status >= 400 else None)
        self.page.on("requestfailed", lambda r: self.failed.append((r.url, r.failure)))
        self.page.on("request", lambda r: self.chat_requests.append(json.loads(r.post_data or "{}")) if r.url.endswith("/api/chat") and r.method == "POST" else None)
        self.page.on("dialog", lambda d: (self.dialogs.append(d.message), d.dismiss()))

    def open(self, path="/"):
        self.page.goto(self.base + path)
        self.page.wait_for_load_state("networkidle")
        return self.page

    def ask(self, text, via="click"):
        p = self.page
        before = p.locator("li.message.assistant").count()
        p.fill("#input", text)
        if via == "enter":
            p.press("#input", "Enter")
        else:
            p.click("#send")
        expect(p.locator("li.message.assistant")).to_have_count(before + 1, timeout=10_000)
        return p.locator("li.message.assistant").last

    def unexpected_network(self):
        return [r for r in self.bad_responses if not INTENTIONAL.search(r[1])]


@pytest.fixture
def tab(browser, local_server):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    t = Tab(ctx, local_server.url)
    yield t
    ctx.close()


def fulfill_once(page, answer=None, citations=None, status=200, body=None, content_type="application/json"):
    payload = body if body is not None else json.dumps({"answer": answer, "citations": citations or []})
    page.route("**/api/chat", lambda route: route.fulfill(status=status, body=payload, content_type=content_type), times=1)


# ================================================================== UI
def test_UI_001_page_loads(tab, record):
    p = tab.open()
    title = p.title()
    inp, send = p.locator("#input"), p.locator("#send")
    record(actual=f"title={title!r}; input visible={inp.is_visible()} enabled={inp.is_enabled()}; send visible={send.is_visible()} "
                  f"enabled={send.is_enabled()}; console={tab.console}; page errors={tab.errors}; 4xx/5xx={tab.bad_responses}")
    assert title == "AI Based Chatbot" and inp.is_visible() and inp.is_enabled() and send.is_visible() and send.is_enabled()
    assert not tab.errors and not [c for c in tab.console if c[0] == "error"] and not tab.bad_responses


def test_UI_002_send_by_click(tab, record):
    tab.open()
    reply = tab.ask("What does the warranty cover?")
    user = tab.page.locator("li.message.user").last.inner_text()
    record(actual=f"user bubble={user!r}; bot reply={reply.inner_text()[:120]!r}; input after send={tab.page.input_value('#input')!r}")
    assert user == "What does the warranty cover?" and "warranty" in reply.inner_text().lower() and tab.page.input_value("#input") == ""


def test_UI_003_send_by_enter(tab, record):
    tab.open()
    reply = tab.ask("How do I return a product?", via="enter")
    record(actual=f"requests={len(tab.chat_requests)}; reply={reply.inner_text()[:100]!r}")
    assert len(tab.chat_requests) == 1 and reply.inner_text().strip()


def test_UI_004_shift_enter_newline(tab, record):
    p = tab.open()
    p.click("#input")
    p.keyboard.type("first line")
    p.keyboard.press("Shift+Enter")
    p.keyboard.type("second line about warranty")
    value = p.input_value("#input")
    sent_before = len(tab.chat_requests)
    p.keyboard.press("Enter")
    expect(p.locator("li.message.assistant")).to_have_count(1)
    bubble = p.locator("li.message.user").last
    lines = bubble.evaluate("e => e.getBoundingClientRect().height / parseFloat(getComputedStyle(e).lineHeight)")
    record(actual=f"textarea value after Shift+Enter={value!r}; requests before Enter={sent_before}; sent message={tab.chat_requests[-1]['message']!r}; bubble ≈{lines:.1f} line-heights tall")
    assert value == "first line\nsecond line about warranty" and sent_before == 0 and "\n" in tab.chat_requests[-1]["message"]


def test_UI_005_order_and_styling(tab, record):
    p = tab.open()
    tab.ask("What does the warranty cover?")
    tab.ask("How do I return a product?")
    classes = p.locator("#messages > li").evaluate_all("els => els.map(e => e.className)")
    styles = p.locator("#messages > li").evaluate_all(
        "els => els.map(e => { const s = getComputedStyle(e), r = e.getBoundingClientRect(); return [s.alignSelf, s.backgroundColor, Math.round(r.left), Math.round(r.right)]; })")
    record(actual=f"order={classes}; styles(alignSelf,bg,left,right)={styles}")
    assert classes == ["message user", "message assistant"] * 2
    assert styles[0][0] == "flex-end" and styles[1][0] == "flex-start" and styles[0][1] != styles[1][1]
    assert styles[0][3] > styles[1][3] - 5 and styles[1][2] < styles[0][2]  # user right, bot left


def test_UI_006_thinking_indicator(tab, record):
    p = tab.open()
    pending = []
    p.route("**/api/chat", lambda route: pending.append(route))
    p.fill("#input", "What does the warranty cover?")
    p.click("#send")
    expect(p.locator("#thinking")).to_be_visible()
    during = (p.locator("#thinking").inner_text(), p.locator("#send").is_disabled())
    p.wait_for_timeout(300)
    pending[0].continue_()
    expect(p.locator("li.message.assistant")).to_have_count(1)
    after = (p.locator("#thinking").is_visible(), p.locator("#send").is_disabled())
    record(actual=f"while waiting: text={during[0]!r}, send disabled={during[1]}; after reply: thinking visible={after[0]}, send disabled={after[1]}")
    assert during[1] and "Searching" in during[0] and after == (False, False)


def test_UI_008_empty_message_not_sent(tab, record):
    p = tab.open()
    for value in ("", "   ", "\n\n"):
        p.fill("#input", value)
        p.click("#send")
        p.press("#input", "Enter")
    p.wait_for_timeout(500)
    record(actual=f"requests sent={len(tab.chat_requests)}; messages shown={p.locator('#messages > li').count()}")
    assert not tab.chat_requests and p.locator("#messages > li").count() == 0


def test_UI_009_long_message_wraps(tab, record):
    p = tab.open()
    long_word = "x" * 3000
    tab.ask(long_word)
    tab.ask(("warranty " * 300).strip())
    widths = p.evaluate("""() => ({doc: document.documentElement.scrollWidth, win: innerWidth,
        bubbles: [...document.querySelectorAll('li.message')].map(e => Math.round(e.getBoundingClientRect().right)),
        chatOverflowX: document.getElementById('chat').scrollWidth > document.getElementById('chat').clientWidth})""")
    record(actual=json.dumps(widths))
    assert widths["doc"] <= widths["win"] and max(widths["bubbles"]) <= widths["win"] and not widths["chatOverflowX"]


def test_UI_010_long_reply_scrolls_and_autoscrolls(tab, record):
    p = tab.open()
    long_answer = "\n\n".join(f"Paragraph {i}: the warranty covers many things [1]." for i in range(80))
    fulfill_once(p, long_answer, [{"id": 1, "title": "warranty-policy.md", "snippet": "…"}])
    tab.ask("Tell me everything about the warranty")
    p.wait_for_timeout(300)
    m = p.evaluate("() => { const c = document.getElementById('chat'); return {scrollHeight: c.scrollHeight, clientHeight: c.clientHeight, scrollTop: Math.round(c.scrollTop), bodyScroll: document.documentElement.scrollHeight > innerHeight}; }")
    composer_visible = p.locator("#composer").is_visible() and p.locator("#composer").bounding_box()["y"] < 800
    at_bottom = m["scrollTop"] + m["clientHeight"] >= m["scrollHeight"] - 5
    record(actual=f"{m}; scrolled to latest={at_bottom}; composer still on screen={composer_visible}")
    assert m["scrollHeight"] > m["clientHeight"] and at_bottom and composer_visible and not m["bodyScroll"]


def test_UI_011_multi_turn_context(tab, record):
    p = tab.open()
    tab.ask("How long does the warranty last?")
    tab.ask("and for refunds?")
    hist = tab.chat_requests[-1]["history"]
    record(actual=f"2nd request history={[(h['role'], h['content'][:40]) for h in hist]}; messages on screen={p.locator('#messages > li').count()}")
    assert [h["role"] for h in hist] == ["user", "assistant"] and hist[0]["content"] == "How long does the warranty last?"
    assert p.locator("#messages > li").count() == 4


def test_UI_012_new_conversation(tab, record):
    p = tab.open()
    tab.ask("What does the warranty cover?")
    p.click("#new-chat")
    state = (p.locator("#messages > li").count(), p.locator("#welcome").is_visible(), p.evaluate("sessionStorage.getItem('chatbot.conversation')"))
    tab.ask("How do I return a product?")
    record(actual=f"after New conversation: messages={state[0]}, welcome visible={state[1]}, stored conversation={state[2]}; next request history={tab.chat_requests[-1]['history']}")
    assert state == (0, True, None) and tab.chat_requests[-1]["history"] == []


def test_UI_013_suggestion_chip(tab, record):
    p = tab.open()
    chip = p.locator(".chip").first
    text = chip.inner_text()
    chip.click()
    expect(p.locator("li.message.assistant")).to_have_count(1)
    record(actual=f"chip {text!r} sent={tab.chat_requests[-1]['message']!r}; welcome hidden={p.locator('#welcome').is_hidden()}")
    assert tab.chat_requests[-1]["message"] == text and p.locator("#welcome").is_hidden()


def test_UI_014_citation_opens_source(tab, record):
    p = tab.open()
    reply = tab.ask("What does the warranty cover?")
    btn = reply.locator("button.cite-ref").first
    btn.click()
    details = reply.locator(".sources details").first
    record(actual=f"citation buttons={reply.locator('button.cite-ref').count()}; sources listed={reply.locator('.sources li').all_inner_texts()[:2]}; "
                  f"first source open after click={details.get_attribute('open') is not None}")
    assert details.get_attribute("open") is not None


def test_UI_015_backend_error_and_retry(tab, record):
    p = tab.open()
    fulfill_once(p, status=502, body=json.dumps({"error": {"code": "upstream_error", "message": "The service could not answer right now. Please try again shortly."}}))
    p.fill("#input", "What does the warranty cover?")
    p.click("#send")
    expect(p.locator("#error")).to_be_visible()
    state = dict(error=p.locator("#error-text").inner_text(), retry=p.locator("#retry").is_visible(), input=p.input_value("#input"),
                 input_enabled=p.locator("#input").is_enabled(), send_enabled=p.locator("#send").is_enabled(), msgs=p.locator("#messages > li").count())
    p.click("#retry")
    expect(p.locator("li.message.assistant")).to_have_count(1)
    after = dict(error_hidden=p.locator("#error").is_hidden(), msgs=p.locator("#messages > li").count())
    record(actual=f"on 502: {state}; after Try again: {after}")
    assert state["error"].startswith("The service could not answer") and state["retry"] and state["input_enabled"] and state["send_enabled"]
    assert after == {"error_hidden": True, "msgs": 2}


def test_UI_016_backend_down(tab, record):
    p = tab.open()
    p.route("**/api/chat", lambda route: route.abort("connectionrefused"), times=1)
    p.fill("#input", "How do I return a product?")
    p.click("#send")
    expect(p.locator("#error")).to_be_visible()
    msg, kept = p.locator("#error-text").inner_text(), p.input_value("#input")
    p.click("#retry")
    expect(p.locator("li.message.assistant")).to_have_count(1)
    record(actual=f"error={msg!r}; question kept in box={kept!r}; send enabled={p.locator('#send').is_enabled()}; retry after server back -> answered")
    assert "Could not reach the server" in msg and kept == "How do I return a product?"


def test_UI_017_non_json_error_page(tab, record):
    p = tab.open()
    fulfill_once(p, status=504, body="<html><body>504 Gateway Time-out</body></html>", content_type="text/html")
    p.fill("#input", "What does the warranty cover?")
    p.click("#send")
    expect(p.locator("#error")).to_be_visible()
    msg = p.locator("#error-text").inner_text()
    record(actual=f"error shown={msg!r}; send enabled={p.locator('#send').is_enabled()}")
    assert "(504)" in msg and "<html" not in msg and p.locator("#send").is_enabled()


def test_UI_018_refresh_keeps_conversation(tab, record):
    p = tab.open()
    tab.ask("What does the warranty cover?")
    p.reload()
    p.wait_for_load_state("networkidle")
    after_reload = p.locator("#messages > li").count()
    other = tab.ctx.new_page()
    other.goto(tab.base + "/")
    other.wait_for_load_state("networkidle")
    new_tab = other.locator("#messages > li").count()
    record(actual=f"messages after reload={after_reload}; in a new tab={new_tab} (documented: conversation lives in the browser tab)")
    assert after_reload == 2 and new_tab == 0


def test_UI_019_login_logout(browser, password_server, demo_password, record):
    ctx = browser.new_context()
    t = Tab(ctx, password_server.url)
    p = t.page
    try:
        p.goto(password_server.url + "/")
        landed = p.url.replace(password_server.url, "")
        p.fill("#password", "definitely-wrong")
        p.click("#sign-in")
        expect(p.locator("#login-error")).not_to_be_empty()
        wrong = p.locator("#login-error").inner_text()
        p.fill("#password", demo_password)
        p.click("#sign-in")
        p.wait_for_url(password_server.url + "/")
        p.wait_for_load_state("networkidle")
        signout_visible = p.locator("#sign-out").is_visible()
        t.ask("What does the warranty cover?")
        p.reload()
        p.wait_for_load_state("networkidle")
        still_in = p.url.endswith("/") and p.locator("#messages > li").count() == 2
        p.click("#sign-out")
        p.wait_for_url("**/login.html")
        p.goto(password_server.url + "/")
        back = p.url.replace(password_server.url, "")
        stored = p.evaluate("sessionStorage.getItem('chatbot.conversation')")
        record(actual=f"unauthenticated / -> {landed}; wrong password msg={wrong!r}; after sign-in Sign out visible={signout_visible}; "
                      f"refresh keeps session+chat={still_in}; after sign-out / -> {back}; conversation cleared={stored is None}")
        assert landed == "/login.html" and "not correct" in wrong and signout_visible and still_in and back == "/login.html" and stored is None
    finally:
        ctx.close()


def test_UI_020_markdown_rendering(tab, record):
    p = tab.open()
    md = ("**Bold claim** about refunds [1].\n\n- first item [1]\n- second item\n\n1. step one\n2. step two\n\n"
          "```python\nprint('hi')\n```\n\nSee [the policy](https://example.com/policy) or https://example.com.")
    fulfill_once(p, md, [{"id": 1, "title": "returns-and-refunds.md", "snippet": "Refunds…"}])
    reply = tab.ask("formatting test")
    info = reply.evaluate("""e => ({strong: e.querySelectorAll('strong').length, ul: e.querySelectorAll('ul').length,
        li: e.querySelectorAll(':scope > ul > li').length, cite: e.querySelectorAll('button.cite-ref').length,
        pre: e.querySelectorAll('pre, code').length, links: e.querySelectorAll('a').length,
        rawFence: e.innerText.includes('```'), rawLink: e.innerText.includes('[the policy](')})""")
    record(actual=f"{info} – bold, bullet/numbered lists and [n] citations render; code fences and links are NOT rendered (shown as plain text; not a designed feature)")
    assert info["strong"] == 1 and info["ul"] == 2 and info["li"] == 4 and info["cite"] == 2


def test_UI_021_question_typed_while_waiting_is_not_lost(tab, record):
    p = tab.open()
    pending = []
    p.route("**/api/chat", lambda route: pending.append(route))
    p.fill("#input", "What does the warranty cover?")
    p.click("#send")
    expect(p.locator("#thinking")).to_be_visible()
    p.fill("#input", "And how do I return a product?")
    p.press("#input", "Enter")
    box_after_enter = p.input_value("#input")
    p.wait_for_timeout(200)
    pending[0].continue_()
    p.unroute("**/api/chat")
    expect(p.locator("li.message.assistant")).to_have_count(1)
    p.wait_for_timeout(800)
    for r in pending[1:]:
        r.continue_()
    shown = p.locator("li.message.user").all_inner_texts()
    sent = [r["message"] for r in tab.chat_requests]
    survived = "And how do I return a product?" in shown + sent + [p.input_value("#input")]
    record(actual=f"input box right after Enter={box_after_enter!r}; requests sent={sent}; user messages shown={shown}; "
                  f"second question anywhere (screen/request/input)={survived}")
    assert survived, "second question silently discarded: input cleared but ask() returned early because busy"


def test_UI_022_documents_dialog(tab, record, tmp_path):
    p = tab.open()
    p.click("#docs-button")
    expect(p.locator("#docs-dialog")).to_be_visible()
    good = tmp_path / "qa-ui-upload.md"
    good.write_text("# QA UI doc\nThe flamingo discount is 12 percent.")
    bad = tmp_path / "evil.exe"
    bad.write_bytes(b"MZ")
    p.set_input_files("#file", str(good))
    p.click("#upload-button")
    expect(p.locator("#upload-status")).to_contain_text("added")
    expect(p.locator("#doc-list")).to_contain_text("qa-ui-upload.md")
    ok_status = p.locator("#upload-status").inner_text()
    p.set_input_files("#file", str(bad))
    p.click("#upload-button")
    expect(p.locator("#upload-status")).to_contain_text("Unsupported")
    bad_status = p.locator("#upload-status").inner_text()
    p.keyboard.press("Escape")
    reply = tab.ask("What is the flamingo discount?")
    record(actual=f"upload ok status={ok_status!r}; disallowed status={bad_status!r}; indexer note={p.locator('#indexer-status').inner_text()!r}; "
                  f"chat cites uploaded doc={'qa-ui-upload.md' in reply.inner_text()}")
    assert "qa-ui-upload.md" in reply.inner_text()
    httpx.delete(f"{tab.base}/api/documents/qa-ui-upload.md")


def test_UI_023_follow_up_after_very_long_answer(tab, record):
    p = tab.open()
    long_answer = " ".join(["The warranty covers defects in materials and workmanship [1]."] * 150)  # ~9,300 chars
    fulfill_once(p, long_answer, [{"id": 1, "title": "warranty-policy.md", "snippet": "…"}])
    tab.ask("Explain the warranty in full detail")
    p.fill("#input", "and how long does it last?")
    p.click("#send")
    p.wait_for_timeout(1500)
    err = p.locator("#error-text").inner_text() if p.locator("#error").is_visible() else ""
    record(actual=f"previous answer length={len(long_answer)} chars; follow-up result: error={err!r}; bot replies={p.locator('li.message.assistant').count()}")
    assert not err and p.locator("li.message.assistant").count() == 2, "a long previous answer makes every follow-up fail (history content > 8000 chars -> 422)"


# ================================================================== FRONT END
def test_FE_001_console_and_network_clean(tab, record):
    p = tab.open()
    tab.ask("What does the warranty cover?")
    p.locator(".cite-ref").first.click()
    tab.ask("and for refunds?", via="enter")
    p.click("#new-chat")
    p.locator(".chip").nth(1).click()
    expect(p.locator("li.message.assistant")).to_have_count(1)
    p.click("#docs-button")
    expect(p.locator("#indexer-status")).to_contain_text("Local mode")
    p.keyboard.press("Escape")
    p.reload()
    p.wait_for_load_state("networkidle")
    record(actual=f"console errors/warnings={tab.console}; page errors={tab.errors}; failed requests={tab.failed}; 4xx/5xx={tab.bad_responses}")
    assert not tab.console and not tab.errors and not tab.failed and not tab.bad_responses


def test_FE_002_build_and_syntax(record):
    js = [str(f) for f in (REPO / "Code" / "frontend").glob("*.js")]
    node = [subprocess.run(["node", "--check", f], capture_output=True, text=True) for f in js]
    comp = subprocess.run([str(PY), "-m", "compileall", "-q", "-x", "tests", "Code/backend/chatbot"], cwd=REPO, capture_output=True, text=True,
                          env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(REPO / "qa" / ".pycache")})
    configs = [f for f in ("package.json", ".eslintrc", ".eslintrc.json", "eslint.config.js", "tsconfig.json", "ruff.toml", "pyproject.toml", "setup.cfg", "mypy.ini")
               if (REPO / f).exists() or (REPO / "Code" / "frontend" / f).exists()]
    record(actual=f"no build step (plain JS); node --check {len(js)} files exit codes={[n.returncode for n in node]}; python compileall exit={comp.returncode}; "
                  f"lint/type-check configs found={configs or 'none (not configured)'}")
    assert all(n.returncode == 0 for n in node) and comp.returncode == 0


def test_FE_003_static_assets(local_server, record):
    out = {}
    for path, ctype in [("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css"), ("/favicon.svg", "image/svg+xml"),
                        ("/login.html", "text/html"), ("/login.js", "javascript")]:
        r = httpx.get(local_server.url + path)
        out[path] = (r.status_code, r.headers.get("content-type"))
        assert r.status_code == 200 and ctype in r.headers["content-type"], (path, out[path])
    record(actual=json.dumps(out) + "; no external fonts/images/CDN referenced")


XSS = ['<script>alert("xss-script")</script>', '<img src=x onerror=alert("xss-img")>', '<svg onload=alert("xss-svg")>',
       '"><iframe src="javascript:alert(\'xss-iframe\')"></iframe>', '**<b onmouseover=alert(1)>bold</b>** [1]']


def _dom_injection(p):
    return p.evaluate("""() => ({imgs: document.querySelectorAll('#messages img, #messages svg, #messages iframe, #messages script, #messages b[onmouseover]').length,
                                 scripts: document.scripts.length})""")


def test_FE_004_xss_in_bot_reply(tab, record):
    p = tab.open()
    scripts_before = p.evaluate("document.scripts.length")
    fulfill_once(p, "\n\n".join(XSS), [{"id": 1, "title": "x", "snippet": "y"}])
    reply = tab.ask("xss test")
    p.mouse.move(400, 300)
    p.wait_for_timeout(500)
    inj = _dom_injection(p)
    record(actual=f"dialogs fired={tab.dialogs}; injected elements={inj['imgs']}; scripts {scripts_before}->{inj['scripts']}; payload shown as text={'<script>' in reply.inner_text()}")
    assert not tab.dialogs and inj["imgs"] == 0 and inj["scripts"] == scripts_before and "<script>" in reply.inner_text()


def test_FE_005_xss_in_user_message_and_sources(tab, record):
    p = tab.open()
    tab.ask(XSS[0] + " " + XSS[1])
    fulfill_once(p, "See [1].", [{"id": 1, "title": XSS[1], "snippet": XSS[0]}])
    reply = tab.ask("source xss")
    reply.locator("summary").first.click()
    p.wait_for_timeout(300)
    p.reload()  # stored conversation re-rendered from sessionStorage
    p.wait_for_load_state("networkidle")
    p.locator("summary").first.click()
    p.wait_for_timeout(300)
    inj = _dom_injection(p)
    record(actual=f"dialogs fired={tab.dialogs}; injected elements after send+reload={inj['imgs']}; user bubble text={p.locator('li.message.user').first.inner_text()[:60]!r}")
    assert not tab.dialogs and inj["imgs"] == 0


def test_FE_006_no_secrets_in_bundle_or_requests(tab, local_server, record):
    from test_backend_live import SECRET_PATTERNS

    hits = []
    for path in ("/", "/app.js", "/login.html", "/login.js", "/app.css"):
        text = httpx.get(local_server.url + path).text
        hits += [(path, name) for name, pat in SECRET_PATTERNS.items() if re.search(pat, text)]
    headers = []
    tab.page.on("request", lambda r: headers.append({k: v for k, v in r.headers.items() if k.lower() in ("authorization", "api-key", "x-upload-key", "cookie")}))
    p = tab.open()
    tab.ask("What does the warranty cover?")
    p.click("#docs-button")
    p.wait_for_timeout(500)
    sent = [h for h in headers if h]
    record(actual=f"secret patterns in served HTML/JS/CSS={hits}; auth/key headers in browser requests={sent}")
    assert not hits and not sent


def test_FE_007_session_cookie_flags(browser, password_server, demo_password, record):
    ctx = browser.new_context()
    p = ctx.new_page()
    try:
        p.goto(password_server.url + "/login.html")
        p.fill("#password", demo_password)
        p.click("#sign-in")
        p.wait_for_url(password_server.url + "/")
        cookie = next(c for c in ctx.cookies() if c["name"] == "chatbot_session")
        js_visible = "chatbot_session" in p.evaluate("document.cookie")
        record(actual=f"httpOnly={cookie['httpOnly']}, sameSite={cookie['sameSite']}, secure={cookie['secure']} (served over plain http locally; "
                      f"Secure is set when X-Forwarded-Proto=https, see BE-028), path={cookie['path']}, readable by JS={js_visible}")
        assert cookie["httpOnly"] and cookie["sameSite"] == "Lax" and not js_visible
    finally:
        ctx.close()


def test_FE_008_csrf_cross_site_post(browser, password_server, demo_password, record):
    ctx = browser.new_context()
    p = ctx.new_page()
    try:
        p.goto(password_server.url + "/login.html")
        p.fill("#password", demo_password)
        p.click("#sign-in")
        p.wait_for_url(password_server.url + "/")
        evil = ("<html><body><form id=f method=POST enctype=text/plain action='%s/api/chat'>"
                "<input name='{\"message\":\"csrf\",\"x\":\"' value='\"}'></form><script>f.submit()</script></body></html>") % password_server.url
        p.route("http://evil.test/**", lambda r: r.fulfill(body=evil, content_type="text/html"))
        seen = {}
        p.on("request", lambda r: seen.update(cookie="cookie" in r.headers) if r.url.endswith("/api/chat") else None)
        p.on("response", lambda r: seen.update(status=r.status) if r.url.endswith("/api/chat") else None)
        p.goto("http://evil.test/attack")
        p.wait_for_timeout(1500)
        record(actual=f"cross-site form POST to /api/chat: session cookie sent={seen.get('cookie')}, response status={seen.get('status')}")
        assert seen.get("status") != 200 and not seen.get("cookie")
    finally:
        ctx.close()


@pytest.mark.parametrize("w,h", [(375, 812), (768, 1024), (1280, 800)])
def test_FE_009_responsive(browser, local_server, record, w, h):
    ctx = browser.new_context(viewport={"width": w, "height": h})
    t = Tab(ctx, local_server.url)
    try:
        p = t.open()
        t.ask("What does the warranty cover? " + "y" * 400)
        m = p.evaluate("() => ({scrollW: document.documentElement.scrollWidth, innerW: innerWidth})")
        vis = {s: p.locator(s).is_visible() and p.locator(s).bounding_box()["x"] + p.locator(s).bounding_box()["width"] <= w
               for s in ("#input", "#send", "#new-chat", "#docs-button", "#title")}
        p.click("#docs-button")
        dlg = p.locator("#docs-dialog").bounding_box()
        record(actual=f"{w}x{h}: scrollWidth={m['scrollW']} innerWidth={m['innerW']}; on-screen={vis}; dialog x={dlg['x']:.0f} w={dlg['width']:.0f}")
        assert m["scrollW"] <= m["innerW"] and all(vis.values()) and dlg["x"] >= 0 and dlg["x"] + dlg["width"] <= w
    finally:
        ctx.close()


def _axe(page):
    page.add_script_tag(path=str(AXE))
    return page.evaluate("async () => (await axe.run(document, {resultTypes: ['violations']})).violations.map(v => ({id: v.id, impact: v.impact, n: v.nodes.length, target: v.nodes[0].target.join(' ')}))")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_FE_010_accessibility_axe(browser, local_server, password_server, record, scheme):
    if not AXE.exists():
        pytest.skip("axe-core not installed in qa/node_modules")
    ctx = browser.new_context(bypass_csp=True, color_scheme=scheme)
    t = Tab(ctx, local_server.url)
    try:
        p = t.open()
        basics = p.evaluate("""() => ({lang: document.documentElement.lang, title: document.title,
            inputLabel: !!document.querySelector('label[for=input]'), sendName: document.getElementById('send').getAttribute('aria-label')})""")
        welcome = _axe(p)
        t.ask("What does the warranty cover?")
        chat = _axe(p)
        p.click("#docs-button")
        dialog = _axe(p)
        lp = ctx.new_page()
        lp.goto(password_server.url + "/login.html")
        login = _axe(lp)
        allv = {"welcome": welcome, "conversation": chat, "documents dialog": dialog, "login": login}
        serious = {k: [v for v in vs if v["impact"] in ("serious", "critical")] for k, vs in allv.items()}
        record(actual=f"[{scheme}] basics={basics}; axe violations={json.dumps(allv)}")
        assert basics["lang"] == "en" and basics["title"] and basics["inputLabel"] and basics["sendName"]
        assert not any(serious.values()), f"serious/critical axe violations: {serious}"
    finally:
        ctx.close()


def test_FE_011_keyboard_only(tab, record):
    p = tab.open()
    first_focus = p.evaluate("document.activeElement.id")
    p.evaluate("document.activeElement.blur()")
    order = []
    for _ in range(8):
        p.keyboard.press("Tab")
        order.append(p.evaluate("""() => { const e = document.activeElement; const s = getComputedStyle(e);
            return [e.id || e.className || e.tagName, s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0]; }"""))
    while p.evaluate("document.activeElement.id") != "input":
        p.keyboard.press("Tab")
    composer_border = p.evaluate("getComputedStyle(document.getElementById('composer')).borderColor")
    p.keyboard.type("What does the warranty cover?")
    p.keyboard.press("Enter")
    expect(p.locator("li.message.assistant")).to_have_count(1)
    focus_back = p.evaluate("document.activeElement.id")
    record(actual=f"autofocus={first_focus}; tab order (element, visible outline)={order}; composer border when textarea focused={composer_border}; focus after reply={focus_back}")
    reached = [o[0] for o in order]
    assert "input" in reached and "send" in reached and "new-chat" in reached
    # BODY = Tab wrapped past the last element to the browser chrome; the textarea uses the composer border as its focus indicator
    assert all(o[1] for o in order if o[0] not in ("input", "BODY"))
    assert focus_back == "input"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_FE_012_color_contrast(browser, local_server, record, scheme):
    ctx = browser.new_context(bypass_csp=True, color_scheme=scheme)
    t = Tab(ctx, local_server.url)
    try:
        p = t.open()
        t.ask("What does the warranty cover?")
        fulfill_once(p, status=502, body=json.dumps({"error": {"code": "upstream_error", "message": "The service could not answer right now."}}))
        p.fill("#input", "error state for contrast")
        p.click("#send")
        expect(p.locator("#error")).to_be_visible()
        p.add_script_tag(path=str(AXE))
        res = p.evaluate("""async () => { const r = await axe.run(document, {runOnly: ['color-contrast']});
            return {passes: r.passes.reduce((n, v) => n + v.nodes.length, 0), violations: r.violations.map(v => v.nodes.map(n => n.target.join(' ') + ' ' + (n.any[0] || {}).message))}; }""")
        record(actual=f"[{scheme}] WCAG AA color-contrast: {res['passes']} text nodes pass, violations={res['violations']}")
        assert not res["violations"]
    finally:
        ctx.close()


def test_FE_013_local_storage_empty(tab, record):
    p = tab.open()
    tab.ask("What does the warranty cover?")
    p.click("#docs-button")
    p.fill("#upload-key", "qa-fake-upload-key")
    p.dispatch_event("#upload-key", "change")
    ls = p.evaluate("Object.keys(localStorage)")
    record(actual=f"localStorage keys={ls}")
    assert ls == []


def test_FE_014_no_secret_in_session_storage(tab, record):
    p = tab.open()
    p.click("#docs-button")
    p.fill("#upload-key", "qa-fake-upload-key")
    p.dispatch_event("#upload-key", "change")
    ss = p.evaluate("Object.fromEntries(Object.entries(sessionStorage))")
    record(actual=f"sessionStorage keys={list(ss)}; upload key stored in clear text={'qa-fake-upload-key' in json.dumps(ss)}")
    assert "qa-fake-upload-key" not in json.dumps(ss), "upload key persisted in sessionStorage"
