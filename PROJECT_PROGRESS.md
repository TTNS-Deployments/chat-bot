# Project progress – AI Based Chatbot

For anyone (person or AI session) picking up this work. Update it after each piece of work.

## Context

- Repository: `TTNS-Deployments/chat-bot` (public). Modelled on `TTNS-Deployments/Call-Center-Analysis`.
- Deploys for Tata Tele Business Services (TTBS). Changes are pushed to `main` when the user asks.
- The demo is deployed from **Azure Cloud Shell (Bash)**: only management API (ARM) tokens work there. Microsoft
  Graph, App Service deployment (Kudu/SCM), Storage and Key Vault tokens fail. The user cannot create Microsoft
  Entra ID app registrations. The Codespace cannot sign in to Azure.
- Models: GPT-5 family only. `gpt-5-mini` 2025-08-07 is the only GPT-5 model with quota (South India, Global
  Standard, 500K TPM shared per subscription with the call-center demo).

## Decisions

| Date | Decision |
|---|---|
| 2026-10-14 | Bicep compiled to `Deployment/ARM/azuredeploy.json`; SKU presets in `Deployment/ARM/parameters/`; FastAPI app; AI Search indexer with integrated vectorization. |
| 2026-10-22 | Demo uses the cheapest option for every resource: web app on the existing call-center B1 plan (`ccai-demo-gqausd-web-plan`, never modified), Free AI Search, LRS storage, no diagnostic-log export, pay-per-token models. |
| 2026-10-22 | Demo sign-in with one shared password (`authMode=password`), no Entra ID. Password in Key Vault (`demo-password`). |
| 2026-10-22 | Chat: own Azure OpenAI resource in South India, `gpt-5-mini`, reasoning effort low, API `2025-04-01-preview`, no temperature. Embeddings: `text-embedding-3-small` in a second resource in **East US** (user's choice). |
| 2026-10-22 | Free AI Search has no managed identity, so in that mode the template copies the search admin key, a storage connection string and the embedding key into Key Vault; the app and the indexer use them. Basic and higher keep managed identities. |
| 2026-10-22 | Demo resource names start with `chatbot-` (`shortResourceNames`), plus a fixed 6-character suffix where Azure needs globally unique names. |
| 2026-10-22 | Cloud Shell code deployment: `package.py --vendor` (dependencies in `.packages`), upload with the storage account key, `WEBSITE_RUN_FROM_PACKAGE` SAS URL (1 year). `deploy.sh` passes the current package URL into infrastructure redeploys so they keep the running code. |
| 2026-10-25 | `deploy.sh` reads the shared plan's location and kind one value at a time: the plan returns `reserved: null` and `kind: linux`, and `-o tsv` prints list queries one value per line. |
| 2026-10-28 | Document scoping, masking of personal information and chat-history export, enforced in code (`scope.py`, `privacy.py`, `history.py`). Document ID = file name (`title` field, already filterable), so no index rebuild. Conversations stored as JSON files on App Service's shared `/home` (two gunicorn workers), masked, 7-day retention. Masking is rule-based (no NER package), .docx built without new dependencies; no PDF export. |
| 2026-10-22 | Secrets (`demoPassword`, `uploadApiKey`) are passed with `--param`, written to Key Vault only when given; redeploys keep them. `deploy.sh` generates the upload key on the first deployment and prints it once. |

## Status

| Item | Status |
|---|---|
| App, tests (188), Bicep/ARM, portal form, presets, scripts, docs | Done; tested locally and against a mock Azure CLI |
| Demo preset (`demo.parameters.json`, `Deployment/environments/demo.env`), password sign-in, Free-search key mode, Cloud Shell code path | Done; not yet deployed |
| Package with dependencies | Verified: runs on a clean Python 3.12 with only `.packages`, using the App Service startup command |
| First live deployment of the demo | **Done 2026-10-25** from Cloud Shell: `deploy.sh -e demo`. Resource suffix `wewqam` (web app `chatbot-web-wewqam`, Key Vault `chatbot-kv-wewqam`, storage `chatbotstwewqam`). `/healthz` reported the search index ready. Secrets are in Key Vault only. |

## Not yet verified on Azure

- `text-embedding-3-small` availability and quota in East US with the Standard deployment type (the validator checks it at deploy time).
- That no other Free search service exists in the subscription (also checked at deploy time).
- Free-tier behaviour with the integrated-vectorization skillset (documented limit: 20 enriched documents per indexer per day).

## Next steps

1. Verify the live demo: sign in, ask a question, upload a document; read the Log stream on errors.
2. Later deployments: `cd ~/chat-bot && git pull && Deployment/scripts/deploy.sh -e demo` (no --param: stored secrets are kept).
3. Regenerate `AI-Based-Chatbot-Artifacts.pdf` if the README changes are to be reflected in it.
