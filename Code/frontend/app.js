// AI Based Chatbot – chat page. Plain JavaScript, no build step.
// The conversation lives in this tab (sessionStorage) and is sent with each question so the
// backend can resolve follow-ups. Model output is rendered with DOM text nodes, never innerHTML.
(() => {
  "use strict";

  const MAX_HISTORY = 12; // messages sent with each question (the backend trims further)
  const STORE_KEY = "chatbot.conversation";
  const KEY_KEY = "chatbot.uploadKey";

  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $("messages"), welcome: $("welcome"), thinking: $("thinking"),
    error: $("error"), errorText: $("error-text"), retry: $("retry"),
    form: $("composer"), input: $("input"), send: $("send"), newChat: $("new-chat"),
    chat: $("chat"), suggestions: $("suggestions"), title: $("title"), subtitle: $("subtitle"),
    docsButton: $("docs-button"), dialog: $("docs-dialog"), uploadForm: $("upload-form"),
    file: $("file"), uploadKey: $("upload-key"), uploadButton: $("upload-button"),
    uploadStatus: $("upload-status"), docList: $("doc-list"), indexerStatus: $("indexer-status"),
    refreshDocs: $("refresh-docs"),
  };

  const storage = {
    get(key, fallback) { try { const v = sessionStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
    set(key, value) { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
    remove(key) { try { sessionStorage.removeItem(key); } catch { /* storage unavailable */ } },
  };

  /** @type {{role: "user"|"assistant", content: string, citations?: {id:number,title:string,snippet:string}[]}[]} */
  let conversation = storage.get(STORE_KEY, []);
  let busy = false;
  let lastQuestion = null;

  // ------------------------------------------------------------------ rendering
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v; else if (k === "text") node.textContent = v; else node.setAttribute(k, v);
    }
    for (const child of children) if (child != null) node.append(child);
    return node;
  }

  // Inline formatting: **bold** and [n] citation markers.
  function inline(text, messageId, citationIds) {
    const frag = document.createDocumentFragment();
    const pattern = /\*\*(.+?)\*\*|\[(\d{1,2})\]/g;
    let last = 0;
    for (const m of text.matchAll(pattern)) {
      frag.append(text.slice(last, m.index));
      if (m[1] !== undefined) {
        frag.append(el("strong", { text: m[1] }));
      } else if (citationIds.has(Number(m[2]))) {
        const btn = el("button", { type: "button", class: "cite-ref", "aria-label": `Source ${m[2]}`, text: m[2] });
        btn.addEventListener("click", () => showSource(messageId, Number(m[2])));
        frag.append(btn);
      } else {
        frag.append(m[0]);
      }
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    return frag;
  }

  // Block formatting: paragraphs and "- " / "* " / "1. " lists.
  function formatAnswer(text, messageId, citations) {
    const ids = new Set((citations || []).map((c) => c.id));
    const frag = document.createDocumentFragment();
    for (const block of text.split(/\n{2,}/)) {
      const lines = block.split("\n").filter((l) => l.trim());
      if (!lines.length) continue;
      if (lines.every((l) => /^\s*([-*•]|\d+[.)])\s+/.test(l))) {
        const ul = el("ul");
        for (const l of lines) ul.append(el("li", {}, inline(l.replace(/^\s*([-*•]|\d+[.)])\s+/, ""), messageId, ids)));
        frag.append(ul);
      } else {
        const p = el("p");
        lines.forEach((l, i) => { if (i) p.append(el("br")); p.append(inline(l, messageId, ids)); });
        frag.append(p);
      }
    }
    return frag;
  }

  function renderMessage(msg, index) {
    const id = `m${index}`;
    if (msg.role === "user") return el("li", { class: "message user", id }, msg.content);
    const li = el("li", { class: "message assistant", id }, formatAnswer(msg.content, id, msg.citations));
    if (msg.citations && msg.citations.length) {
      const list = el("ol");
      for (const c of msg.citations) {
        list.append(el("li", {},
          el("details", { id: `${id}-s${c.id}` },
            el("summary", {}, el("b", { text: `[${c.id}]` }), c.title),
            el("p", { class: "snippet", text: c.snippet }))));
      }
      li.append(el("section", { class: "sources", "aria-label": "Sources" }, el("h3", { text: "Sources" }), list));
    }
    return li;
  }

  function render() {
    els.messages.replaceChildren(...conversation.map(renderMessage));
    els.welcome.hidden = conversation.length > 0;
    els.chat.scrollTop = els.chat.scrollHeight;
  }

  function showSource(messageId, n) {
    const details = document.getElementById(`${messageId}-s${n}`);
    if (!details) return;
    details.open = true;
    details.scrollIntoView({ block: "nearest", behavior: "smooth" });
    details.classList.add("flash");
    setTimeout(() => details.classList.remove("flash"), 1200);
  }

  function setBusy(value) {
    busy = value;
    els.thinking.hidden = !value;
    els.send.disabled = value;
    els.input.setAttribute("aria-busy", String(value));
    if (value) els.chat.scrollTop = els.chat.scrollHeight;
  }

  function showError(message) {
    els.errorText.textContent = message;
    els.error.hidden = !message;
    els.retry.hidden = !lastQuestion;
  }

  // ------------------------------------------------------------------ API
  async function api(path, options = {}) {
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new Error("Could not reach the server. Check your connection and try again.");
    }
    let body = null;
    try { body = await response.json(); } catch { /* not JSON */ }
    if (!response.ok) {
      const message = body && body.error && body.error.message;
      throw new Error(message || `The server returned an error (${response.status}).`);
    }
    return body;
  }

  async function ask(question) {
    if (busy || !question.trim()) return;
    lastQuestion = question;
    showError("");
    const history = conversation.slice(-MAX_HISTORY).map(({ role, content }) => ({ role, content }));
    conversation.push({ role: "user", content: question });
    render();
    setBusy(true);
    try {
      const result = await api("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: question, history }),
      });
      conversation.push({ role: "assistant", content: result.answer, citations: result.citations });
      lastQuestion = null;
      storage.set(STORE_KEY, conversation);
    } catch (err) {
      conversation.pop(); // keep the history consistent; the question goes back into the box
      els.input.value = question;
      autosize();
      showError(err.message);
    } finally {
      setBusy(false);
      render();
      els.input.focus();
    }
  }

  // ------------------------------------------------------------------ composer
  function autosize() {
    els.input.style.height = "auto";
    els.input.style.height = `${Math.min(els.input.scrollHeight, 200)}px`;
  }

  els.form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = els.input.value.trim();
    if (!q) return;
    els.input.value = "";
    autosize();
    ask(q);
  });
  els.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); els.form.requestSubmit(); }
  });
  els.input.addEventListener("input", autosize);
  els.retry.addEventListener("click", () => {
    if (!lastQuestion) return;
    els.input.value = "";
    ask(lastQuestion);
  });
  els.newChat.addEventListener("click", () => {
    conversation = [];
    lastQuestion = null;
    storage.remove(STORE_KEY);
    showError("");
    render();
    els.input.focus();
  });
  els.suggestions.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) ask(chip.textContent);
  });

  // ------------------------------------------------------------------ documents
  const keyHeaders = () => (els.uploadKey.value ? { "X-Upload-Key": els.uploadKey.value } : {});
  const sizeText = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

  function uploadStatus(text, bad = false) {
    els.uploadStatus.textContent = text;
    els.uploadStatus.classList.toggle("bad", bad);
  }

  async function loadDocuments() {
    els.indexerStatus.textContent = "Loading…";
    try {
      const data = await api("/api/documents", { headers: keyHeaders() });
      els.docList.replaceChildren(...data.documents.map((d) =>
        el("li", {}, el("span", { text: d.name }), el("span", { class: "muted", text: sizeText(d.size) }))));
      if (!data.documents.length) els.docList.append(el("li", { class: "muted", text: "No documents yet." }));
      const ix = data.indexer || {};
      els.indexerStatus.textContent = ix.status === "local"
        ? "Local mode: .md and .txt files are searchable immediately."
        : `Indexing: ${ix.status || "unknown"}` +
          (ix.lastRunEnd ? ` · last run ${new Date(ix.lastRunEnd).toLocaleString()}` : "") +
          (ix.itemsFailed ? ` · ${ix.itemsFailed} file(s) failed` : "") +
          (ix.errors && ix.errors.length ? ` · ${ix.errors[0]}` : "");
    } catch (err) {
      els.docList.replaceChildren();
      els.indexerStatus.textContent = err.message;
    }
  }

  els.docsButton.addEventListener("click", () => {
    els.uploadKey.value = storage.get(KEY_KEY, "");
    uploadStatus("");
    els.dialog.showModal();
    loadDocuments();
  });
  els.uploadKey.addEventListener("change", () => storage.set(KEY_KEY, els.uploadKey.value));
  els.refreshDocs.addEventListener("click", loadDocuments);

  els.uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const file = els.file.files[0];
    if (!file) return;
    const maxMb = Number(els.uploadForm.dataset.maxMb || 16);
    if (file.size > maxMb * 1048576) { uploadStatus(`The file is larger than ${maxMb} MB.`, true); return; }
    const body = new FormData();
    body.append("file", file);
    els.uploadButton.disabled = true;
    uploadStatus(`Uploading ${file.name}…`);
    try {
      const result = await api("/api/documents", { method: "POST", headers: keyHeaders(), body });
      storage.set(KEY_KEY, els.uploadKey.value);
      uploadStatus(result.indexing === "local"
        ? `${result.name} added.`
        : `${result.name} uploaded. Indexing ${result.indexing}; it is searchable in a few minutes.`);
      els.uploadForm.reset();
      loadDocuments();
    } catch (err) {
      uploadStatus(err.message, true);
    } finally {
      els.uploadButton.disabled = false;
    }
  });

  // ------------------------------------------------------------------ start
  api("/api/config").then((cfg) => {
    els.title.textContent = cfg.title;
    els.subtitle.textContent = cfg.subtitle;
    els.docsButton.hidden = !cfg.uploadEnabled;
    els.uploadForm.dataset.maxMb = String(cfg.maxUploadMb);
  }).catch(() => { /* the static title stays */ });

  render();
  els.input.focus();
})();
