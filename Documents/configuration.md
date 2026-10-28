# Configuration – AI Based Chatbot

*AI based chatbot platform* · Author: Vaibhav Chaudhary

The web app reads environment variables. On Azure they are App Service app settings written by the template (`Deployment/bicep/main.bicep`), so redeploying restores them. Locally they come from `.env` (copy `.env.example`). Nothing in this list is a secret except `UPLOAD_API_KEY` (the administrator key for deleting documents), which on Azure is a Key Vault reference.

## Set by the deployment

| Variable | Example | Purpose |
|---|---|---|
| `CHATBOT_MODE` | `azure` | `azure` uses the Azure services; `local` uses the sample documents and an extractive stand-in for the model. Defaults to `azure` when `AZURE_OPENAI_ENDPOINT` is set, otherwise `local`. |
| `AZURE_OPENAI_ENDPOINT` | `https://<name>.openai.azure.com/` | Azure OpenAI account |
| `AZURE_OPENAI_API_VERSION` | `2025-04-01-preview` | Azure OpenAI data-plane API version |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | `chat` | Chat deployment name |
| `CHAT_REASONING_EFFORT` | `low` | Sent to reasoning models such as `gpt-5-mini` (`minimal`, `low`, `medium`, `high`). `none` sends no effort and allows `CHAT_TEMPERATURE` instead. |
| `AZURE_OPENAI_EMBEDDING_ENDPOINT` | `https://<name>.openai.azure.com/` | Azure OpenAI account of the embedding model (may differ from the chat account) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `embedding` | Embedding deployment name (used by the index vectorizer and the indexer skillset) |
| `AZURE_OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding model name |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `1536` | Vector size of the index (3072 for `text-embedding-3-large`) |
| `AZURE_SEARCH_ENDPOINT` | `https://<name>.search.windows.net` | AI Search service |
| `AZURE_SEARCH_INDEX` | `documents-index` | Index name. The data source, skillset and indexer are named `<index>-datasource`, `-skillset`, `-indexer`. |
| `AZURE_SEARCH_SEMANTIC_CONFIG` | `default` | Semantic configuration name; empty when `semanticSearch` is `disabled` |
| `AZURE_STORAGE_ACCOUNT_URL` | `https://<name>.blob.core.windows.net/` | Blob endpoint for uploads |
| `AZURE_STORAGE_CONTAINER` | `documents` | Container the indexer reads |
| `AZURE_STORAGE_RESOURCE_ID` | `/subscriptions/…/storageAccounts/<name>` | Lets the indexer connect with its managed identity (no key) |
| `ALLOW_DOCUMENT_UPLOAD` | `True` | Show and accept uploads |
| `AUTH_MODE` | `none` | `none` or `password` (puts the chat page behind one shared password). Any other value stops the app at startup, so a typo cannot switch sign-in off. |
| `DEMO_PASSWORD` | `@Microsoft.KeyVault(VaultName=…;SecretName=demo-password)` | The shared password (only with `AUTH_MODE=password`). If it is empty or the reference is unresolved, sign-in is refused. |
| `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_DATASOURCE_CONNECTION`, `AZURE_OPENAI_EMBEDDING_KEY` | Key Vault references | Only for a Free-tier search service, which has no managed identity: the search admin key, the storage connection string for the indexer, and the embedding key for the indexer and vectorizer |
| `WEBSITE_RUN_FROM_PACKAGE`, `PYTHONPATH` | SAS URL, `/home/site/wwwroot/.packages` | Only for a run-from-package deployment (`deploy.sh --code-via-arm`) |
| `UPLOAD_API_KEY` | `@Microsoft.KeyVault(VaultName=…;SecretName=upload-api-key)` | Only for deleting documents (`DELETE /api/documents/{name}`, header `X-Upload-Key`). Adding and listing documents need no key. Locally, leave it empty to allow deletes without a key. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | | Sends telemetry to Application Insights |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` | App Service installs `requirements.txt` during deployment |

## Optional tuning (not set by the template; add as app settings)

| Variable | Default | Effect |
|---|---|---|
| `SEARCH_TOP_K` | `5` | Passages retrieved per question (1-20). More sources can improve answers but cost more tokens. |
| `MAX_SOURCE_CHARS` | `12000` | Character budget for sources in the prompt. Each passage is also capped at 3,000 characters. |
| `MAX_HISTORY_TURNS` | `6` | Earlier question/answer pairs sent to the model. |
| `CHAT_TEMPERATURE` | not sent | Sampling temperature, only used when `CHAT_REASONING_EFFORT=none`. Models that reject it are detected and called without it. |
| `CHAT_MAX_TOKENS` | `2000` | Maximum answer length (`max_completion_tokens`); for reasoning models this includes the reasoning tokens. |
| `SUMMARY_MAX_TOKENS` | `150` | Answer tokens for a short summary ("summarize …"). Reasoning models get 400 more for reasoning, at `minimal` effort. The answer is also cut to about 90 words at a sentence boundary. Detailed summaries use `CHAT_MAX_TOKENS`. |
| `RATE_LIMIT_PER_MINUTE` | `20` | Questions per client IP per minute, per instance. `0` disables the limit. |
| `TRUST_FORWARDED_FOR` | `true` on App Service, else `false` | Take the client IP for the rate limit and sign-in lockout from the last `X-Forwarded-For` entry (added by the proxy). Set `true` only behind a proxy you trust; otherwise the header is ignored, because clients can forge it. |
| `MAX_UPLOAD_MB` | `16` | Upload size limit. |
| `AZURE_SEARCH_API_VERSION` | `2024-07-01` | AI Search REST API version. |
| `AZURE_CLIENT_ID` | | Client ID of a user-assigned managed identity, if you switch the web app to one. |
| `LOCAL_DATA_DIR` | `.localdata` | Local mode: where uploaded files are stored. |
| `ALLOW_SENSITIVE_REVEAL` | `true` | `false` keeps personal and customer information hidden even when a user asks for it. The app has no user roles; this is the switch for deployments where nobody should see such values. |
| `CHAT_HISTORY_DIR` | `/home/data/chatbot/conversations` on App Service, else `.localdata/conversations` | Where conversations are stored for the "Download chat" export. Must be shared by all workers and instances (App Service's `/home` is). |
| `CHAT_HISTORY_DAYS` | `7` | Stored conversations untouched for this many days are deleted. |

## Changing the answer behaviour

The system prompt is in `Code/backend/chatbot/prompts.py` (`SYSTEM_PROMPT`). It asks for answers only from the numbered sources, with `[n]` citations, and an explicit "not found" when the sources do not cover the question. It also states the scope, privacy and summary rules, but those are enforced in code (`scope.py`, `privacy.py`, `summary.py`), so wording changes cannot weaken them. Change the wording there (for example tone, language or answer length) and redeploy the code (`deploy.sh --skip-infra`). The unit tests in `Code/backend/tests/test_prompts.py` check the citation contract.

The chunk size (2,000 characters with 300 overlap) and the indexer schedule (30 minutes) are in `Code/backend/chatbot/search.py`. The app applies changes to the skillset and indexer on its next start. Run a reset and run of the indexer to re-chunk existing documents.
