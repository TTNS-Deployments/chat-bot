# Support

## Raising a support ticket

1. Sign in to the DBCH portal at https://dobigcloudhub.ttns.in with the user ID and password sent to you from hello@ttns.in.
2. Open **Support** in the left menu, then **View Ticket**.
3. Click **+** to raise a new ticket and complete all mandatory fields.
4. Click **Submit** and keep the generated **Ticket ID** for reference.

Managed infrastructure support is provided 24x7 as defined in the support matrix of the Statement of Work.

## Before you raise a ticket

- **The chat page shows an error:** open `https://<web app>/healthz`. `searchSetup` should be `ready`. Application logs are in Application Insights (`<prefix>-<env>-*-appi`) and in Log Analytics (`AppServiceConsoleLogs`).
- **A document is not found in answers:** open **Documents** on the chat page and check the indexing status and errors.
- **Deployment issues:** see the troubleshooting sections in [README.md](README.md#troubleshooting) and [Documents/deployment-guide.md](Documents/deployment-guide.md#troubleshooting).

Include the environment, the approximate time (with time zone), the question asked and any error text in the ticket.
