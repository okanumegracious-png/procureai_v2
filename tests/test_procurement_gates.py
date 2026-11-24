import sqlite3

import pytest
from fastapi import HTTPException

import main as procureai_main
import app as dashboard_app
from app import assess_offer_feasibility as dashboard_feasibility


def _gate_db(status="active", confirmation=None):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE projects (project_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE project_purchase_requests (project_id TEXT NOT NULL, request_id INTEGER NOT NULL);
        CREATE TABLE buyer_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL,
            budget_delivery_accepted TEXT NOT NULL
        );
    """)
    db.execute("INSERT INTO projects(project_id,status) VALUES('PRJ-1',?)", (status,))
    db.execute("INSERT INTO project_purchase_requests(project_id,request_id) VALUES('PRJ-1',101)")
    if confirmation:
        db.execute("INSERT INTO buyer_confirmations(project_id,status,budget_delivery_accepted) VALUES('PRJ-1',?,?)", confirmation)
    return db


def test_analysis_and_po_gate_requires_linked_project_and_confirmed_buyer():
    db = _gate_db()
    with pytest.raises(HTTPException) as exc:
        procureai_main._require_request_buyer_confirmation(db, 101)
    assert exc.value.status_code == 409
    db.close()


def test_analysis_and_po_gate_accepts_explicit_buyer_budget_and_delivery_confirmation():
    db = _gate_db(confirmation=("confirmed", "yes"))
    assert procureai_main._require_request_buyer_confirmation(db, 101) == "PRJ-1"
    db.close()


@pytest.mark.parametrize("confirmation", [("confirmed", "no"), ("confirmed", "conditional"), ("changes_requested", "yes"), ("rejected", "yes")])
def test_analysis_and_po_gate_rejects_non_final_or_unaccepted_buyer_decisions(confirmation):
    db = _gate_db(confirmation=confirmation)
    with pytest.raises(HTTPException) as exc:
        procureai_main._require_request_buyer_confirmation(db, 101)
    assert exc.value.status_code == 409
    db.close()


def test_analysis_and_po_gate_rejects_closed_projects():
    db = _gate_db(status="terminated", confirmation=("confirmed", "yes"))
    with pytest.raises(HTTPException) as exc:
        procureai_main._require_request_buyer_confirmation(db, 101)
    assert exc.value.status_code == 409
    db.close()


def test_feasibility_requires_budget_technical_delivery_and_supplier_checks():
    base = {
        "estimated_total": 700000,
        "days_left": 14,
        "lead_days": 4,
        "supplier_rating": 4.2,
        "supplier_status": "verified",
        "technical_status": "compliant",
        "budget_context": {"budget": 850000, "available_budget": 850000},
    }
    result = dashboard_feasibility(**base)
    assert result["feasible"] is True
    assert result["award_eligible"] is True
    assert result["budget_feasibility"] == "within_budget"


def test_feasibility_blocks_over_budget_and_unreviewed_technical_offers():
    base = {
        "estimated_total": 900000,
        "days_left": 14,
        "lead_days": 4,
        "supplier_rating": 4.2,
        "supplier_status": "verified",
        "technical_status": "pending",
        "budget_context": {"budget": 850000, "available_budget": 800000},
    }
    result = dashboard_feasibility(**base)
    assert result["budget_feasibility"] == "over_budget"
    assert result["award_eligible"] is False
    assert result["technical_feasibility"] == "pending"


def test_feasibility_requires_project_budget_to_be_configured():
    result = dashboard_feasibility(
        estimated_total=1000, days_left=10, lead_days=2, supplier_rating=4.0,
        supplier_status="verified", technical_status="compliant",
        budget_context={"budget": 0, "available_budget": None},
    )
    assert result["budget_feasibility"] == "not_configured"
    assert result["feasible"] is False
    assert result["award_eligible"] is False


def test_quote_schema_migrates_technical_review_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "technical-review.db")
    dashboard_app.init_db()
    with dashboard_app.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(quotes)")}
    assert {"technical_status", "technical_notes", "technical_reviewed_by", "technical_reviewed_at"} <= columns


def test_budget_feasibility_warns_about_other_unpriced_open_requests():
    result = dashboard_feasibility(
        estimated_total=700000, days_left=14, lead_days=4, supplier_rating=4.2,
        supplier_status="verified", technical_status="compliant",
        budget_context={"budget": 850000, "available_budget": 850000, "unpriced_open_requests": 2},
    )
    assert result["budget_feasibility"] == "within_budget"
    assert any("2 other open project request" in issue for issue in result["issues"])


@pytest.mark.parametrize("status", ["completed", "terminated", "cancelled"])
def test_workflow_buyer_gate_rejects_closed_projects(status):
    db = _gate_db(status=status, confirmation=("confirmed", "yes"))
    with pytest.raises(HTTPException) as exc:
        procureai_main._require_buyer_confirmation(db, "PRJ-1")
    assert exc.value.status_code == 409
    db.close()


def test_workflow_purchase_order_gate_rechecks_supplier_verification():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE workflow_suppliers (
        supplier_id TEXT PRIMARY KEY, verification_status TEXT NOT NULL
    )""")
    db.execute("INSERT INTO workflow_suppliers VALUES('SUP-1','pending')")
    with pytest.raises(HTTPException) as exc:
        procureai_main._require_verified_workflow_supplier(db, "SUP-1")
    assert exc.value.status_code == 409
    db.execute("UPDATE workflow_suppliers SET verification_status='verified' WHERE supplier_id='SUP-1'")
    assert procureai_main._require_verified_workflow_supplier(db, "SUP-1")["verification_status"] == "verified"
    db.close()
