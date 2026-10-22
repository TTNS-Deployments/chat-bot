# Demo Deployment – AI Based Chatbot

*AI based chatbot platform*

A show-only demo with a couple of documents, kept running all the time at the lowest cost. It runs from **Azure Cloud Shell (Bash)**, where only management API (ARM) calls work: Microsoft Graph, the App Service deployment (Kudu) endpoints and Storage/Key Vault sign-in tokens are not available. Everything below works within that limit, and **no Microsoft Entra ID app registration is needed**.

## What the demo deploys

All settings are in [`Deployment/ARM/parameters/demo.parameters.json`](../Deployment/ARM/parameters/demo.parameters.json) and [`Deployment/environments/demo.env`](../Deployment/environments/demo.env).

| Resource | Name | Choice | Cost when idle |
|---|---|---|---|
| Web app | `chatbot-web-<suffix>` | Its own web app on the **existing** call-center plan `ccai-demo-gqausd-web-plan` (B1, Linux). The plan is not changed. | None extra (billing is per plan) |
| Azure OpenAI, chat | `chatbot-openai-<suffix>` (South India) | `gpt-5-mini` 2025-08-07, Global Standard, 20K TPM, reasoning effort low | None (pay per token) |
| Azure OpenAI, embeddings | `chatbot-embed-<suffix>` (East US) | `text-embedding-3-small`, Standard, 10K TPM | None (pay per token) |
| Azure AI Search | `chatbot-search-<suffix>` | **Free** tier (50 MB, 3 indexes, no semantic ranker) | None |
| Storage | `chatbotst<suffix>` | Standard LRS; shared-key access on (needed for Cloud Shell and the Free search tier) | A few rupees a month |
| Key Vault | `chatbot-kv-<suffix>` | Standard; holds the demo password, the upload key and the search keys | A few rupees a month |
| Log Analytics + Application Insights | `chatbot-logs`, `chatbot-insights` | 30 days, 1 GB/day cap; diagnostic log export turned off | Close to nothing |

The suffix is six characters, fixed for the resource group, so names stay the same on every redeploy. The call-center `ccai-*` resources are never touched.

**Sign-in:** the chat page asks for one shared password (`authMode=password`). It is stored in Key Vault as `demo-password`. After 5 wrong passwords, sign-in is blocked for 15 minutes for that address (counted per web worker).

**Why the Free search tier uses keys:** a Free search service cannot have a managed identity (Basic tier or higher is required). So the search service reads the documents and calls the embedding model with keys, and the web app calls the search service with its admin key. The deployment copies these keys into Key Vault itself; nobody handles them. The web app still uses its managed identity for Azure OpenAI, Storage and Key Vault.

**Data location:** documents and questions are embedded in East US, and answers are written in South India. Everything else is in Central India.

## Before the first deployment (once)

In Cloud Shell:

```bash
az provider register -n Microsoft.Search --wait
```

## Deploy

Choose the demo password, put it between the quotes, and run:

```bash
cd ~ && (git clone https://github.com/TTNS-Deployments/chat-bot.git 2>/dev/null || true) && cd ~/chat-bot && git pull && Deployment/scripts/deploy.sh -e demo --param demoPassword='CHOOSE-A-PASSWORD'
```

What it does:

1. Checks the settings, the regions, the models and the quota, that the shared plan exists and is Linux in Central India, and that no other Free search service exists in the subscription.
2. Deploys the template into `Vaibhav.Chaudhary` (the resource group is reused, never recreated).
3. Packages the app **with its dependencies**, uploads it to the storage account with the account key, and points the web app at it (run-from-package, read-only link valid for one year).
4. Uploads the sample documents, restarts the web app, and waits until `/healthz` reports `"searchSetup": "ready"`.
5. Prints the chat page address and, on the first deployment, the **upload key**. Save the upload key: Cloud Shell cannot read it back from Key Vault (the portal can: *Key Vault → Secrets*, with the *Key Vault Secrets User* role).

**Later deployments** (after a `git pull`): run the same command **without** `--param demoPassword=...`. The stored password and upload key are kept, and the running code is kept during the infrastructure step. To change the password or the key, pass `--param demoPassword='...'` or `--param uploadApiKey='...'` again.

Only code changed? `Deployment/scripts/deploy.sh -e demo --skip-infra`.

## Check that it works

1. Open the chat page address. The sign-in page appears; enter the demo password.
2. Ask *What does the warranty cover?*. The answer should cite `warranty-policy.md`. The first answer after a restart can take 30 seconds or more on the shared B1 plan.
3. **Documents** → paste the upload key → upload a small file. It is searchable within a few minutes. The Free tier runs an indexer at most every 3 minutes, limits each run to 3–10 minutes, and may process at most 20 documents per indexer per day through the AI pipeline: enough for a couple of documents, not for bulk loading.
4. If something fails: *Web app → Log stream* in the portal, or `https://<web app>/healthz` (`searchSetupDetail` gives the reason).

## Adding the demo documents by script

```bash
cd ~/chat-bot && Deployment/scripts/ingest.sh -g Vaibhav.Chaudhary --account-key --restart ./my-docs
```

`--account-key` uploads with the storage key (Cloud Shell has no Storage token). `--restart` restarts the web app, which starts the indexer on startup. Keep the total under the Free tier's 50 MB.

## Troubleshooting

| What you see | What to do |
|---|---|
| `already has a Free search service` | Only one is allowed per subscription. Use Basic: add `--set searchSku=basic` (billed every hour). |
| `InsufficientQuota` | The `gpt-5-mini` quota in South India is shared with the call-center demo. Lower it: `--set chatCapacity=10`. For embeddings in East US: `--set embeddingCapacity=5`. |
| A model "is not offered" in a region | Check what is: `az cognitiveservices model list -l eastus -o table` (or `southindia`), then adjust the preset. |
| `/healthz` shows `"searchSetup": "failed"` | Usually Key Vault references were not resolved yet. Restart: `az webapp restart -g Vaibhav.Chaudhary -n <web app>`. |
| Sign-in says it is not configured | The demo password was never set, or Key Vault was not readable yet. Redeploy with `--param demoPassword='...'`, or restart the web app after a few minutes. |
| The page is slow after a restart | Normal on the shared B1 plan; both apps start in turn. |

## Removing the demo

Do **not** delete the resource group: it also holds the call-center demo. Delete only the `chatbot-*` resources:

```bash
az resource list -g Vaibhav.Chaudhary --query "[?starts_with(name, 'chatbot')].id" -o tsv | xargs -r az resource delete --ids
```

Key Vault and Azure OpenAI are soft-deleted. To deploy again with the same names, purge them first:

```bash
az keyvault purge --name chatbot-kv-<suffix>
az cognitiveservices account purge -g Vaibhav.Chaudhary -l southindia -n chatbot-openai-<suffix>
az cognitiveservices account purge -g Vaibhav.Chaudhary -l eastus -n chatbot-embed-<suffix>
```
