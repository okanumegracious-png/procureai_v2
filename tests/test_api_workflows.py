import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

import app as dashboard_module
import main as procureai_main
import procureai_store as shared_store


@pytest.fixture
def authenticated_client(tmp_path, monkeypatch):
    database = tmp_path / "procureai-test.db"
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(database))
    monkeypatch.setenv("PROCUREAI_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("PROCUREAI_AUTO_BACKUP", "false")
    monkeypatch.setenv("PROCUREAI_ADMIN_USERNAME", "test-admin")
    salt = b"procureai-test-salt"
    rounds = 10000
    digest = hashlib.pbkdf2_hmac("sha256", b"test-password", salt, rounds)
    salt_text = base64.urlsafe_b64encode(salt).decode().rstrip("=")
    digest_text = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    monkeypatch.setenv("PROCUREAI_ADMIN_PASSWORD_HASH", "pbkdf2_sha256$" + str(rounds) + "$" + salt_text + "$" + digest_text)
    monkeypatch.setattr(dashboard_module, "DB", database)
    monkeypatch.setattr(shared_store, "DB_PATH", database)
    monkeypatch.setattr(procureai_main.intake_store, "path", str(database))

    with TestClient(procureai_main.app, base_url="https://testserver") as client:
        response = client.post("/api/auth/login", json={"username": "test-admin", "password": "test-password"})
        assert response.status_code == 200
        csrf = client.cookies.get("procureai_csrf")
        assert csrf
        client.headers.update({"X-CSRF-Token": csrf})
        yield client


def test_browser_api_requires_session_and_csrf(tmp_path, monkeypatch):
    database = tmp_path / "auth-test.db"
    monkeypatch.setenv("PROCUREAI_DB_PATH", str(database))
    monkeypatch.setenv("PROCUREAI_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("PROCUREAI_AUTO_BACKUP", "false")
    monkeypatch.setattr(dashboard_module, "DB", database)
    monkeypatch.setattr(shared_store, "DB_PATH", database)
    monkeypatch.setattr(procureai_main.intake_store, "path", str(database))
    with TestClient(procureai_main.app, base_url="https://testserver") as client:
        assert client.get("/api/dashboard").status_code == 401
        # Login with a known test-only credential, then omit the CSRF header on a write.
        salt = b"csrf-test-salt"
        rounds = 10000
        digest = hashlib.pbkdf2_hmac("sha256", b"test-password", salt, rounds)
        salt_text = base64.urlsafe_b64encode(salt).decode().rstrip("=")
        digest_text = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        monkeypatch.setenv("PROCUREAI_ADMIN_USERNAME", "test-admin")
        monkeypatch.setenv("PROCUREAI_ADMIN_PASSWORD_HASH", "pbkdf2_sha256$" + str(rounds) + "$" + salt_text + "$" + digest_text)
        assert client.post("/api/auth/login", json={"username":"test-admin","password":"test-password"}).status_code == 200
        response = client.post("/api/projects", json={"title":"CSRF test","start_date":"2026-11-25","due_date":"2026-12-25"})
        assert response.status_code == 403


def test_procurement_flow_requires_buyer_technical_supplier_and_rfq_gates(authenticated_client):
    client = authenticated_client
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    project_response = client.post("/api/projects", json={
        "title": "Integration test procurement",
        "start_date": "2026-11-25",
        "due_date": "2026-12-25",
        "project_location": "Oyigbo, Rivers State",
        "project_category": "Electrical",
        "estimated_budget": 850000,
        "expected_duration_days": 30,
        "priority": "medium",
        "planned_purchase_requests": 1,
    })
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["project"]["project_id"]

    request_response = client.post("/api/requests", json={
        "project_id": project_id,
        "material": "T10 reinforcement steel",
        "category": "Electrical equipment",
        "quantity": 100,
        "unit": "kg",
        "site": "Oyigbo, Rivers State",
        "required_by": "2026-12-20",
        "priority": "Normal",
    })
    assert request_response.status_code == 201, request_response.text
    request_id = request_response.json()["id"]

    supplier_response = client.post("/api/suppliers", json={
        "name": "Verified Test Supplier",
        "city": "Port Harcourt, Rivers State",
        "rating": 4.5,
        "on_time": 90,
        "status": "Verified",
        "email": "supplier@example.test",
        "services": "Steel supply and reinforcement materials",
    })
    assert supplier_response.status_code == 201, supplier_response.text
    supplier_id = supplier_response.json()["supplier_id"]

    quote_response = client.post("/api/quotes", json={
        "supplier_id": supplier_id,
        "request_id": request_id,
        "unit_price": 780,
        "freight": 0,
        "tax_rate": 7.5,
        "lead_days": 4,
        "payment_days": 30,
        "note": "T10 quotation for integration test",
    })
    assert quote_response.status_code == 201, quote_response.text
    quote_id = quote_response.json()["id"]

    analysis_before_buyer = client.get("/api/analysis", params={"request_id": request_id})
    assert analysis_before_buyer.status_code == 409

    confirmation = client.post(f"/api/projects/{project_id}/buyer-confirmations", json={
        "buyer_name": "Test Buyer",
        "organisation": "Test Organisation",
        "channel": "email",
        "status": "confirmed",
        "requirements_confirmed": "T10 reinforcement steel grade and dimensions confirmed.",
        "budget_delivery_accepted": "yes",
        "comments": "Integration test approval",
    })
    assert confirmation.status_code == 200, confirmation.text

    analysis_pending = client.get("/api/analysis", params={"request_id": request_id})
    assert analysis_pending.status_code == 200
    assert analysis_pending.json()["recommendation"] is None
    po_pending = client.post("/api/orders/from-analysis", json={"quote_id": quote_id})
    assert po_pending.status_code == 409
    assert "Technical compliance" in po_pending.json()["detail"]

    technical = client.post(f"/api/quotes/{quote_id}/technical-review", json={
        "status": "compliant",
        "notes": "Reviewed manufacturer datasheet, grade and 12m dimensions; matches buyer specification.",
    })
    assert technical.status_code == 200, technical.text

    analysis_compliant = client.get("/api/analysis", params={"request_id": request_id})
    assert analysis_compliant.status_code == 200
    assert analysis_compliant.json()["recommendation"]["id"] == quote_id
    assert analysis_compliant.json()["recommendation"]["budget_feasibility"] == "within_budget"

    po_without_rfq = client.post("/api/orders/from-analysis", json={"quote_id": quote_id})
    assert po_without_rfq.status_code == 409
    assert "Email the purchase request" in po_without_rfq.json()["detail"]
    legacy_po_without_rfq = client.post("/api/orders", json={
        "request_id": request_id, "supplier_id": supplier_id
    })
    assert legacy_po_without_rfq.status_code == 409
    assert "Email the purchase request" in legacy_po_without_rfq.json()["detail"]

    # Simulate a successfully dispatched RFQ record; no external email is sent in CI.
    with dashboard_module.connect() as db:
        procureai_main._ensure_supplier_request_email_table(db)
        db.execute("""INSERT INTO supplier_request_emails
            (quote_id,supplier_id,request_id,recipient,sent_by,sent_at)
            VALUES(?,?,?,?,?,?)""",
            (quote_id,supplier_id,request_id,"supplier@example.test","test-admin","2026-11-25T12:00:00"))
    po_created = client.post("/api/orders/from-analysis", json={"quote_id": quote_id})
    assert po_created.status_code == 201, po_created.text
    order_id = po_created.json()["id"]
    approval = client.post(f"/api/orders/{order_id}/decision", json={"action":"approve","note":"Integration approval"})
    assert approval.status_code == 200, approval.text

    invoice_created = client.post(f"/api/orders/{order_id}/invoice")
    assert invoice_created.status_code == 201, invoice_created.text
    invoice_id = invoice_created.json()["invoice_id"]
    invoice_detail = client.get(f"/api/invoices/{invoice_id}")
    assert invoice_detail.status_code == 200
    assert invoice_detail.json()["invoice"]["invoice_status"] == "Unpaid"
    paid = client.post(f"/api/invoices/{invoice_id}/paid")
    assert paid.status_code == 200
    invoice_list = client.get("/api/invoices")
    assert invoice_list.status_code == 200
    assert invoice_list.json()["invoices"][0]["status"] == "Paid"


def test_technical_review_requires_evidence_notes(authenticated_client):
    response = authenticated_client.post("/api/quotes/999999/technical-review", json={
        "status": "compliant", "notes": "short"
    })
    assert response.status_code == 422


def test_job_completion_project_closure_and_termination(authenticated_client):
    client = authenticated_client
    project = client.post("/api/projects", json={
        "title":"Job report and closure test",
        "start_date":"2026-11-25",
        "due_date":"2026-12-25",
        "project_location":"Oyigbo, Rivers State",
        "project_category":"Maintenance",
        "estimated_budget":250000,
        "expected_duration_days":30,
        "priority":"medium",
        "planned_purchase_requests":0,
    })
    assert project.status_code == 201, project.text
    project_id = project.json()["project"]["project_id"]

    report = client.post(f"/api/projects/{project_id}/job-completion-report", json={
        "status":"Submitted",
        "job":"Workshop distribution panel repair",
        "job_date":"2026-11-25",
        "location":"Oyigbo workshop",
        "equipment":"Main distribution panel",
        "purpose":"Restore safe and reliable power distribution.",
        "initial_condition":"Panel had a damaged breaker and loose termination.",
        "work_carried_out":"Replaced breaker, tightened terminations and tested circuits.",
        "final_condition":"Panel operational after inspection and testing.",
        "technician":"Test Technician",
        "supervisor":"Test Supervisor",
        "before_photo":{"name":"before.png","mime_type":"image/png","data_url":"data:image/png;base64,iVBORw0KGgo="},
        "after_photo":{"name":"after.png","mime_type":"image/png","data_url":"data:image/png;base64,iVBORw0KGgo="},
    })
    assert report.status_code == 200, report.text
    assert report.json()["report"]["status"] == "Submitted"
    saved_report = client.get(f"/api/projects/{project_id}/job-completion-report")
    assert saved_report.status_code == 200
    assert saved_report.json()["report"]["before_photo"] is not None
    assert saved_report.json()["report"]["after_photo"] is not None

    completed = client.post(f"/api/projects/{project_id}/complete", json={
        "completion_note":"Work completed and documented."
    })
    assert completed.status_code == 200, completed.text
    project_detail = client.get(f"/api/projects/{project_id}")
    assert project_detail.status_code == 200
    assert project_detail.json()["project"]["status"] == "completed"

    second = client.post("/api/projects", json={
        "title":"Project termination test",
        "start_date":"2026-11-25",
        "due_date":"2026-12-25",
        "project_location":"Oyigbo, Rivers State",
        "project_category":"Maintenance",
        "estimated_budget":100000,
        "expected_duration_days":30,
        "priority":"low",
        "planned_purchase_requests":0,
    })
    assert second.status_code == 201, second.text
    terminated_id = second.json()["project"]["project_id"]
    terminated = client.post(f"/api/projects/{terminated_id}/terminate", json={
        "termination_note":"Client cancelled the project before mobilisation."
    })
    assert terminated.status_code == 200, terminated.text
    terminated_detail = client.get(f"/api/projects/{terminated_id}")
    assert terminated_detail.status_code == 200
    assert terminated_detail.json()["project"]["status"] == "terminated"
    report_after_termination = client.post(f"/api/projects/{terminated_id}/job-completion-report", json={
        "status":"Draft","job":"Should be blocked"
    })
    assert report_after_termination.status_code == 409
