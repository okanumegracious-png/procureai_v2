# ProcureAI

ProcureAI is an industrial operations and procurement workspace. Its shared Python API supports administrator-entered company configuration, procurement records, BOQ extraction, natural-language intake, supplier records, quotation comparison and human-controlled purchase approval.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PROCUREAI_DB_PATH=./data/procureai.db
export PROCUREAI_WEBHOOK_SECRET="use-a-long-random-secret"
export OPENAI_API_KEY="your-provider-key"
uvicorn main:app --host 127.0.0.1 --port 8000
``

Open http://127.0.0.1:8000. On Windows PowerShell, use `$env:NAME="value"` to set environment variables.

## Docker

Mount a durable volume at `/data`. The container stores its database at `/data/procureai.db`. Database files are runtime data and are not tracked in Git.

Required production configuration:
- `PROCUREAI_ADMIN_USERNAME`: the single administrator login name.
- `PROCUREAI_ADMIN_PASSWORD_HASH`: PBKDF2-SHA256 password hash for the administrator; never store the plaintext password in source control.
- `PROCUREAI_WEBHOOK_SECRET`: strong random secret used by protected BOQ and supplier workflow endpoints.
- `OPENAI_API_KEY`: required for BOQ extraction. If it is absent, extraction returns a configuration error rather than fabricated sample items.
- `PROCUREAI_DB_PATH`: optional; defaults to `/data/procureai.db` in Docker.
- `FRONTEND_URL`: comma-separated allowed browser origins; defaults to localhost for development.

Keep all API secrets in Railway server environment variables only. Never expose them in browser JavaScript.

## Company setup\n\nAfter signing in, open **Company setup** to enter the organisation name, registration number, industry, services, delivery modes, client segments and default currency. These values are stored in the shared application database; the workspace does not assume a particular customer or pre-populate fictional company information.\n\nThe application does not seed fictional suppliers, requests or quotations. On startup it removes only the exact legacy sample suppliers and purchase requests shipped by the earlier demo dataset, including their linked quotations and orders. Other records are retained.\n\n## Unified data

The dashboard, BOQ workflow tables and agentic intake use the same SQLite file through `PROCUREAI_DB_PATH`. Back up that file and its WAL state using a SQLite-aware backup process. Do not commit production database files or secrets.

## Procurement safety boundaries

- BOQ extraction stores the parsed project and line items; it does not claim values that are absent from the source.
- Supplier discovery currently searches the application's own supplier register. Only records explicitly marked `verified` are eligible for a shortlist. Adding a supplier creates an `unverified` record; independent verification is still a human task.
- RFQ sending is intentionally disabled until an approved email provider is configured.
- Quotations are stored and compared from submitted line-item totals. Confirm scope, tax, freight, validity and delivery terms before making a decision.
- Every purchase-order proposal starts as `pending_approval`. A named human decision is required before it can become approved. Recommendations never issue orders automatically.

## Current limitations before production

The browser dashboard uses a single-administrator login. Dashboard, intake, quotation and purchase-order browser APIs require a server-side session cookie and CSRF token. Keep the admin password hash and webhook key in Railway variables only. Rotate the initial password after first access. Supplier verification is a manual workflow, not a live external registry check. BOQ extraction requires a configured model provider and should be reviewed against the source document.

## Tests

Run the current intake tests with:

```bash
pytest -q
``

### Supplier request email (SMTP)

To enable the mandatory seller-email step before generating a PO proposal, configure these Railway service variables:

- `SMTP_HOST`: SMTP server hostname
- `SMTP_PORT`: usually `587` for STARTTLS or `465` for SSL
- `SMTP_USERNAME`: SMTP login username
- `SMTP_PASSWORD`: SMTP credential or app password
- `SMTP_FROM_EMAIL`: approved sender address
- `SMTP_USE_TLS`: `true` for STARTTLS (default)
- `SMTP_USE_SSL`: `true` only for implicit SSL on port 465

A real verified seller must have an email address saved. The application records a successful send and blocks PO proposal generation until the request email has been sent. Configure the SMTP variables before using this feature in production.

PDF exports open the browser print dialog; choose **Save as PDF** to save the currently filtered register.