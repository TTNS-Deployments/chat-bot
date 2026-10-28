# Deployment

| File | Purpose |
|---|---|
| `bicep/main.bicep`, `bicep/modules/*.bicep` | **The infrastructure source.** One module per group of concerns: monitoring, storage, Key Vault, Azure OpenAI, AI Search, App Service, role assignments. |
| `ARM/azuredeploy.json` | The ARM template compiled from the Bicep source. Used by the *Deploy to Azure* button and the deploy scripts. **Never edit it by hand**: run `scripts/build_arm.sh`. |
| `ARM/parameters/dev.parameters.json`, `standard.parameters.json`, `prod.parameters.json`, `demo.parameters.json` | **The SKU files (presets).** Choose the tier and size of every service. Copy one to add a preset (for example `uat.parameters.json`). |
| `ARM/createUiDefinition.json` | Portal form used by the *Deploy to Azure* button (a dropdown or slider per SKU, with the Azure AI Search limits built in). |
| `scripts/deploy.sh`, `scripts/deploy.ps1` | Validation, infrastructure and code in one command (bash and PowerShell). `deploy.sh --code-via-arm` deploys the code with management calls only (Azure Cloud Shell). |
| `environments/<env>.env` | Script settings per environment, read by `deploy.sh -e <env>` (for example the demo's resource group). |
| `scripts/ingest.sh`, `scripts/ingest.ps1` | Upload files or folders to the knowledge base and start indexing. |
| `scripts/validate_skus.py` | Checks a preset and overrides against the template, the AI Search scale rules and (with `--check-region`) Azure's regional model, service and quota availability. Both deploy scripts run it first. |
| `scripts/build_arm.sh` | Compiles Bicep into `ARM/azuredeploy.json`; `--check` fails when the committed template is stale (CI and the pre-commit hook). |
| `scripts/package.py` | Builds `build/webapp.zip`. |

Step-by-step instructions are in [Documents/deployment-guide.md](../Documents/deployment-guide.md).

## SKU and sizing parameters

Values are the three presets. "Options" are what the template accepts; anything else is rejected by `validate_skus.py` before Azure is called.

| Parameter | dev | standard | prod | Options / what it affects |
|---|---|---|---|---|
| `solutionPrefix` | aibot | aibot | aibot | 3-8 lowercase letters/digits. Part of every resource name. |
| `environmentName` | dev | test | prod | `dev`, `test`, `uat`, `prod`. Part of resource names and tags. |
| `location` | centralindia | centralindia | centralindia | Region for everything except Azure OpenAI. |
| **App Service** | | | | |
| `appServicePlanSku` | B1 | P0v3 | P1v3 | `B1`-`B3`, `S1`-`S3`, `P0v3`-`P3v3`. Cost and CPU/memory per instance. Basic has no deployment slots or autoscale. |
| `appServiceInstanceCount` | 1 | 1 | 2 | 1-10. Two or more for the App Service SLA and for more concurrent users. |
| **Azure OpenAI** | | | | |
| `openAiLocation` | eastus2 | eastus2 | eastus2 | Must offer both models with the chosen deployment types. Prompts and documents are processed here (see "Data residency" below). |
| `openAiSku` | S0 | S0 | S0 | `S0` (the only tier). |
| `chatModelName` / `chatModelVersion` | gpt-5-mini / 2025-08-07 | same | same | Any chat-completions model your region offers. Larger models answer better, cost more and are slower. |
| `chatDeploymentSku` | GlobalStandard | GlobalStandard | DataZoneStandard | `GlobalStandard` (widest availability, highest default quota), `DataZoneStandard` (processing stays in the US or EU data zone), `Standard` (processing in `openAiLocation`). |
| `chatCapacity` | 30 | 50 | 150 | Thousands of tokens per minute (TPM). One question uses roughly 3-5k tokens with five sources, so 30K TPM is about 6-10 questions a minute. Must fit your quota. |
| `embeddingModelName` | text-embedding-3-small | text-embedding-3-small | text-embedding-3-large | `text-embedding-3-small` (1536 dimensions) or `text-embedding-3-large` (3072, better recall, about 6x the price, larger index). **Changing it later means rebuilding the index.** |
| `embeddingModelVersion` | 1 | 1 | 1 | |
| `embeddingDeploymentSku` | GlobalStandard | GlobalStandard | Standard | As for the chat deployment. |
| `embeddingCapacity` | 30 | 50 | 120 | TPM for indexing and for vectorising questions. Low values only slow down indexing of large document sets. |
| **Azure AI Search** | | | | |
| `searchSku` | basic | basic | standard | `basic`, `standard`, `standard2`, `standard3`. Storage per partition, index count and price. Basic suits a few GB of documents. |
| `searchReplicaCount` | 1 | 2 | 2 | basic: 1-3, standard tiers: 1-12. Query throughput; 2 replicas give the read SLA, 3 the read/write SLA. |
| `searchPartitionCount` | 1 | 1 | 1 | basic: 1-3, standard tiers: 1, 2, 3, 4, 6, 12. Index size. Replicas x partitions must be 36 or less. |
| `semanticSearch` | free | standard | standard | `free` (1,000 semantic queries a month), `standard` (pay per 1,000 queries beyond the free amount), `disabled` (hybrid search without reranking; slightly lower answer quality). |
| **Data and operations** | | | | |
| `storageSku` | Standard_LRS | Standard_ZRS | Standard_ZRS | `Standard_LRS`, `Standard_ZRS`, `Standard_GRS`, `Standard_RAGRS`, `Standard_GZRS`, `Standard_RAGZRS`. Redundancy of the source documents. |
| `keyVaultSku` | standard | standard | standard | `standard`, `premium` (HSM-backed keys; not needed for this solution). |
| `logAnalyticsRetentionDays` | 30 | 30 | 90 | 30-730. Longer retention costs more. |
| `logAnalyticsDailyQuotaGb` | 1 | 5 | -1 | Daily ingestion cap in GB; `-1` = no cap. When the cap is hit, logs stop until the next day. |
| **Application** | | | | |
| `allowDocumentUpload` | true | true | true | Show the upload control on the chat page. Adding documents needs no key. |

Parameters that are **not** in the SKU files, because the scripts or the portal set them:

| Parameter | Set by | Purpose |
|---|---|---|
| `deployerPrincipalId`, `deployerPrincipalType` | deploy scripts | Gives the person or pipeline that deploys data access, for document ingestion. |
| `uploadApiKey` | deploy scripts (`--param`; generated on the first deployment) or the portal form | The administrator key for deleting documents through the API (adding documents needs no key), stored in Key Vault as `upload-api-key`. Written only when a value is passed, so redeploys keep it. |
| `authMode`, `demoPassword` | SKU file / `--param` / portal form | `password` puts the chat page behind one shared password, stored in Key Vault as `demo-password` (kept on redeploy when not passed). |
| `appPackageUrl` | `deploy.sh --code-via-arm` | Run-from-package URL; passed into infrastructure redeploys so they keep the running code. |
| `deployAppCode`, `repositoryUrl`, `repositoryBranch` | portal form (default on) | Lets the button deploy the application code from this public repository. The scripts deploy the code themselves and leave it off. |
| `tags` | SKU files | Extra tags on every resource. |

## The demo preset

`demo.parameters.json` is the cheapest setup that still runs everything, for an always-on, show-only demo deployed from Azure Cloud Shell: the web app joins an existing App Service plan (`existingAppServicePlanName`), AI Search is `free`, Azure OpenAI is pay-per-token (`gpt-5-mini` in South India, `text-embedding-3-small` in East US through `embeddingLocation`), storage is LRS with shared-key access on, diagnostic-log export is off, resource names start with `chatbot-` (`shortResourceNames`), and the page asks for a shared password. Steps: [Documents/demo-deployment.md](../Documents/demo-deployment.md).

## Data residency

The default presets place everything in Central India except Azure OpenAI, which uses `eastus2` because both models and deployment types are widely available there. With `GlobalStandard`, prompts may be processed in any Azure region. For data-residency requirements, set `openAiLocation` to your region and use `Standard` deployments, then run `validate_skus.py --check-region` to confirm that the models are offered there.

## Changing the embedding model after deployment

The vector field size is fixed when the index is created. After changing `embeddingModelName`:

1. Redeploy.
2. Delete the index: `az rest --method delete --url "<searchEndpoint>/indexes/documents-index?api-version=2024-07-01" --resource https://search.azure.com`.
3. Restart the web app. It recreates the index with the new size (wait until `/healthz` shows `"searchSetup": "ready"`).
4. Reset and run the indexer so it re-reads every document:
   `az rest --method post --url "<searchEndpoint>/indexers/documents-index-indexer/reset?api-version=2024-07-01" --resource https://search.azure.com`, then `.../run?api-version=2024-07-01` the same way.
