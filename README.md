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
5. Capture structured quotations manually or parse a supplier message with Magic Extraction.
6. Record buyer confirmation of requirements, budget and delivery.
7. Run bid analysis and review commercial, delivery and supplier risks.
8. Generate a purchase-order proposal and obtain human approval.
9. Generate and track invoices from approved orders.
10. Record project progress, delivery evidence and job completion reports.
11. Complete or archive the project while preserving its records.

## Evidence-based feasibility

Each quotation records three explicit due-diligence fields in addition to its commercial terms:

- **Technical compliance:** not assessed, compliant, or deviation.
- **Stock availability:** not confirmed, available in full, partially available, or unavailable.
- **Certificates / regulatory compliance:** pending, verified, not applicable, or non-compliant.

Bid analysis also checks supplier verification, supplier rating, delivery lead time, commercial completeness and the combined known cost of the project's linked purchase requests against its approved budget. If another linked request has no quotation yet, budget fit remains provisional. Missing evidence is shown as conditional rather than being treated as a pass. Deviations, unavailable stock, non-compliant documents, over-budget estimates, late delivery and ineligible suppliers block PO review.

Existing quotations receive conservative defaults (technical not assessed, stock not confirmed, compliance pending). They are not silently marked compliant by the migration; an administrator must review and update the evidence.

## Approval controls

- Requests must belong to an active managed project before analysis or PO processing.
- Buyer confirmation must be recorded as confirmed, with explicit budget and delivery acceptance.
- Supplier verification and successful RFQ dispatch are checked before dashboard PO creation.
- Buyer confirmation and supplier verification are rechecked at approval time.
- AI recommendations never approve or award a supplier automatically.
- Completed, cancelled and terminated projects cannot progress through procurement.

## Data protection and backups

The SQLite database is stored at `PROCUREAI_DB_PATH`. In production this resolves to `/data/procureai.db` on the Railway Volume.

ProcureAI can create a consistent SQLite online backup, verify it with `PRAGMA integrity_check`, and retain the seven newest backup files. Automatic backup checks run hourly and create a backup when the latest one is at least 24 hours old. Administrators can inspect backups and request an immediate backup through:

- `GET /api/admin/database/backups`
- `POST /api/admin/database/backups`

These backups are stored on the same Railway Volume as the live database. They protect against some application-level mistakes but **are not off-site disaster recovery**. Production operators should periodically copy a verified backup to independent storage and test restoring it to a separate database before relying on it.

## Local development

```bash
python -m pip install -r requirements.txt
uvicorn main:app --reload
```

Configure the required administrator credentials and email settings in environment variables. Never commit API keys, OAuth secrets, passwords, production databases or backup files.

## Automated checks

```bash
python -m compileall -q app.py main.py procureai_store.py agentic tests
python -m pytest -q
```

## Current architecture note

The dashboard procurement register and the namespaced BOQ/workflow API currently use distinct table families inside the same SQLite database. They are not yet a single canonical supplier/quotation record set. Do not assume a supplier or quotation created in one workflow is automatically available in the other. A safe, ID-preserving consolidation migration is tracked as a separate hardening task; no existing production records should be deleted or silently merged.
