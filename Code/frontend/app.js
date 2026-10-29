// AI Based Chatbot – chat page. Plain JavaScript, no build step.
// The conversation lives in this tab (sessionStorage) and is sent with each question so the
// backend can resolve follow-ups. Model output is rendered with DOM text nodes, never innerHTML.
// The server also stores the conversation (masked) under a random ID, for the "Download chat"
// export, and remembers which documents it is limited to (a question that names a document). Adding
  // documents needs no key.
(() => {
  "use strict";

  const MAX_HISTORY = 12; // messages sent with each question (the backend trims further)
  const MAX_HISTORY_CHARS = 8000; // per message, the API's limit for history entries
  const STORE_KEY = "chatbot.conversation";
  const ID_KEY = "chatbot.conversationId";

  const $ = (id) => document.getElementById(id);
  const els = {
    messages: $("messages"), welcome: $("welcome"), thinking: $("thinking"),
    error: $("error"), errorText: $("error-text"), retry: $("retry"),
    form: $("composer"), input: $("input"), send: $("send"), newChat: $("new-chat"),
    chat: $("chat"), suggestions: $("suggestions"), title: $("title"), subtitle: $("subtitle"),
    docsButton: $("docs-button"), dialog: $("docs-dialog"), uploadForm: $("upload-form"),
    file: $("file"), uploadButton: $("upload-button"),
    uploadStatus: $("upload-status"), docList: $("doc-list"), indexerStatus: $("indexer-status"),
    refreshDocs: $("refresh-docs"), signOut: $("sign-out"),
    downloadMenu: $("download-menu"),
  };

  const storage = {
    get(key, fallback) { try { const v = sessionStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
    set(key, value) { try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
    remove(key) { try { sessionStorage.removeItem(key); } catch { /* storage unavailable */ } },
  };

  /** @type {{role: "user"|"assistant", content: string, scope?: string[], citations?: {id:number,title:string,snippet:string,section?:string}[]}[]} */
  let conversation = storage.get(STORE_KEY, []);
  let conversationId = storage.get(ID_KEY, null);
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
            el("summary", {}, el("b", { text: `[${c.id}]` }), c.section ? `${c.title} – ${c.section}` : c.title),
            el("p", { class: "snippet", text: c.snippet }))));
      }
      li.append(el("section", { class: "sources", "aria-label": "Sources" }, el("h3", { text: "Sources" }), list));
    }
    if (msg.scope && msg.scope.length) li.append(el("p", { class: "scope-note", text: `Searched only: ${msg.scope.join(", ")}` }));
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
    if (response.status === 401 && body && body.error && body.error.code === "sign_in_required") {
      window.location.replace("/login.html"); // demo sign-in expired
      throw new Error("Please sign in.");
    }
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
    const history = conversation.slice(-MAX_HISTORY).map(({ role, content }) => ({ role, content: content.slice(0, MAX_HISTORY_CHARS) }));
    conversation.push({ role: "user", content: question });
    render();
    setBusy(true);
    try {
      const result = await api("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: question, history, conversationId }),
      });
      conversation.push({ role: "assistant", content: result.answer, citations: result.citations, scope: result.scope });
      lastQuestion = null;
      conversationId = result.conversationId || conversationId;
      storage.set(ID_KEY, conversationId);
      storage.set(STORE_KEY, conversation);
      if (result.action && result.action.type === "download") downloadHistory(result.action.format);
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
    if (!q || busy) return; // while an answer is loading, keep the next question in the box
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
  // ------------------------------------------------------------------ chat history download
  // Only ever started by the user: the "Download chat" menu or a "download chat history" message.
  async function downloadHistory(format) {
    els.downloadMenu.open = false;
    if (!conversationId) { showError("There is nothing to download yet. Ask a question first."); return; }
    let response;
    try {
      response = await fetch(`/api/history/export?format=${encodeURIComponent(format)}`, { headers: { "X-Conversation-Id": conversationId } });
    } catch {
      showError("Could not reach the server. Check your connection and try again.");
      return;
    }
    if (!response.ok) {
      let message = `The download failed (${response.status}).`;
      try { message = (await response.json()).error.message || message; } catch { /* not JSON */ }
      showError(message);
      return;
    }
    const blob = await response.blob();
    const name = (/filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "") || [])[1] || `chat-history.${format}`;
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: name });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  els.downloadMenu.addEventListener("click", (e) => {
    const item = e.target.closest("button[data-format]");
    if (item) downloadHistory(item.dataset.format);
  });
  document.addEventListener("click", (e) => {
    if (els.downloadMenu.open && !els.downloadMenu.contains(e.target)) els.downloadMenu.open = false;
  });

  function forgetConversation() {
    if (conversationId) {
      fetch("/api/history", { method: "DELETE", headers: { "X-Conversation-Id": conversationId } }).catch(() => { /* expires anyway */ });
    }
    conversationId = null;
    storage.remove(ID_KEY);
  }

  els.newChat.addEventListener("click", () => {
    conversation = [];
    lastQuestion = null;
    forgetConversation();
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
  const sizeText = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);

  function uploadStatus(text, bad = false) {
    els.uploadStatus.textContent = text;
    els.uploadStatus.classList.toggle("bad", bad);
  }

  async function loadDocuments() {
    els.indexerStatus.textContent = "Loading…";
    try {
      const data = await api("/api/documents");
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
    uploadStatus("");
    els.dialog.showModal();
    loadDocuments();
  });
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
      const result = await api("/api/documents", { method: "POST", body });
      uploadStatus(result.indexing === "local" ? `${result.name} added.`
        : result.indexing === "scheduled" ? `${result.name} uploaded. It is indexed on the next scheduled run (within 30 minutes).`
        : `${result.name} uploaded. Indexing ${result.indexing}; it is searchable in a few minutes.`);
      els.uploadForm.reset();
      loadDocuments();
    } catch (err) {
      uploadStatus(err.message, true);
    } finally {
      els.uploadButton.disabled = false;
    }
  });

  els.signOut.addEventListener("click", async () => {
    forgetConversation();
    try { await fetch("/api/logout", { method: "POST" }); } catch { /* signed out locally anyway */ }
    storage.remove(STORE_KEY);
    window.location.replace("/login.html");
  });

  // ------------------------------------------------------------------ start
  api("/api/config").then((cfg) => {
    els.title.textContent = cfg.title;
    els.subtitle.textContent = cfg.subtitle;
    els.docsButton.hidden = !cfg.uploadEnabled;
    els.signOut.hidden = cfg.authMode !== "password";
    els.uploadForm.dataset.maxMb = String(cfg.maxUploadMb);
  }).catch(() => { /* the static title stays */ });

  render();
  els.input.focus();
})();
