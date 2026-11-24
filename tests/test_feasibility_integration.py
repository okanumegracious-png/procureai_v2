from datetime import date, timedelta

import app as dashboard_app


def _project_context(db, budget=1000):
    db.execute("CREATE TABLE projects (project_id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    db.execute("CREATE TABLE project_purchase_requests (project_id TEXT NOT NULL, request_id INTEGER NOT NULL)")
    db.execute("CREATE TABLE project_intake (project_id TEXT PRIMARY KEY, estimated_budget REAL NOT NULL DEFAULT 0)")
    db.execute("INSERT INTO projects(project_id,status) VALUES('P-1','active')")
    db.execute("INSERT INTO project_intake(project_id,estimated_budget) VALUES('P-1',?)", (budget,))


def test_project_budget_assessment_uses_all_linked_request_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "budget.db")
    dashboard_app.init_db()
    with dashboard_app.connect() as db:
        _project_context(db, budget=1000)
        supplier_id = db.execute("INSERT INTO suppliers(name,city,status) VALUES('Verified Vendor','Port Harcourt','Verified')").lastrowid
        request_ids = []
        for index, quantity in enumerate((1, 2), start=1):
            request_id = db.execute("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (f"PR-{index}","Cable","Electrical",quantity,"roll","Site",date.today().isoformat(),"now")).lastrowid
            request_ids.append(request_id)
            db.execute("INSERT INTO project_purchase_requests(project_id,request_id) VALUES('P-1',?)", (request_id,))
        db.execute("INSERT INTO quotes(supplier_id,request_id,unit_price,freight,tax_rate,created_at) VALUES(?,?,?,?,?,?)",
                   (supplier_id,request_ids[1],100,0,0,"now"))

    assessment = dashboard_app._project_budget_assessment(request_ids[0], 100)
    assert assessment["status"] == "within_budget"
    assert assessment["known_estimate"] == 300


def test_project_budget_assessment_stays_incomplete_when_other_requests_are_unquoted(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "incomplete.db")
    dashboard_app.init_db()
    with dashboard_app.connect() as db:
        _project_context(db, budget=1000)
        ids = []
        for index in range(2):
            rid = db.execute("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (f"PR-{index}","Cable","Electrical",1,"roll","Site",date.today().isoformat(),"now")).lastrowid
            ids.append(rid)
            db.execute("INSERT INTO project_purchase_requests(project_id,request_id) VALUES('P-1',?)", (rid,))
    assessment = dashboard_app._project_budget_assessment(ids[0], 100)
    assert assessment["status"] == "incomplete"
    assert assessment["unquoted_requests"] == 1


def test_analysis_marks_fully_documented_offer_feasible(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "analysis.db")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    dashboard_app.init_db()
    with dashboard_app.connect() as db:
        _project_context(db, budget=10000)
        supplier_id = db.execute("INSERT INTO suppliers(name,city,rating,on_time,status) VALUES('Verified Vendor','Port Harcourt',4.5,95,'Verified')").lastrowid
        request_id = db.execute("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", ("PR-FEASIBLE","Cable","Electrical",10,"roll","Site",(date.today()+timedelta(days=30)).isoformat(),"now")).lastrowid
        db.execute("INSERT INTO project_purchase_requests(project_id,request_id) VALUES('P-1',?)", (request_id,))
        db.execute("""INSERT INTO quotes(supplier_id,request_id,unit_price,freight,tax_rate,lead_days,payment_days,
            technical_status,stock_status,compliance_status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (supplier_id,request_id,100,0,7.5,5,30,"compliant","available","verified","now"))

    result = dashboard_app.analyse_request(request_id)
    assert result["offers"][0]["feasibility"]["status"] == "feasible"
    assert result["offers"][0]["award_eligible"] is True


def test_existing_quotation_without_evidence_is_conditional(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "DB", tmp_path / "conditional.db")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    dashboard_app.init_db()
    with dashboard_app.connect() as db:
        _project_context(db, budget=10000)
        supplier_id = db.execute("INSERT INTO suppliers(name,city,rating,on_time,status) VALUES('Verified Vendor','Port Harcourt',4.5,95,'Verified')").lastrowid
        request_id = db.execute("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", ("PR-CONDITIONAL","Cable","Electrical",10,"roll","Site",(date.today()+timedelta(days=30)).isoformat(),"now")).lastrowid
        db.execute("INSERT INTO project_purchase_requests(project_id,request_id) VALUES('P-1',?)", (request_id,))
        db.execute("INSERT INTO quotes(supplier_id,request_id,unit_price,created_at) VALUES(?,?,?,?)", (supplier_id,request_id,100,"now"))

    result = dashboard_app.analyse_request(request_id)
    assert result["offers"][0]["feasibility"]["status"] == "conditional"
    assert result["offers"][0]["award_eligible"] is False
