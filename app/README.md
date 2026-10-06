# Application code

Place the chatbot application (the web app that orchestrates retrieval and answer
generation) in this folder. The infrastructure in `templates/` provisions the Linux
App Service that hosts it; the runtime is set by `APP_RUNTIME` in `config/skus.env`.

`deploy.sh` sets these app settings. Secrets are Key Vault references, which the app's
managed identity resolves, so no keys appear in plaintext.

| Setting | Value |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint |
| `AZURE_OPENAI_API_KEY` | Key Vault secret `OpenAIApiKey` |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | Chat deployment name (`chat` by default) |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | Embedding deployment name (`embedding` by default) |
| `AZURE_SEARCH_ENDPOINT` | AI Search endpoint |
| `AZURE_SEARCH_ADMIN_KEY` | Key Vault secret `SearchAdminKey` |
| `COSMOS_ENDPOINT`, `COSMOS_DATABASE`, `COSMOS_CONTAINER` | Cosmos DB endpoint and names |
| `COSMOS_CONNECTION_STRING` | Key Vault secret `CosmosDbConnectionString` |
| `STORAGE_ACCOUNT_NAME`, `STORAGE_BLOB_ENDPOINT`, `STORAGE_CONTAINER` | Storage details |
| `STORAGE_CONNECTION_STRING` | Key Vault secret `StorageConnectionString` |
| `KEY_VAULT_URI` | Key Vault URI |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Set by the App Service template |

The web app's managed identity also holds data-plane roles on each service (Key Vault
Secrets User, Storage Blob Data Contributor, Search Index Data Contributor and Search
Service Contributor, Cognitive Services OpenAI User, Cosmos DB Built-in Data
Contributor), so the app can use identity-based access instead of keys.
