# Project Workspace / Dashboard

ProcureAI project workspaces provide a client-facing project introduction, procurement lead, delivery dates, milestone tracker, progress calculation and an attributed daily activity log. The feature uses the existing authenticated FastAPI application and the shared SQLite database configured by `PROCUREAI_DB_PATH`.

## Database schema

The startup migration creates these tables without deleting or resetting existing project, BOQ, supplier, quotation, purchase-order or activity data.

```sql
CREATE TABLE projects (
  project_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  client_name TEXT NOT NULL DEFAULT '',
  agency_details TEXT NOT NULL DEFAULT '',
  executive_summary TEXT NOT NULL DEFAULT '',
  procurement_lead TEXT NOT NULL DEFAULT '',
  deliverables_json TEXT NOT NULL DEFAULT '[]',
  start_date TEXT,
  due_date TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  milestones_json TEXT NOT NULL DEFAULT '[]',
  progress_percent REAL NOT NULL DEFAULT 0
    CHECK(progress_percent >= 0 AND progress_percent <= 100),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE project_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  author TEXT NOT NULL,
  update_type TEXT NOT NULL DEFAULT 'daily',
  status TEXT NOT NULL DEFAULT 'In progress',
  progress_percent REAL NOT NULL DEFAULT 0
    CHECK(progress_percent >= 0 AND progress_percent <= 100),
  field_notes TEXT NOT NULL DEFAULT '',
  blockers TEXT NOT NULL DEFAULT '',
  next_steps TEXT NOT NULL DEFAULT '',
  completed_milestones_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
```

Indexes support due-date sorting and reverse-chronological activity retrieval. Existing `workflow_projects` rows are backfilled into `projects` so older BOQ workspaces remain discoverable. Creating a project also creates its matching `workflow_projects` record, allowing BOQ extraction to use the same project ID. If a BOQ upload arrives first from an existing integration, the API creates a minimal project workspace automatically so the workflow is still visible in the Projects register.

## Authenticated API

All browser endpoints below use the existing session cookie and CSRF protection. The author of an update is taken from the authenticated session; it is not accepted from the request body.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/projects` | List workspaces with due status and current progress |
| POST | `/api/projects` | Create a project and linked BOQ workflow record |
| GET | `/api/projects/{project_id}/overview` | Fetch overview, dates, milestones, counts and latest 40 updates |
| POST | `/api/projects/{project_id}/updates` | Add a daily / field / blocker / milestone update |
| POST | `/api/projects/{project_id}/recalculate` | Recalculate and return current project progress |

### Create project request

```json
{
  "title": "Generator overhaul — Site A",
  "client_name": "Client / agency",
  "agency_details": "Operations department, site and engagement reference",
  "executive_summary": "Scope, objectives and expected outcome",
  "procurement_lead": "Procurement lead",
  "deliverables": ["Approved BOQ", "Evaluated supplier bids", "Approved PO"],
  "start_date": "2026-11-24",
  "due_date": "2026-12-24",
  "status": "active"
}
```

### Daily update request

```json
{
  "update_type": "daily",
  "status": "In progress",
  "progress_percent": 35,
  "field_notes": "Inspection completed and technical scope confirmed.",
  "blockers": "Awaiting final site access approval.",
  "next_steps": "Complete RFQ pack and confirm supplier list.",
  "completed_milestones": ["rfq_dispatch"]
}
```

Allowed update types are `daily`, `field_note`, `blocker` and `milestone`. Allowed milestone keys are `boq_extraction`, `rfq_dispatch`, `bid_evaluation` and `po_award`.

## Progress calculation

The overall progress percentage is deliberately transparent and reproducible:

- Procurement milestones contribute **60%** in total (15% each).
- The latest daily update's reported workstream progress contributes **40%**.
- Formula: `overall = completed_milestone_weight + (latest_daily_progress × 0.40)`.
- BOQ extraction is completed automatically when project BOQ items are persisted.
- Bid evaluation is completed when the supplier recommendation/comparison workflow runs.
- PO award is completed only after a purchase order is explicitly approved.
- RFQ dispatch is recorded by the team in a daily/milestone update. It is not inferred from quotations received because the existing supplier shortlist endpoint does not actually dispatch an RFQ email.

Due status is computed from the ISO due date: overdue, due today, due soon (within 7 days), on track, or completed. The daily progress value is the latest submitted workstream report, not an average of historical updates; this allows current progress to be corrected as work changes.

## Frontend

The live ProcureAI dashboard is currently a static HTML/CSS/JavaScript application. The Projects navigation, project register, responsive workspace, countdown, milestone view, daily activity feed and update form are integrated into that existing frontend.

A reusable Next.js client component is also provided at `components/ProjectWorkspace.tsx`, with styles at `components/project-workspace.css`. It consumes the same session-authenticated API and can be used if/when the frontend is migrated to Next.js. It is source code only; it is not part of the current static production build.
