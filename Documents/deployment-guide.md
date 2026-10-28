# Deployment Guide – AI Based Chatbot

*AI based chatbot platform* · Author: Vaibhav Chaudhary

## Prerequisites

| Requirement | Notes |
|---|---|
| Azure subscription | **Owner** on the target resource group (or Contributor + User Access Administrator), because the template creates role assignments. |
| Azure OpenAI | The subscription needs access to Azure OpenAI and **quota** for the chat and embedding models with the chosen deployment types in `openAiLocation` (check *Azure AI Foundry → Quotas*). `validate_skus.py --check-region` warns when the free quota looks too low. |
| Resource providers | `Microsoft.Web`, `Microsoft.CognitiveServices`, `Microsoft.Search`, `Microsoft.Storage`, `Microsoft.KeyVault`, `Microsoft.OperationalInsights`, `Microsoft.Insights`. Register any missing one with `az provider register -n <namespace>`. |
| Tools (scripts only) | Azure CLI 2.60+ (`az login`), Python 3.10+, and bash (Linux, macOS, WSL, Cloud Shell) or PowerShell 7. The dev container in `.devcontainer/` has all of them. |

## 1. Choose SKUs

Pick a preset in `Deployment/ARM/parameters/` (`dev`, `standard`, `prod`), edit it, or copy it to a new name. [Deployment/README.md](../Deployment/README.md) explains every parameter. You can also override single values on the command line with `--set name=value` (bash) or `-Set name=value,name2=value2` (PowerShell) without editing the file.

Check the selection before deploying:

```bash
python3 Deployment/scripts/validate_skus.py -e dev                  # static checks
python3 Deployment/scripts/validate_skus.py -e dev --check-region   # plus region, model and quota checks (az login)
```

## 2a. Deploy with the script (recommended)

```bash
az login
az account set --subscription "<subscription>"
Deployment/scripts/deploy.sh -g rg-chatbot-dev -e dev --seed
```

PowerShell:

```powershell
az login
./Deployment/scripts/deploy.ps1 -ResourceGroup rg-chatbot-dev -Environment dev -Seed
```

The script:

1. Validates the SKU selection, including region and model availability (`--skip-region-check` skips the Azure part).
2. Creates the resource group in the preset's `location` (override with `-l`).
3. Deploys `azuredeploy.json` with the preset and your overrides. It grants **you** data access for ingestion, and keeps the existing administrator key if Key Vault already has one.
4. Packages the code and deploys it to the web app (remote build).
5. Waits until `/healthz` reports `"searchSetup": "ready"`, meaning the index and indexer exist.
6. With `--seed`, uploads `Data/sample_docs` and starts the indexer.

Re-running is safe. Use `--skip-infra` to deploy only code, `--skip-code` to deploy only infrastructure. The deployment name is `chatbot-<preset>`.

## 2b. Deploy with the button

The **Deploy to Azure** button in the README opens a portal form built from `createUiDefinition.json`:

| Tab | Fields |
|---|---|
| Basics | Subscription, resource group, region (for everything except Azure OpenAI), solution prefix, environment |
| Azure OpenAI | Azure OpenAI region; chat model, version, deployment type and capacity; embedding model, version, deployment type and capacity |
| Azure AI Search | Tier, replicas and partitions (limited to what the tier allows), semantic ranker |
| Hosting and data | App Service plan and instance count, storage redundancy, Key Vault tier, log retention and daily cap |
| Application | Deploy the application code from Git (default **Yes**), repository URL and branch, document upload on/off |

With *Deploy the application code = Yes*, App Service pulls this repository's `main` branch and builds it during the deployment, so the chatbot is ready at the end. With *No*, deploy the code afterwards:

```bash
Deployment/scripts/deploy.sh -g <rg> -e dev --skip-infra
```

The portal gives its deployment another name (for example `Microsoft.Template-…`). With `--skip-infra`, the script uses the latest chatbot deployment in the resource group when `chatbot-<preset>` does not exist, or you can name it with `-n <deployment-name>`.

> The button needs the template URLs to be publicly readable (this repository is public). For a private fork, publish the two JSON files as a Template Spec or to a storage account with a SAS token, and update the button URL.

## 2c. Demo from Azure Cloud Shell

See [demo-deployment.md](demo-deployment.md): `Deployment/scripts/deploy.sh -e demo --param demoPassword='...'`, using management (ARM) calls only.

## 2d. Deploy with GitHub Actions

`.github/workflows/deploy.yml` (manual run) deploys any preset through OIDC, with no stored passwords. Set it up once:

1. Create an app registration or user-assigned identity for GitHub. Add a federated credential for `repo:<org>/<repo>:environment:<env>`.
2. Grant it **Owner** (or Contributor + User Access Administrator) on the resource group.
3. Create a GitHub environment for each preset (`dev`, `standard`, `prod`) with these variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` and `AZURE_RESOURCE_GROUP`.
4. Run *Actions → Deploy → Run workflow*.

## 3. After deployment

1. **Health:** open `https://<web app>/healthz`. Expect `"status": "ok"` and `"searchSetup": "ready"`. `pending` means the app is still setting up; it retries for about 5 minutes. `failed` includes the reason; usually the role assignments were not active yet, so wait a few minutes and restart the web app.
2. **Administrator key** (only for deleting documents through the API): `az keyvault secret show --vault-name <key vault> -n upload-api-key --query value -o tsv`. You need Key Vault Secrets User or Officer on the vault; the scripts grant it to the deploying identity. Portal deployments: assign yourself *Key Vault Secrets User* on the vault first.
3. **Add documents:** use **Documents** on the chat page (no key needed), or run `Deployment/scripts/ingest.sh -g <rg> ./my-docs` (it uses the latest chatbot deployment in the group; `-n` picks another). The indexer status and per-file errors appear in the Documents dialog.
4. **Smoke test:** after `--seed`, ask "What does the warranty cover?". The answer should cite `warranty-policy.md`.
5. **Replace the sample documents** with your own, and delete the samples from the container (the indexer removes them from the index on its next run).
6. **Optional, sign-in:** to restrict the chatbot to your organisation, turn on App Service Authentication:
   `az webapp auth microsoft update -g <rg> -n <web app> --create-client-secret-name auth --issuer https://login.microsoftonline.com/<tenant-id>/v2.0 --yes` (or *Web app → Authentication → Add identity provider → Microsoft* in the portal), then set *Unauthenticated requests* to *HTTP 302 redirect*. Exclude `/healthz` from authentication if the App Service health check reports failures.

## Moving from local mode to Azure

Locally everything runs without Azure (see [local-development.md](local-development.md)). To run the app on your machine against a deployed environment, set `CHATBOT_MODE=azure` and the `AZURE_*` values from the deployment outputs in `.env`. Your `az login` identity is used; `deploy.sh` grants it the needed roles.

## Troubleshooting

| Symptom | Check |
|---|---|
| `validate_skus.py`: model not offered in the region | Choose a model/version/deployment type from `az cognitiveservices model list -l <region> -o table`, or change `openAiLocation`. |
| Deployment fails with `InsufficientQuota` on a model deployment | Lower `chatCapacity`/`embeddingCapacity`, use another deployment type or region, or request quota. |
| `AuthorizationFailed` on role assignments | You need Owner, or Contributor + User Access Administrator, on the resource group. |
| A Key Vault or Azure OpenAI name already exists / is soft-deleted | Purge it: `az keyvault purge -n <name>`; `az cognitiveservices account purge -n <name> -g <rg> -l <region>`. |
| `/healthz` shows `"searchSetup": "failed"` | The detail says why. Usually roles had not propagated: restart the web app. If the index exists with another vector size, see "Changing the embedding model" in [Deployment/README.md](../Deployment/README.md). |
| Deleting a document returns "administrator key is not available yet" | The Key Vault reference has not resolved. Wait a few minutes and restart the web app (*Configuration* shows a red mark on unresolved references). |
| Chat returns "The service could not answer right now" | Application Insights → Failures shows the underlying error (deployment name, permissions, model retired). |
| Answers say the information was not found | Documents dialog → indexing status and errors; confirm the file has a text layer (scanned PDFs need OCR). |
| Portal deployment fails on `sourcecontrols` (Git deploy) | The infrastructure is in place; deploy the code with `deploy.sh --skip-infra`, or check *Web app → Deployment Center → Logs*. |
| `ingest.sh` keeps retrying | New role assignments take up to 10 minutes. If it never succeeds, check that you have Storage Blob Data Contributor and Search Service Contributor. |
