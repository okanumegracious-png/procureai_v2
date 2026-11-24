"""Shared durable SQLite storage for ProcureAI services.

All application services use PROCUREAI_DB_PATH so dashboard, BOQ and intake
records can live in one database. Never store secrets in this database.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("PROCUREAI_DB_PATH", str(Path(__file__).resolve().parent / "procureai.db")))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_store() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_projects (
            project_id TEXT PRIMARY KEY,
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'created',
            source_filename TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS projects (
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
            progress_percent REAL NOT NULL DEFAULT 0 CHECK(progress_percent >= 0 AND progress_percent <= 100),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_due_date ON projects(due_date);
        CREATE TABLE IF NOT EXISTS project_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            author TEXT NOT NULL,
            update_type TEXT NOT NULL DEFAULT 'daily',
            status TEXT NOT NULL DEFAULT 'In progress',
            progress_percent REAL NOT NULL DEFAULT 0 CHECK(progress_percent >= 0 AND progress_percent <= 100),
            field_notes TEXT NOT NULL DEFAULT '',
            blockers TEXT NOT NULL DEFAULT '',
            next_steps TEXT NOT NULL DEFAULT '',
            completed_milestones_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_project_updates_project_created
            ON project_updates(project_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS workflow_boq_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES workflow_projects(project_id) ON DELETE CASCADE,
            item_code TEXT NOT NULL DEFAULT '',
            item_name TEXT NOT NULL,
            specification TEXT NOT NULL DEFAULT '',
            quantity REAL NOT NULL CHECK(quantity >= 0),
            unit TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'General',
            source_page INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_boq_project
            ON workflow_boq_items(project_id);
        CREATE TABLE IF NOT EXISTS workflow_suppliers (
            supplier_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            website TEXT,
            address TEXT,
            categories_json TEXT NOT NULL DEFAULT '[]',
            verification_status TEXT NOT NULL DEFAULT 'unverified'
                CHECK(verification_status IN ('unverified','pending','verified','rejected')),
            verification_notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workflow_quotations (
            quotation_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES workflow_projects(project_id),
            supplier_id TEXT NOT NULL REFERENCES workflow_suppliers(supplier_id),
            currency TEXT NOT NULL DEFAULT 'NGN',
            delivery_days INTEGER NOT NULL CHECK(delivery_days >= 0),
            line_items_json TEXT NOT NULL,
            total_amount REAL NOT NULL CHECK(total_amount >= 0),
            status TEXT NOT NULL DEFAULT 'submitted',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_quotes_project
            ON workflow_quotations(project_id);
        CREATE TABLE IF NOT EXISTS workflow_purchase_orders (
            po_number TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES workflow_projects(project_id),
            quotation_id TEXT NOT NULL UNIQUE REFERENCES workflow_quotations(quotation_id),
            supplier_id TEXT NOT NULL REFERENCES workflow_suppliers(supplier_id),
            total_amount REAL NOT NULL,
            currency TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_approval'
                CHECK(status IN ('pending_approval','approved','rejected')),
            requested_by TEXT NOT NULL DEFAULT '',
            decided_by TEXT,
            decision_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            decided_at TEXT
        );
        CREATE TABLE IF NOT EXISTS company_profile (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            company_name TEXT NOT NULL DEFAULT '',
            registration_number TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            services_json TEXT NOT NULL DEFAULT '[]',
            service_modes_json TEXT NOT NULL DEFAULT '[]',
            client_segments_json TEXT NOT NULL DEFAULT '[]',
            currency TEXT NOT NULL DEFAULT 'NGN',
            updated_at TEXT NOT NULL
        );
        """)
        # Backfill the workspace view for BOQ projects created before the project dashboard existed.
        db.execute("""INSERT OR IGNORE INTO projects
            (project_id,title,executive_summary,status,created_at,updated_at)
            SELECT project_id,project_id,summary,status,created_at,updated_at
            FROM workflow_projects""")
