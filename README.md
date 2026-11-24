# ProcureAI — Procurement & Project Operations Platform

**Production:** https://procureaiv2-production.up.railway.app/

ProcureAI manages procurement from project intake and purchase requests through supplier sourcing, quotation capture, bid evaluation, purchase-order approval, invoicing, delivery tracking and job completion reporting.

## Operating principles

**AI assists. Calculations are traceable. People approve.**

ProcureAI does not treat an AI recommendation or an emailed request for quotation as an award or purchase commitment. Purchase orders require internal human approval and the applicable buyer and supplier checks.

## Technology

- **Backend:** Python 3.11, FastAPI and Uvicorn.
- **Frontend:** HTML, CSS and vanilla JavaScript.
- **Database:** SQLite with WAL mode and a shared `PROCUREAI_DB_PATH`.
- **Production storage:** Railway Volume mounted at `/data`; the Docker image configures `/data/procureai.db`.
- **Email:** Gmail API OAuth, with optional HTTPS provider support.
- **AI:** Optional OpenAI integration; deterministic rules remain available when no API key is configured.
- **Hosting and source control:** Railway and GitHub.

## Main procurement workflow

1. Create a project with location, budget, category, priority and duration.
2. Add one or more purchase requests to the project.
3. Source suppliers and complete supplier verification.
4. Send a formal RFQ and record supplier quotations.
5. Capture structured quotations manually or parse a supplier message with Magic Extraction. Multi-line supplier messages can be split into independently priced line items for administrator selection.
6. Record buyer confirmation of requirements, budget and delivery.
7. Run bid analysis and review commercial, delivery and supplier risks.
8. Generate a purchase-order proposal and obtain human approval.
9. Generate and track invoices from approved orders.
10. Record project progress, delivery evidence and job completion reports.
11. Complete or archive the project while preserving its records.

## Approval controls

- Requests must belong to an active managed project before analysis or PO processing.
- Buyer confirmation must be recorded as confirmed, with explicit budget and delivery acceptance.
- Supplier verification and successful RFQ dispatch are checked before dashboard PO creation.
- Buyer confirmation and supplier verification are rechecked at approval time.
- Each quotation starts with technical compliance set to pending; an administrator must record evidence and mark it compliant before a PO proposal or approval.
- Bid analysis separates project budget, existing approved/pending commitments, delivery, supplier verification and technical compliance. A missing project budget is explicitly reported rather than treated as a pass.
- AI recommendations never approve or award a supplier automatically.
- Completed, cancelled and terminated projects cannot progress through procurement.

## Data protection and backups

The SQLite database is stored at `PROCUREAI_DB_PATH`. In production this resolves to `/data/procureai.db` on the Railway Volume.

ProcureAI can create a consistent SQLite online backup, verify it with `PRAGMA integrity_check`, and retain the seven newest backup files. Automatic backup checks run hourly and create a backup when the latest one is at least 24 hours old. Administrators can inspect backups and request an immediate backup through:

- `GET /api/admin/database/backups`
- `POST /api/admin/database/backups`

These backups are stored on the same Railway Volume as the live database. The Company setup page lets an administrator download a backup for independent storage and run an isolated restore verification. The verification copies the selected backup into a temporary database, checks SQLite integrity and foreign keys, and never overwrites the live database. Download a verified copy to independent storage regularly: same-volume copies are not off-site disaster recovery. A full operational restore into a replacement production service still requires a controlled maintenance procedure.

## Local development

```bash
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

Configure the required administrator credentials and email settings in environment variables. Never commit API keys, OAuth secrets, passwords, production databases or backup files.

## Technical compliance review

Dashboard quotations can be reviewed through `POST /api/quotes/{quote_id}/technical-review` with `status` set to `compliant`, `deviation`, `non_compliant` or `pending`, plus evidence notes. Workflow API quotations use `POST /api/v1/workflows/quotations/{quotation_id}/technical-review` with the API key. Marking a quote compliant requires written notes; editing its commercial quotation resets the review to pending. The purchase-order creation and approval paths recheck the review status.

## Automated checks

```bash
python -m compileall -q app.py main.py procureai_store.py agentic tests
python -m pytest -q
```

## Current architecture note

The dashboard procurement register and the namespaced BOQ/workflow API currently use distinct table families inside the same SQLite database. They are not yet a single canonical supplier/quotation record set. Do not assume a supplier or quotation created in one workflow is automatically available in the other. A safe, ID-preserving consolidation migration is tracked as a separate hardening task; no existing production records should be deleted or silently merged.


## Continuous integration

GitHub Actions runs Python compilation and the regression suite on pull requests and pushes to `main`. The suite covers database backup integrity/restore checks, procurement approval gates, technical-review migrations, multi-line supplier quotation parsing, authenticated API/CSRF behaviour, and the main buyer → quotation → technical review → RFQ dispatch gate sequence. Integration tests use temporary SQLite databases and do not connect to production data.
