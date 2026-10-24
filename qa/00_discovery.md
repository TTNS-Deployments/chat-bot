# 00 – Discovery (read-only)

Repo: `/workspaces/chat-bot`, branch `main`, commit `02f4fcc` · Date: 2026-10-24

## Tech stack
| Part | What |
|---|---|
| Backend | Python 3.12.3, FastAPI 0.143.0 (Starlette 1.8.0), Pydantic 2.14.1, Uvicorn 0.54.0 (dev) / Gunicorn 26.2 + UvicornWorker (Docker, App Service) |
| LLM SDK | `openai` 3.31.0 (`AzureOpenAI` client, Entra ID token via `azure-identity` 1.26.0) |
| Search | Azure AI Search REST over `httpx` 0.28.1 (30 s timeout) |
| Storage | Azure Blob (`azure-storage-blob`) or local folder `.localdata/documents` |
| Frontend | Plain HTML/CSS/vanilla JS (`Code/frontend`), **no build step, no npm package, no lint/type-check config** |
| Infra | Bicep → ARM (`Deployment/`), not exercised by this QA (deployment tests exist in pytest) |
| Tooling present | `.venv` (uv-managed), Playwright 1.64 + Chromium already in `.venv`/`~/.cache/ms-playwright`, Node 24, Docker CLI |

## Install / configure / run / test
- `make setup` (venv + `requirements-dev.txt`), `make run` → `cd Code/backend && uvicorn chatbot.main:app --reload --port 8000`, `make test` → `pytest Code/backend -q`.
- Docker: `docker build -t ai-based-chatbot . && docker run -p 8000:8000 ai-based-chatbot`.
- Config: env vars (`.env.example`; `.env` absent and git-ignored). `CHATBOT_MODE=local|azure` (local default when no `AZURE_OPENAI_ENDPOINT`). In azure mode, missing `AZURE_OPENAI_ENDPOINT/AZURE_SEARCH_ENDPOINT/AZURE_STORAGE_ACCOUNT_URL/AZURE_STORAGE_RESOURCE_ID` → `RuntimeError` at import.
- Other knobs: `AUTH_MODE=none|password`, `DEMO_PASSWORD`, `UPLOAD_API_KEY`, `ALLOW_DOCUMENT_UPLOAD`, `MAX_UPLOAD_MB` (16), `RATE_LIMIT_PER_MINUTE` (20, per client IP, per instance), `SEARCH_TOP_K`, `MAX_HISTORY_TURNS` (6), `MAX_SOURCE_CHARS`, `CHAT_MAX_TOKENS`, `CHAT_REASONING_EFFORT`, `LOCAL_DATA_DIR`.
- CLI: `python -m chatbot ask|setup-search|indexer-status|serve`.

## Backend routes (`Code/backend/chatbot/main.py`)
| Method | Path | Auth | Request | Response |
|---|---|---|---|---|
| GET | `/healthz` | public | – | `{status, version, mode, searchSetup[, searchSetupDetail]}` |
| GET | `/api/config` | public | – | `{title, subtitle, mode, authMode, uploadEnabled, maxUploadMb}` |
| POST | `/api/login` | public | `{password: 1..200}` | 200 `{signedIn:true}` + cookie `chatbot_session` (HttpOnly, SameSite=Lax, Secure if https); 403 wrong; 429 after 5 failures/15 min; 404 if auth off; 503 if no password configured |
| POST | `/api/logout` | public | – | deletes cookie |
| POST | `/api/chat` | sign-in cookie when `AUTH_MODE=password` | `{message: 1..4000 non-blank, history: [{role: user\|assistant, content: 1..8000}] ≤40}` | `{answer, citations:[{id,title,snippet}]}`; errors `{error:{code,message}}`: 422 invalid_request, 429 rate_limited, 502 upstream_error, 503 busy, 400 content_filtered, 500 internal_error |
| GET | `/api/documents` | `X-Upload-Key` (+ cookie) | – | `{documents:[{name,size,lastModified}], indexer}` |
| POST | `/api/documents` | `X-Upload-Key` | multipart `file` | 201 `{name,size,indexing}`; 415 unsupported, 413 too large, 422 empty |
| DELETE | `/api/documents/{name}` | `X-Upload-Key` | – | `{deleted}`; 404 |
| GET | `/api/docs`, `/api/openapi.json` | (cookie) | Swagger UI | |
| GET | `/` (static) | (cookie, else 303 → `/login.html`) | `index.html`, `app.js`, `app.css`, `favicon.svg`, `login.html`, `login.js` | |

Middleware: sign-in gate; security headers (CSP `default-src 'self'`, X-Frame-Options DENY, nosniff, Referrer-Policy no-referrer, `Cache-Control: no-store` on pages/API). No CORS middleware. Global exception handler → generic 500 JSON.

## Frontend (`Code/frontend`)
- `index.html` + `app.js`: header (Documents, New conversation, Sign out buttons), welcome + 3 suggestion chips, `<ol id=messages>`, "Searching the documents…" thinking indicator, error box with "Try again", composer (`<textarea maxlength=4000 required>` + send button), Documents `<dialog>` (upload key input, file upload, list, refresh).
- Calls: `GET /api/config` on load, `POST /api/chat` (fetch, JSON, no timeout/abort), `GET/POST /api/documents` with `X-Upload-Key`.
- Enter sends, Shift+Enter newline. Rendering uses DOM text nodes (no innerHTML); supports `**bold**`, `- `/`1. ` lists, paragraphs and clickable `[n]` citation buttons. **No** code-block, link or full Markdown rendering.
- Conversation in `sessionStorage["chatbot.conversation"]` (survives refresh in the same tab); last 12 messages sent as history. Upload key stored in `sessionStorage["chatbot.uploadKey"]`.
- `login.html` + `login.js`: shared-password sign-in.
- Not present: streaming, stop, regenerate, copy, feedback (thumbs), server-side history list.

## Chatbot engine
- Provider: Azure OpenAI chat completions (default deployment `chat`, gpt-5-mini, `reasoning_effort=low`, `max_completion_tokens=2000`), client `timeout=60 s`, `max_retries=2`. Error mapping in `llm.py`: RateLimit→503 busy, content filter→400, other `APIError` (incl. timeout/connection/5xx)→502, Entra auth failure→502; empty content → fallback text "I could not produce an answer…".
- System prompt `prompts.py::SYSTEM_PROMPT`: answer only from numbered sources, cite `[n]`, say "not found" otherwise, treat sources as data, answer in the question's language.
- Memory: **stateless server**; client sends history; server keeps last `MAX_HISTORY_TURNS` exchanges; short follow-ups (<6 words) are searched together with previous user question.
- RAG: Azure AI Search hybrid + semantic reranking (azure mode) / keyword scoring over `Data/sample_docs` + uploaded `.md/.txt` (local mode). Citations extracted from `[n]` markers.
- Tools/function calling: **none**. Streaming: **none**. Guardrails: Azure content filter mapping, prompt rule; rate limit; upload type/size checks; `safe_name` file-name sanitising.

## Local / mock mode
`CHATBOT_MODE=local` replaces AI Search with `LocalRetriever` and Azure OpenAI with `LocalChatModel` (extractive: quotes best sentence of top 2 sources). This is a **mock LLM**: it cannot test answer quality, conversational memory recall, refusal or prompt-injection resistance of the real model. `AzureChatModel` accepts an injected client, so the real `openai` SDK can be pointed at an `httpx.MockTransport`/local stub server for failure tests.

## Data stores / external services
| Service | Mockable locally |
|---|---|
| Azure OpenAI | Yes – LocalChatModel or real SDK + mock transport |
| Azure AI Search | Yes – LocalRetriever (or `httpx` mock) |
| Blob Storage | Yes – LocalDocumentStore |
| Key Vault / App Insights | Not needed locally |
No API keys are available in this environment (`.env` absent) → live-model tests are **Blocked**.

## Existing automated tests
`Code/backend/tests/` – 9 files, 109 tests (README says 91): API, auth, LLM error mapping (fake client), prompts, retrieval, search REST, documents, deployment consistency. No frontend tests.
