# Data

`sample_docs/` holds fictional documents for "Northwind Appliances", used to try the chatbot:

- In local mode they are searched directly (no Azure needed).
- `Deployment/scripts/deploy.sh --seed` (or `deploy.ps1 -Seed`) uploads them to the `documents` container of a deployment as a smoke test.

They are not real product information. Replace them with your own documents for real use.
