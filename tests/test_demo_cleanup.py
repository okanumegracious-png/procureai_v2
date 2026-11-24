from datetime import date, timedelta

import app as dashboard_app


def test_fresh_workspace_has_no_fictional_seed_records(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "fresh.db")
    dashboard_app.init_db()
    assert dashboard_app.rows("SELECT * FROM suppliers") == []
    assert dashboard_app.rows("SELECT * FROM requests") == []
    assert dashboard_app.rows("SELECT * FROM quotes") == []


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
