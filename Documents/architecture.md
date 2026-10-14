# Azure Deployment Architecture – AI Based Chatbot

*AI based chatbot platform* · Author: Vaibhav Chaudhary

This document covers:

- the components and why each was chosen
- how a question is answered and how documents are ingested
- security
- scaling and resilience

![Architecture](../imgs/architecture.svg)

The diagram source is [`imgs/architecture.svg`](../imgs/architecture.svg) (hand-written SVG; edit it as text). A text version is at the end of this document.

## 1. Components

| Component | Azure resource (name pattern `<prefix>-<env>-<suffix>-*`) | Purpose | SKU parameters |
|---|---|---|---|
| Web app | App Service (Linux, Python 3.12), FastAPI on gunicorn/uvicorn | Serves the chat page and the API: `/api/chat`, `/api/documents`, `/healthz`. Creates the search index, data source, skillset and indexer on startup. | `appServicePlanSku`, `appServiceInstanceCount` |
| Language models | Azure OpenAI account with a `chat` and an `embedding` deployment | The chat model writes grounded answers. The embedding model vectorises document chunks (indexer) and questions (index vectorizer). | `openAiLocation`, `chat*`, `embedding*` |
| Retrieval | Azure AI Search, index `documents-index` | Hybrid search (keyword + vector) with optional semantic reranking. The indexer reads Blob Storage, splits documents into 2,000-character chunks with overlap, embeds them, and projects one search document per chunk. | `searchSku`, `searchReplicaCount`, `searchPartitionCount`, `semanticSearch` |
| Document store | Storage account (StorageV2), container `documents` | Source files. Blob soft delete lets the indexer remove deleted files from the index. Shared-key access is disabled. | `storageSku` |
| Secrets | Key Vault (RBAC mode) | The document upload key (`upload-api-key`), read by the web app through a Key Vault reference. | `keyVaultSku` |
| Monitoring | Log Analytics + workspace-based Application Insights | App telemetry (requests, dependencies, exceptions), plus diagnostic logs from App Service, AI Search and Azure OpenAI. | `logAnalyticsRetentionDays`, `logAnalyticsDailyQuotaGb` |

### Why these choices

- **App Service (not Container Apps or Functions)** matches the Call-Center-Analysis solution: one always-on web app, remote build from a zip or Git, health check, and managed identity, with no container registry to run.
- **AI Search integrated vectorization (indexer + skillset)** instead of a custom ingestion pipeline: the service cracks PDF, Office, HTML and text files, chunks and embeds them, and retries on its own. The web app only uploads files and starts the indexer, so there is no Functions app, queue or Event Grid subscription to operate.
- **Hybrid + semantic search** gives the best grounding quality for mixed questions (exact terms like product codes plus paraphrased questions).
- **Conversation history in the browser**: the page keeps the conversation (in session storage) and sends recent turns with each question. The server is stateless, so there is no database to run or secure, and instances scale out freely. Conversations are not stored server-side.

## 2. Answering a question (steps 1-5 in the diagram)

1. The browser posts `{message, history}` to `/api/chat`. The API validates length, roles and history size, and applies a per-IP rate limit.
2. The app builds a search query. Short follow-ups such as "and for returns?" are combined with the previous user question, and the query is sent to AI Search as a hybrid query (`search` text plus a `vectorQueries` text query), with semantic reranking when it is enabled.
3. AI Search vectorises the query text itself with the index's Azure OpenAI vectorizer (embedding deployment), so the app makes no separate embedding call.
4. The app takes the top passages (5 by default, within a 12,000-character budget), numbers them `[1]`…`[n]`, and sends the system prompt, the recent conversation (6 turns by default) and the question with the sources to the chat deployment. The system prompt requires answers from the sources only, with citations, and treats source text as data rather than instructions.
5. The app returns the answer and the sources it actually cited (title and snippet). The page links each `[n]` marker to its source.

## 3. Ingesting documents (steps A-C)

- **A.** A user with the upload key adds a file on the chat page (`POST /api/documents`, PDF/Office/HTML/Markdown/CSV/JSON/text, 16 MB max). Alternatively, `Deployment/scripts/ingest.sh` (or `.ps1`) uploads files or folders with `az storage blob upload`. Either way the file lands in the `documents` container, and the indexer is started.
- **B.** The indexer (managed identity, *Storage Blob Data Reader*) reads new and changed blobs. It also runs every 30 minutes, so files added any other way (Storage Explorer, AzCopy, a pipeline) are picked up.
- **C.** The skillset splits the text and calls the embedding deployment (managed identity, *Cognitive Services OpenAI User*). Index projections write one search document per chunk (`chunk_id`, `parent_id`, `title` = file name, `chunk`, `text_vector`).

Deleting a file (page or storage) soft-deletes the blob, and the next indexer run removes its chunks.

Scanned PDFs without a text layer are not OCR'd by this skillset. Add an OCR skill if you need them.

## 4. Security

- **No keys.** Azure OpenAI, AI Search and Storage have key-based auth **disabled**. The web app and the search service authenticate with system-assigned managed identities:

  | Identity | Role | Scope |
  |---|---|---|
  | Web app | Cognitive Services OpenAI User | Azure OpenAI |
  | Web app | Search Index Data Reader, Search Service Contributor | AI Search |
  | Web app | Storage Blob Data Contributor | Storage account |
  | Web app | Key Vault Secrets User | Key Vault |
  | AI Search | Storage Blob Data Reader | Storage account |
  | AI Search | Cognitive Services OpenAI User | Azure OpenAI |
  | Deploying identity (scripts only) | Storage Blob Data Contributor, Search Service Contributor, Search Index Data Contributor, Cognitive Services OpenAI User, Key Vault Secrets Officer | respective resources |

- **One secret**, the upload key, generated at deployment, kept in Key Vault and resolved by App Service. An unresolved Key Vault reference is never accepted as a key.
- **Web hardening:** HTTPS only, TLS 1.2, FTP off, strict CSP, no inline script, model output rendered as text, generic error messages to the browser.
- **Not included:** user sign-in, private endpoints, WAF. See the README security notes for how to add sign-in.

## 5. Scaling and resilience

| Concern | Lever |
|---|---|
| More concurrent users | `appServiceInstanceCount` / a larger `appServicePlanSku`; `chatCapacity` (TPM) |
| More documents | `searchPartitionCount`, `searchSku` |
| More queries per second, read SLA | `searchReplicaCount` (2+) |
| Faster indexing of large batches | `embeddingCapacity` |
| Azure OpenAI throttling | The app retries twice, then returns HTTP 503 "busy"; the page offers *Try again*. |
| Role propagation after first deployment | The app retries search setup ten times, 30 seconds apart; `/healthz` shows the state. |
| Region outage | Single region by design. For DR, deploy a second environment in another region and front both with Front Door. |

## Text version of the diagram (Mermaid)

```mermaid
flowchart LR
    U[Users<br/>browser] -->|1 question| W[App Service<br/>chat page + API<br/>managed identity]
    W -->|5 answer + citations| U
    W -->|2 hybrid query| S[Azure AI Search<br/>documents-index]
    S -->|3 vectorise query| E[Azure OpenAI<br/>embedding]
    W -->|4 question + sources| C[Azure OpenAI<br/>chat]
    W -->|A upload| B[(Blob Storage<br/>documents)]
    B -->|B indexer reads| S
    S -->|C embed chunks| E
    K[Key Vault<br/>upload-api-key] -.->|Key Vault reference| W
    W & S & C -.-> M[Application Insights<br/>Log Analytics]
```
