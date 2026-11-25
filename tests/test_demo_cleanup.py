from datetime import date

import app as dashboard_app


def test_fresh_workspace_seed_suppliers_are_explicitly_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "fresh.db")
    dashboard_app.init_db()
    suppliers = dashboard_app.rows("SELECT * FROM suppliers ORDER BY id")
    assert len(suppliers) == 7

    demo = [row for row in suppliers if "[FICTIONAL DEMO]" in row["name"]]
    sourced = [row for row in suppliers if "[FICTIONAL DEMO]" not in row["name"]]
    assert len(demo) == 1
    assert demo[0]["status"] == "Demo / Unverified"
    assert not demo[0]["email"] and not demo[0]["phone"]
    assert len(sourced) == 6
    assert all(row["status"] == "Pending verification" for row in sourced)
    assert all("NOT VERIFIED" in row["contact"] for row in sourced)
    assert dashboard_app.rows("SELECT * FROM requests") == []
    assert dashboard_app.rows("SELECT * FROM quotes") == []
    assert dashboard_app.rows("SELECT * FROM orders") == []


def test_startup_removes_only_legacy_demo_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "migration.db")
    dashboard_app.init_db()
    today = date.today().isoformat()
    with dashboard_app.connect() as db:
        demo_supplier = db.execute(
            "INSERT INTO suppliers(name,city,contact) VALUES(?,?,?)",
            ("Steelyard Ltd", "Lagos", "sales@steelyard.example"),
        ).lastrowid
        real_supplier = db.execute(
            "INSERT INTO suppliers(name,city,contact) VALUES(?,?,?)",
            ("Gestro Parts Vendor", "Lagos", "sales@gestro.example.ng"),
        ).lastrowid
        demo_request = db.execute(
            """INSERT INTO requests(code,material,category,quantity,unit,site,required_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("PR-0142", "Rebar T10 (12m)", "Reinforcement", 100, "kg", "Demo site", today, "2026-01-01"),
        ).lastrowid
        real_request = db.execute(
            """INSERT INTO requests(code,material,category,quantity,unit,site,required_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("PR-9001", "Generator oil filter", "Generator parts", 2, "ea", "Client site", today, "2026-01-01"),
        ).lastrowid
        db.execute(
            "INSERT INTO quotes(supplier_id,request_id,unit_price,created_at) VALUES(?,?,?,?)",
            (demo_supplier, demo_request, 100, "2026-01-01"),
        )
    dashboard_app.init_db()
    assert dashboard_app.one("SELECT id FROM suppliers WHERE id=?", (demo_supplier,)) is None
    assert dashboard_app.one("SELECT id FROM requests WHERE id=?", (demo_request,)) is None
    assert dashboard_app.one("SELECT id FROM suppliers WHERE id=?", (real_supplier,)) is not None
    assert dashboard_app.one("SELECT id FROM requests WHERE id=?", (real_request,)) is not None
    assert dashboard_app.rows("SELECT * FROM quotes") == []


def test_startup_preserves_operational_procurement_records_when_legacy_reset_marker_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "preserve.db")
    dashboard_app.init_db()
    today = date.today().isoformat()
    with dashboard_app.connect() as db:
        db.execute(
            "DELETE FROM procureai_migrations WHERE migration_key=?",
            ("2026-11-24-clear-pre-mvp-request-fixtures",),
        )
        supplier_id = db.execute(
            "INSERT INTO suppliers(name,city,contact,status) VALUES(?,?,?,?)",
            ("Operational Vendor Ltd", "Port Harcourt", "vendor@example.ng", "Active"),
        ).lastrowid
        request_id = db.execute(
            """INSERT INTO requests(code,material,category,quantity,unit,site,required_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            ("PR-RECOVERY-001", "Industrial pump seal", "Mechanical", 4, "ea", "Port Harcourt", today, "2026-11-20"),
        ).lastrowid
        quote_id = db.execute(
            """INSERT INTO quotes(supplier_id,request_id,unit_price,created_at)
               VALUES(?,?,?,?)""",
            (supplier_id, request_id, 125000, "2026-11-21"),
        ).lastrowid
        order_id = db.execute(
            """INSERT INTO orders(code,supplier_id,request_id,quantity,unit_price,freight,tax,total,status,required_by,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("PO-RECOVERY-001", supplier_id, request_id, 4, 125000, 0, 0, 500000, "Pending approval", today, "2026-11-22"),
        ).lastrowid

    dashboard_app.init_db()

    assert dashboard_app.one("SELECT id FROM suppliers WHERE id=?", (supplier_id,)) is not None
    assert dashboard_app.one("SELECT id FROM requests WHERE id=?", (request_id,)) is not None
    assert dashboard_app.one("SELECT id FROM quotes WHERE id=?", (quote_id,)) is not None
    assert dashboard_app.one("SELECT id FROM orders WHERE id=?", (order_id,)) is not None
