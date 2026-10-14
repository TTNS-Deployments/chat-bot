# Security

## Reporting a vulnerability

Do **not** open a public issue for security problems. Raise a ticket marked *Security* in the DBCH support portal (see [SUPPORT.md](SUPPORT.md)), or contact the TTNS service delivery team directly. Include the steps to reproduce and the potential impact.

## How the solution protects data

| Area | Control |
|---|---|
| Service access | The web app and the Azure AI Search service use **system-assigned managed identities** with least-privilege RBAC roles. Key-based access is **disabled** on Azure OpenAI, Azure AI Search and the storage account, so there are no service keys to leak. |
| Secrets | The only secret, the document upload key, is generated at deployment, stored in **Key Vault** and read by the web app through a Key Vault reference. Nothing secret is stored in the repository or the parameter files. |
| Document upload | Requires the upload key (`X-Upload-Key` header), checked in constant time. Only document file types are accepted, file names are sanitised, and size is capped (16 MB by default). Upload can be switched off with `allowDocumentUpload=false`. |
| Prompt injection | The system prompt tells the model to treat retrieved text as data and to answer only from the sources. Uploads are restricted to key holders because uploaded text becomes grounding data. |
| Chat endpoint | Input is validated (length, roles, history size), and each client IP is rate limited (20 questions a minute by default). Errors returned to the browser do not include internal details. |
| Browser | Strict Content Security Policy (`'self'` only, no inline script), `X-Frame-Options: DENY`, `nosniff`, no referrer. Model output is rendered as text, never as HTML. |
| Transport | HTTPS only, TLS 1.2 minimum, FTP disabled on the web app. |
| Monitoring | Application Insights for the app; diagnostic logs from App Service, Azure AI Search and Azure OpenAI go to Log Analytics. User questions are not written to the logs. |

## Known limits

- The chat page has **no sign-in**: anyone with the URL can ask questions and use Azure OpenAI tokens. For internal use, turn on App Service Authentication (Microsoft Entra ID), see the README's security notes.
- All services keep public network endpoints (protected by Entra ID). Private endpoints and a web application firewall are not part of this template.
- Out of scope per the SOW: DDoS/WAF management and security or legal compliance audits.
