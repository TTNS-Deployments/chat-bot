# Configuration – AI Based Chatbot

*AI based chatbot platform* · Author: Vaibhav Chaudhary

The web app reads environment variables. On Azure they are App Service app settings written by the template (`Deployment/bicep/main.bicep`), so redeploying restores them. Locally they come from `.env` (copy `.env.example`). Nothing in this list is a secret except `UPLOAD_API_KEY`, which on Azure is a Key Vault reference.

## Set by the deployment

| Variable | Example | Purpose |
|---|---|---|
| `CHATBOT_MODE` | `azure` | `azure` uses the Azure services; `local` uses the sample documents and an extractive stand-in for the model. Defaults to `azure` when `AZURE_OPENAI_ENDPOINT` is set, otherwise `local`. |
| `AZURE_OPENAI_ENDPOINT` | `https://<name>.openai.azure.com/` | Azure OpenAI account |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | `chat` | Chat deployment name |
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
| `UPLOAD_API_KEY` | `@Microsoft.KeyVault(VaultName=…;SecretName=upload-api-key)` | Key for the documents API. Locally, leave it empty to allow uploads without a key. |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | | Sends telemetry to Application Insights |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` | App Service installs `requirements.txt` during deployment |

## Optional tuning (not set by the template; add as app settings)

| Variable | Default | Effect |
|---|---|---|
| `SEARCH_TOP_K` | `5` | Passages retrieved per question (1-20). More sources can improve answers but cost more tokens. |
| `MAX_SOURCE_CHARS` | `12000` | Character budget for sources in the prompt. Each passage is also capped at 3,000 characters. |
| `MAX_HISTORY_TURNS` | `6` | Earlier question/answer pairs sent to the model. |
| `CHAT_TEMPERATURE` | `0.2` | Sampling temperature. Reasoning models that reject it are detected automatically and called without it. |
| `CHAT_MAX_TOKENS` | `800` | Maximum answer length (`max_completion_tokens`). |
| `RATE_LIMIT_PER_MINUTE` | `20` | Questions per client IP per minute, per instance. `0` disables the limit. |
| `MAX_UPLOAD_MB` | `16` | Upload size limit. |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Azure OpenAI data-plane API version. |
| `AZURE_SEARCH_API_VERSION` | `2024-07-01` | AI Search REST API version. |
| `AZURE_CLIENT_ID` | | Client ID of a user-assigned managed identity, if you switch the web app to one. |
| `LOCAL_DATA_DIR` | `.localdata` | Local mode: where uploaded files are stored. |

## Changing the answer behaviour

The system prompt is in `Code/backend/chatbot/prompts.py` (`SYSTEM_PROMPT`). It asks for answers only from the numbered sources, with `[n]` citations, and an explicit "not found" when the sources do not cover the question. Change the wording there (for example tone, language or answer length) and redeploy the code (`deploy.sh --skip-infra`). The unit tests in `Code/backend/tests/test_prompts.py` check the citation contract.

The chunk size (2,000 characters with 300 overlap) and the indexer schedule (30 minutes) are in `Code/backend/chatbot/search.py`. The app applies changes to the skillset and indexer on its next start. Run a reset and run of the indexer to re-chunk existing documents.
