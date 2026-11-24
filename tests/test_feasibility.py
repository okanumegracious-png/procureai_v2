import app as dashboard_app


def test_offer_is_feasible_only_when_all_required_evidence_is_confirmed():
    result = dashboard_app.assess_offer_feasibility(
        days_left=14, lead_days=7, rating=4.2, supplier_status="verified",
        technical_status="compliant", stock_status="available",
        compliance_status="verified", budget_status="within_budget",
    )
    assert result["status"] == "feasible"
    assert result["blockers"] == []
    assert result["unknowns"] == []


def test_missing_technical_stock_or_budget_evidence_remains_conditional():
    result = dashboard_app.assess_offer_feasibility(
        days_left=14, lead_days=7, rating=4.0, supplier_status="verified",
        technical_status="not_assessed", stock_status="not_confirmed",
        compliance_status="pending", budget_status="incomplete",
    )
    assert result["status"] == "conditional"
    assert result["unknowns"]
    assert result["blockers"] == []


def test_over_budget_or_late_delivery_is_infeasible():
    result = dashboard_app.assess_offer_feasibility(
        days_left=3, lead_days=8, rating=4.5, supplier_status="verified",
        technical_status="compliant", stock_status="available",
        compliance_status="verified", budget_status="over_budget",
    )
    assert result["status"] == "infeasible"
    assert any("delivery" in item.lower() for item in result["blockers"])
    assert any("budget" in item.lower() for item in result["blockers"])


def test_unverified_supplier_and_noncompliant_offer_cannot_pass():
    result = dashboard_app.assess_offer_feasibility(
        days_left=14, lead_days=7, rating=4.5, supplier_status="pending verification",
        technical_status="deviation", stock_status="unavailable",
        compliance_status="non_compliant", budget_status="within_budget",
    )
    assert result["status"] == "infeasible"
    assert len(result["blockers"]) >= 4


def test_existing_quote_schema_gets_safe_default_evidence_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "feasibility.db")
    dashboard_app.init_db()
    with dashboard_app.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(quotes)").fetchall()}
        assert {"technical_status", "stock_status", "compliance_status"}.issubset(columns)
