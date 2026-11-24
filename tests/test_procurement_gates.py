import sqlite3

import pytest
from fastapi import HTTPException

import main as procureai_main


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
