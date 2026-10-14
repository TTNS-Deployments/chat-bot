# Local Development – AI Based Chatbot

*AI based chatbot platform* · Author: Vaibhav Chaudhary

## Run it without Azure

Local mode replaces the Azure services with local equivalents, so you can work on the page and the API without a subscription:

| Azure service | Local replacement |
|---|---|
| Azure AI Search | Keyword scoring over `Data/sample_docs` and uploaded `.md`/`.txt` files |
| Azure OpenAI | An extractive stand-in that quotes the best sentences from the top sources, with citations |
| Blob Storage | The `.localdata/documents` folder |
| Key Vault | `UPLOAD_API_KEY` in `.env` (empty = uploads without a key) |

```bash
make setup     # creates .venv and installs everything
make run       # http://localhost:8000
make test      # unit tests (prompt building, retrieval, API, deployment consistency)
```

Without `make`:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cd Code/backend && uvicorn chatbot.main:app --reload --port 8000
```

The dev container (`.devcontainer/`) has Python 3.12, the Azure CLI with Bicep, PowerShell, shellcheck and PSScriptAnalyzer, and runs `make setup hooks` on creation.

Docker alternative: `docker build -t ai-based-chatbot . && docker run -p 8000:8000 ai-based-chatbot`.

## Run it against a deployed environment

Copy `.env.example` to `.env`, set `CHATBOT_MODE=azure` and fill in the `AZURE_*` values from the deployment outputs (`az deployment group show -g <rg> -n chatbot-dev --query properties.outputs`). Sign in with `az login`. The deploy scripts gave your identity the roles it needs.

```bash
make run                                   # make loads .env

# Command line tools (from Code/backend, with the .env variables exported):
set -a; . ../../.env; set +a
../../.venv/bin/python -m chatbot ask "What does the warranty cover?"
../../.venv/bin/python -m chatbot indexer-status
```

## API

Interactive API documentation is at `/api/docs` (OpenAPI at `/api/openapi.json`).

```bash
curl -s localhost:8000/api/chat -H 'content-type: application/json' \
  -d '{"message":"How do I return a product?","history":[]}'
```

```json
{"answer": "Start the return from the My orders page or by calling customer support [1]. …",
 "citations": [{"id": 1, "title": "returns-and-refunds.md", "snippet": "…"}]}
```

| Status | Meaning |
|---|---|
| 422 `invalid_request` | Message empty or longer than 4,000 characters, history longer than 40 messages, or an unknown role |
| 429 `rate_limited` | More than `RATE_LIMIT_PER_MINUTE` questions from one IP in a minute |
| 502 `upstream_error` | Azure OpenAI or AI Search failed (details in Application Insights) |
| 503 `busy` | Azure OpenAI or AI Search throttled the request; try again |
| 400 `content_filtered` | The question or answer was blocked by the Azure OpenAI content filter |

## Infrastructure changes

1. Edit `Deployment/bicep/main.bicep` or a module.
2. `make build-arm` (or `Deployment/scripts/build_arm.sh`) to regenerate `Deployment/ARM/azuredeploy.json`.
3. If you added a parameter, add it to the SKU files and `createUiDefinition.json`. `make test` fails if the portal form, the parameter files and the template disagree.
4. Commit the Bicep change **and** `azuredeploy.json` together. With `make hooks` the pre-commit hook refuses a commit whose `azuredeploy.json` is stale, and CI checks the same thing.

Lint everything with `make lint` (shellcheck, PSScriptAnalyzer, Bicep linter).
