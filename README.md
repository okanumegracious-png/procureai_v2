# ProcureAI

ProcureAI is a construction procurement workspace with a shared Python API, SQLite persistence, BOQ extraction, natural-language intake, supplier records, quotation comparison and human-controlled purchase approval.

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

Mount a durable volume at `/data`. The container stores its database at `/data/procureai.db`; the local tracked database is deliberately not copied into the image.

Required production configuration:
- `PROCUREAI_WEBHOOK_SECRET`: strong random secret used by the server-side BOQ proxy and protected workflow endpoints.
- `OPENAI_API_KEY`: required for BOQ extraction. If it is absent, extraction returns a configuration error rather than fabricated sample items.
- `PROCUREAI_DB_PATH`: optional; defaults to `/data/procureai.db` in Docker.
- `FRONTEND_URL`: comma-separated allowed browser origins; defaults to localhost for development.

Keep the API key in the Next.js server environment only. Do not expose it in browser JavaScript.

## Unified data

The dashboard, BOQ workflow tables and agentic intake use the same SQLite file through `PROCUREAI_DB_PATH`. Back up that file and its WAL state using a SQLite-aware backup process. Do not commit production database files or secrets.

## Procurement safety boundaries

- BOQ extraction stores the parsed project and line items; it does not claim values that are absent from the source.
- Supplier discovery currently searches the application's own supplier register. Only records explicitly marked `verified` are eligible for a shortlist. Adding a supplier creates an `unverified` record; independent verification is still a human task.
- RFQ sending is intentionally disabled until an approved email provider is configured.
- Quotations are stored and compared from submitted line-item totals. Confirm scope, tax, freight, validity and delivery terms before making a decision.
- Every purchase-order proposal starts as `pending_approval`. A named human decision is required before it can become approved. Recommendations never issue orders automatically.

## Current limitations before production

The existing browser dashboard has no user login or role-based access control yet. Do not expose dashboard endpoints publicly until an authenticated access layer is added. Supplier verification is a manual workflow, not a live external registry check. BOQ extraction requires a configured model provider and should be reviewed against the source document.

## Tests

Run the current intake tests with:

```bash
pytest -q
``
