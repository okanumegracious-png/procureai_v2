#!/usr/bin/env python3
"""ProcureAI browser application — standard-library edition.

Run: python app.py
Open: http://127.0.0.1:8000
Data is stored in procureai.db beside this file.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
DB = Path(os.getenv("PROCUREAI_DB_PATH", str(BASE / "procureai.db")))
STATIC = BASE / "static"
D = Decimal


def money(value):
    return str(D(str(value)).quantize(D("0.01"), rounding=ROUND_HALF_UP))


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS suppliers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, city TEXT NOT NULL,
          rating REAL NOT NULL DEFAULT 0, on_time REAL NOT NULL DEFAULT 0,
          contact TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'Active'
        );
        CREATE TABLE IF NOT EXISTS requests(
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
          material TEXT NOT NULL, category TEXT NOT NULL, quantity REAL NOT NULL,
          unit TEXT NOT NULL, site TEXT NOT NULL, required_by TEXT NOT NULL,
          priority TEXT NOT NULL DEFAULT 'Normal', status TEXT NOT NULL DEFAULT 'Open',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quotes(
          id INTEGER PRIMARY KEY AUTOINCREMENT, supplier_id INTEGER NOT NULL,
          request_id INTEGER NOT NULL, unit_price REAL NOT NULL, freight REAL NOT NULL DEFAULT 0,
          tax_rate REAL NOT NULL DEFAULT 7.5, lead_days INTEGER NOT NULL DEFAULT 7,
          payment_days INTEGER NOT NULL DEFAULT 30, note TEXT DEFAULT '',
          created_at TEXT NOT NULL,
          FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
          FOREIGN KEY(request_id) REFERENCES requests(id)
        );
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
          supplier_id INTEGER NOT NULL, request_id INTEGER NOT NULL,
          quantity REAL NOT NULL, unit_price REAL NOT NULL, freight REAL NOT NULL,
          tax REAL NOT NULL, total REAL NOT NULL, status TEXT NOT NULL,
          required_by TEXT NOT NULL, created_at TEXT NOT NULL, decision_note TEXT DEFAULT '',
          FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
          FOREIGN KEY(request_id) REFERENCES requests(id)
        );
        """)
        # Add explicit technical, stock and compliance evidence to existing quotations.
        quote_columns = {row["name"] for row in db.execute("PRAGMA table_info(quotes)").fetchall()}
        for column, definition in {
            "technical_status": "TEXT NOT NULL DEFAULT 'not_assessed'",
            "stock_status": "TEXT NOT NULL DEFAULT 'not_confirmed'",
            "compliance_status": "TEXT NOT NULL DEFAULT 'pending'"
        }.items():
            if column not in quote_columns:
                db.execute(f"ALTER TABLE quotes ADD COLUMN {column} {definition}")

        # Link generated purchase-order proposals to the exact quotation used in analysis.
        order_columns = {row["name"] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
        for column, definition in {
            "quote_id": "INTEGER", "analysis_estimate": "REAL DEFAULT 0",
            "lead_days": "INTEGER DEFAULT 0", "payment_days": "INTEGER DEFAULT 0"
        }.items():
            if column not in order_columns:
                db.execute(f"ALTER TABLE orders ADD COLUMN {column} {definition}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_quote_id_unique ON orders(quote_id) WHERE quote_id IS NOT NULL")

        # Add structured seller fields while preserving every existing supplier row.
        supplier_columns = {row["name"] for row in db.execute("PRAGMA table_info(suppliers)").fetchall()}
        for column, definition in {
            "email": "TEXT DEFAULT ''", "phone": "TEXT DEFAULT ''",
            "website": "TEXT DEFAULT ''", "address": "TEXT DEFAULT ''",
            "services": "TEXT DEFAULT ''"
        }.items():
            if column not in supplier_columns:
                db.execute(f"ALTER TABLE suppliers ADD COLUMN {column} {definition}")

        # One-time reset of pre-MVP manually entered test procurement records.
        # The migration marker prevents future startup from deleting live requests.
        db.execute("CREATE TABLE IF NOT EXISTS procureai_migrations (migration_key TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        reset_key = "2026-11-24-clear-pre-mvp-request-fixtures"
        if not db.execute("SELECT 1 FROM procureai_migrations WHERE migration_key=?", (reset_key,)).fetchone():
            db.execute("DELETE FROM orders")
            db.execute("DELETE FROM quotes")
            db.execute("DELETE FROM requests")
            db.execute("INSERT INTO procureai_migrations(migration_key,applied_at) VALUES(?,?)", (reset_key, datetime.utcnow().isoformat()))

        # Explicitly requested fictional supplier for the generator-servicing demo section.
        # Clearly labelled as fictional and unverified; never present as a real vendor.
        supplier_demo_key = "2026-11-24-fictional-generator-service-supplier"
        if not db.execute("SELECT 1 FROM procureai_migrations WHERE migration_key=?", (supplier_demo_key,)).fetchone():
            db.execute("""INSERT INTO suppliers(name,city,rating,on_time,contact,status)
                SELECT ?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM suppliers WHERE name=?)""",
                ("Northstar Generator & Power Services [FICTIONAL DEMO]", "Lagos, Nigeria", 0, 0,
                 "DEMO ONLY — Generator servicing, fault diagnosis, preventive maintenance, repairs and load testing. No real contact details. NOT VERIFIED.",
                 "Demo / Unverified", "Northstar Generator & Power Services [FICTIONAL DEMO]"))
            db.execute("INSERT INTO procureai_migrations(migration_key,applied_at) VALUES(?,?)",
                       (supplier_demo_key, datetime.utcnow().isoformat()))

        # Sourced suppliers for the Workshop Electrical Upgrade test project.
        # Public listings are leads only: every record starts Pending verification.
        supplier_sourcing_key = "2026-11-24-workshop-electrical-suppliers"
        if not db.execute("SELECT 1 FROM procureai_migrations WHERE migration_key=?", (supplier_sourcing_key,)).fetchone():
            sourced_suppliers = [
                {
                    "name": "AENS Projects and Engineering Services Limited",
                    "city": "Port Harcourt, Rivers State",
                    "contact": "Listed in Schneider Electric Nigeria's official panel-builder directory. Public sourcing references: https://www.se.com/ng/en/work/support/where-to-buy/list-of-distributors/ ; https://www.aens-engineering.com . NOT VERIFIED — confirm CAC, current panel-building capacity, OEM authorisation, references and bank ownership before award.",
                    "email": "aensengineering@gmail.com; projects@aens-engineering.com",
                    "phone": "+234 703 253 8441; +234 809 798 3801",
                    "website": "https://www.aens-engineering.com",
                    "address": "45 Aba Road, behind CFC Bus Stop, Mile 1, Port Harcourt, Rivers State, Nigeria",
                    "services": "Electrical distribution panel building; switchboards and power distribution; electrical installation, commissioning and maintenance; automation and control systems. Relevant to the distribution panel and installation/testing scope."
                },
                {
                    "name": "Benotek Engineering Limited",
                    "city": "Woji, Port Harcourt, Rivers State",
                    "contact": "Listed in Schneider Electric Nigeria's official retail-distributor directory. Public sourcing reference: https://www.se.com/ng/en/work/support/where-to-buy/list-of-distributors/ ; https://www.benotek.com . NOT VERIFIED — confirm current distributor status, product availability, CAC, bank ownership and project references before award.",
                    "email": "operations@benotek.com; sales@benotek.com",
                    "phone": "+234 905 046 2888; +234 705 875 1123",
                    "website": "https://www.benotek.com",
                    "address": "Triple Oak Plaza, Peter Odili Extension, Woji-Akpajo Link Road, Gbalajam, Woji, Port Harcourt, Rivers State, Nigeria",
                    "services": "Electrical and automation products distribution; electrical components and project supply. Listed retail distributor for Schneider Electric products; confirm exact panel, breaker and cable brands/ratings against the approved specification."
                },
                {
                    "name": "Detopsy Electrical Shop (Detopsy Engineering Limited)",
                    "city": "Trans Amadi, Port Harcourt, Rivers State",
                    "contact": "Supplier details and product range published on https://detopsyelectricalshop.com/about-us/ . NOT VERIFIED — confirm legal entity/CAC, stock, manufacturer sourcing, product certificates and bank ownership before award.",
                    "email": "",
                    "phone": "+234 701 347 1247",
                    "website": "https://detopsyelectricalshop.com",
                    "address": "Plot 40–42 Azuabie Street, off Peter Odili Road, Trans Amadi, Port Harcourt, Rivers State, Nigeria",
                    "services": "Electrical distribution and protection products including Schneider distribution boards, ABB breakers and Philips LED lighting; electrical accessories and installation support. Relevant to the distribution panel, protection devices and lighting."
                },
                {
                    "name": "Trucast Nigeria Limited",
                    "city": "Rumuomasi, Port Harcourt, Rivers State",
                    "contact": "Company website publishes RC 8183115 and wholesale/project enquiries. Public sourcing reference: https://www.trucast-ng.com/ . No public phone number captured. NOT VERIFIED — confirm CAC record, contact person, product certifications, stock and warranty before award.",
                    "email": "sales@trucast-ng.com",
                    "phone": "",
                    "website": "https://www.trucast-ng.com",
                    "address": "Cluster 2, Air Force Plaza, Rumuomasi Market Junction, Port Harcourt, Rivers State, Nigeria",
                    "services": "Electrical accessories and LED lighting distribution; wholesale and project supply with nationwide delivery. Ask for industrial high-bay LED datasheets, lumen output, IP/IK rating, warranty and SON compliance before considering."
                },
                {
                    "name": "Toscon Global Views Limited",
                    "city": "Mgbuesilaru, Port Harcourt, Rivers State",
                    "contact": "Public company website page: https://www.tosconglobalviews.com/electrical-spare-parts/ (page carries an August 2016 publication date; reconfirm current operations and contacts). NOT VERIFIED — confirm cable manufacturer/brand, current stock, standards documentation and bank ownership.",
                    "email": "info@tosconglobalviews.com",
                    "phone": "+234 803 547 1989; +234 803 591 5642",
                    "website": "https://www.tosconglobalviews.com",
                    "address": "48 RD Road, Mgbuesilaru, behind Shell R.A., Port Harcourt, Rivers State, Nigeria",
                    "services": "Electrical cables and electrical installation materials; specialist project supply. Request conductor size, copper/aluminium composition, insulation type, voltage grade, IEC/NIS compliance and test certificates."
                },
                {
                    "name": "Jesodeg Integrated Services Limited",
                    "city": "Woji, Port Harcourt, Rivers State",
                    "contact": "Company-published electrical services and contact details: https://jesodeg.com/electrical.php . NOT VERIFIED — confirm CAC, qualified personnel, professional credentials, HSE documentation, insurance and project references before engagement.",
                    "email": "support@jesodeg.com",
                    "phone": "+234 913 393 6575",
                    "website": "https://jesodeg.com",
                    "address": "No. 10 Ezegbakagbaka, Woji, Port Harcourt, Rivers State, Nigeria",
                    "services": "Electrical installation and commissioning; distribution boards and power panels; industrial lighting; cable tray construction; cable pulling and termination; grounding and electrical system upgrades. Relevant to installation and testing services."
                }
            ]
            for supplier in sourced_suppliers:
                db.execute("""INSERT INTO suppliers
                    (name,city,rating,on_time,contact,status,email,phone,website,address,services)
                    SELECT ?,?,0,0,?,'Pending verification',?,?,?,?,?
                    WHERE NOT EXISTS (SELECT 1 FROM suppliers WHERE lower(name)=lower(?))""",
                    (supplier["name"], supplier["city"], supplier["contact"], supplier["email"],
                     supplier["phone"], supplier["website"], supplier["address"], supplier["services"],
                     supplier["name"]))
            db.execute("INSERT INTO procureai_migrations(migration_key,applied_at) VALUES(?,?)",
                       (supplier_sourcing_key, datetime.utcnow().isoformat()))

        # Never seed fictional suppliers, requests or quotations into an operational workspace.
        # Remove only the exact legacy demo rows created by earlier ProcureAI versions.
        demo_supplier_names = (
            "Steelyard Ltd", "Metrode Supplies", "BuildMart Distribution",
            "NaijaSteel Traders", "CrestCon Materials",
        )
        demo_request_materials = (
            "Rebar T10 (12m)", "Rebar T16 (12m)", "OPC 42.5 Cement",
            "Granite Aggregate 20mm", "Formwork Plywood 18mm",
        )
        supplier_marks = ",".join("?" for _ in demo_supplier_names)
        request_marks = ",".join("?" for _ in demo_request_materials)
        demo_supplier_ids = [
            row[0] for row in db.execute(
                f"SELECT id FROM suppliers WHERE name IN ({supplier_marks}) AND lower(contact) LIKE '%@%.example'",
                demo_supplier_names,
            ).fetchall()
        ]
        demo_request_ids = [
            row[0] for row in db.execute(
                f"SELECT id FROM requests WHERE code IN ('PR-0142','PR-0143','PR-0144','PR-0145','PR-0146') AND material IN ({request_marks})",
                demo_request_materials,
            ).fetchall()
        ]
        if demo_supplier_ids or demo_request_ids:
            supplier_ids = demo_supplier_ids or [-1]
            request_ids = demo_request_ids or [-1]
            supplier_in = ",".join("?" for _ in supplier_ids)
            request_in = ",".join("?" for _ in request_ids)
            db.execute(
                f"DELETE FROM orders WHERE supplier_id IN ({supplier_in}) OR request_id IN ({request_in})",
                [*supplier_ids, *request_ids],
            )
            db.execute(
                f"DELETE FROM quotes WHERE supplier_id IN ({supplier_in}) OR request_id IN ({request_in})",
                [*supplier_ids, *request_ids],
            )
            if demo_request_ids:
                db.execute(f"DELETE FROM requests WHERE id IN ({request_in})", request_ids)
            if demo_supplier_ids:
                db.execute(f"DELETE FROM suppliers WHERE id IN ({supplier_in})", supplier_ids)



def rows(sql, args=()):
    with connect() as db:
        return [dict(r) for r in db.execute(sql, args).fetchall()]


def one(sql, args=()):
    with connect() as db:
        r = db.execute(sql, args).fetchone()
        return dict(r) if r else None


def dashboard():
    req = rows("SELECT * FROM requests ORDER BY id DESC")
    suppliers = rows("SELECT * FROM suppliers ORDER BY rating DESC")
    recent_quotes = rows("""SELECT q.*,s.name supplier,s.contact,r.code request_code,r.material
            FROM quotes q JOIN suppliers s ON s.id=q.supplier_id
            JOIN requests r ON r.id=q.request_id ORDER BY q.id DESC LIMIT 8""")
    orders = rows("""SELECT o.*,s.name supplier,r.material FROM orders o
                     JOIN suppliers s ON s.id=o.supplier_id JOIN requests r ON r.id=o.request_id
                     ORDER BY o.id DESC""")
    open_requests = sum(1 for r in req if r["status"] == "Open")
    pending = sum(1 for o in orders if o["status"] == "Pending approval")
    total_spend = sum(float(o["total"]) for o in orders if o["status"] == "Approved")
    return {
        "metrics": {"open_requests": open_requests, "suppliers": len(suppliers),
                    "pending_approvals": pending, "approved_spend": round(total_spend,2)},
        "requests": req, "suppliers": suppliers, "orders": orders,
        "recent_quotes": recent_quotes
    }


def _bid_ai_judgement(request, offers, alternatives, days_left):
    """Use OpenAI when configured; always retain a deterministic fallback."""
    quoted = [{
        "quote_id": int(o["id"]), "seller": o["supplier"],
        "unit_price": float(o["unit_price"]), "freight": float(o["freight"]),
        "tax_rate": float(o["tax_rate"]), "estimated_total": float(o["estimated_total"]),
        "lead_days": int(o["lead_days"]), "payment_days": int(o["payment_days"]),
        "rating": float(o["rating"] or 0), "on_time_percent": float(o["on_time"] or 0),
        "seller_status": o["supplier_status"], "feasible": bool(o["feasible"]),
        "issues": o["issues"], "quotation_note": o["note"] or ""
    } for o in offers]
    fallback_reviews = []
    for o in quoted:
        strengths, gaps, improvements = [], [], []
        if o["unit_price"] > 0:
            strengths.append("A priced quotation is available for comparison.")
        if o["lead_days"] <= days_left:
            strengths.append("Quoted lead time fits the remaining delivery window.")
        else:
            gaps.append(f"Lead time is {o['lead_days'] - days_left} day(s) beyond the remaining delivery window.")
            improvements.append("Submit a realistic accelerated delivery plan or phased delivery schedule.")
        if o["rating"] >= 3.5:
            strengths.append("Seller rating meets the preferred 3.5/5 threshold.")
        elif o["rating"] < 2.5:
            gaps.append("Seller rating is below the minimum analysis threshold.")
            improvements.append("Provide recent client references, quality evidence and a corrective-action plan.")
        else:
            gaps.append("Seller rating is below the preferred 3.5/5 threshold.")
            improvements.append("Provide recent performance references and warranty/support commitments.")
        if o["on_time_percent"] < 80:
            gaps.append("Recorded on-time performance is below 80%.")
            improvements.append("Confirm stock availability, delivery milestones and remedies for late delivery.")
        if str(o["seller_status"] or "").lower() not in ("verified", "active"):
            gaps.append("Seller is not currently marked Active or Verified in the supplier register.")
            improvements.append("Complete identity, capability, references and bank-account verification before award.")
        if o["freight"] < 0:
            gaps.append("Freight value needs correction.")
            improvements.append("Resubmit a complete landed-cost breakdown.")
        fallback_reviews.append({
            "quote_id": o["quote_id"], "seller": o["seller"],
            "strengths": strengths or ["Quotation captured for review."],
            "gaps": gaps or ["No material issue identified by the current rule checks; confirm specifications and quote validity."],
            "improvements": improvements or ["Confirm technical compliance, stock, warranty, quote validity and delivery commitments."],
            "risk_level": "high" if (not o["feasible"] or str(o["seller_status"] or "").lower() not in ("verified", "active")) else ("medium" if gaps else "low"),
            "next_step": "Request a revised quotation / clarification" if gaps else "Proceed to technical and commercial due diligence"
        })

    eligible = [o for o in offers if o["feasible"] and str(o["supplier_status"] or "").lower() in ("verified", "active")]
    if not offers:
        action = "Source additional quotations"
        reason = "No quotations have been submitted for this request yet. The review can still be completed as a sourcing-gap assessment."
        solution = "Invite suitable suppliers, request comparable itemised quotations, and confirm specifications, delivery lead time, warranty and payment terms."
    elif eligible:
        action = "Proceed to human review"
        reason = "At least one offer passes the current delivery and minimum-rating checks. This is a shortlist recommendation, not an award decision."
        solution = "Validate the recommended seller's technical compliance, stock, quotation validity, references and final landed cost before approval."
    else:
        action = "Do not award yet — seek revisions or alternatives"
        reason = "No submitted offer currently meets both the basic feasibility checks and seller-status requirements. The analysis is still complete and identifies corrective actions."
        solution = "Ask the strongest candidates to revise delivery, price or supporting evidence; invite additional sellers; or split the requirement into feasible lots if operationally safe."
    judgement = {
        "engine": "rules_fallback", "overall_assessment": reason,
        "recommended_action": action, "solution": solution,
        "seller_reviews": fallback_reviews,
        "caveat": "Assessment is based only on data entered in ProcureAI. Missing specifications, stock, certificates and references must be checked."
    }

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return judgement
    try:
        import urllib.request
        prompt = {
            "request": {k: request[k] for k in ("code", "material", "category", "quantity", "unit", "site", "required_by")},
            "days_remaining": days_left,
            "offers": quoted,
            "unquoted_supplier_options": alternatives,
            "instructions": (
                "Act as a cautious procurement bid-evaluation adviser. Evaluate only the supplied facts. "
                "Do not invent specifications, market prices, supplier capabilities, stock, certifications or references. "
                "Do not automatically award or approve any seller. If there are no bids or all bids fail, still complete "
                "the assessment and recommend concrete next steps. For every quoted seller explain strengths, gaps, "
                "specific improvements and next step. Suggest alternatives only from unquoted_supplier_options, clearly "
                "stating that they have not submitted a bid and require verification. Recommend a solution such as "
                "clarification, revised bid, phased delivery, split lots only where safe, re-sourcing, or no-award. "
                "Return JSON with keys overall_assessment, recommended_action, solution, seller_reviews (array with "
                "quote_id, seller, strengths (array), gaps (array), improvements (array), risk_level, next_step), "
                "alternative_recommendations (array of objects with supplier_id, supplier, reason, verification_note), "
                "caveat. Keep language concise and evidence-based."
            )
        }
        payload = json.dumps({
            "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You are a procurement evaluation assistant. Never make an award decision or fabricate evidence."},
                {"role": "user", "content": json.dumps(prompt, default=str)}
            ]
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=payload,
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=18) as response:
            body = json.loads(response.read().decode("utf-8"))
        ai = json.loads(body["choices"][0]["message"]["content"])
        if isinstance(ai, dict):
            ai["engine"] = "openai"
            # Keep deterministic seller review for any quote the model omitted.
            supplied = {int(x.get("quote_id")): x for x in ai.get("seller_reviews", []) if str(x.get("quote_id", "")).isdigit()}
            ai["seller_reviews"] = [supplied.get(int(x["quote_id"]), x) for x in fallback_reviews]
            known_alternatives = {int(x["supplier_id"]): x for x in alternatives}
            safe_alternatives = []
            for candidate in ai.get("alternative_recommendations", []):
                if not isinstance(candidate, dict):
                    continue
                try:
                    candidate_id = int(candidate.get("supplier_id"))
                except (TypeError, ValueError):
                    continue
                # Only recommend sellers already present in the supplier register.
                if candidate_id in known_alternatives:
                    safe_alternatives.append(known_alternatives[candidate_id])
            ai["alternative_recommendations"] = safe_alternatives
            ai.setdefault("caveat", judgement["caveat"])
            return ai
    except Exception as exc:
        print("Bid AI judgement unavailable; using rules fallback:", type(exc).__name__)
    return judgement


def _project_budget_assessment(request_id, candidate_total):
    """Compare this offer with the best known costs for the linked project requests."""
    try:
        project = one("""SELECT p.project_id,COALESCE(pi.estimated_budget,0) AS budget
            FROM project_purchase_requests pr JOIN projects p ON p.project_id=pr.project_id
            LEFT JOIN project_intake pi ON pi.project_id=p.project_id
            WHERE pr.request_id=?""", (request_id,))
        if not project:
            return {"status": "not_assessed", "budget": 0, "known_estimate": None, "unquoted_requests": None}
        budget = float(project["budget"] or 0)
        if budget <= 0:
            return {"status": "not_provided", "budget": 0, "known_estimate": None, "unquoted_requests": None}
        project_requests = rows("""SELECT r.id,r.quantity FROM project_purchase_requests pr
            JOIN requests r ON r.id=pr.request_id WHERE pr.project_id=?""", (project["project_id"],))
        known_total = 0.0
        unquoted = 0
        for item in project_requests:
            if int(item["id"]) == int(request_id):
                known_total += float(candidate_total)
                continue
            alternatives = rows("SELECT unit_price,freight,tax_rate FROM quotes WHERE request_id=?", (item["id"],))
            if not alternatives:
                unquoted += 1
                continue
            costs = []
            for quote in alternatives:
                price = float(quote["unit_price"] or 0)
                freight = float(quote["freight"] or 0)
                tax_rate = float(quote["tax_rate"] or 0)
                if price > 0 and freight >= 0 and 0 <= tax_rate <= 100:
                    subtotal = float(item["quantity"]) * price
                    costs.append(subtotal + subtotal * tax_rate / 100 + freight)
            if costs:
                known_total += min(costs)
            else:
                unquoted += 1
        if known_total > budget:
            status = "over_budget"
        elif unquoted:
            status = "incomplete"
        else:
            status = "within_budget"
        return {"status": status, "budget": budget, "known_estimate": round(known_total, 2),
                "unquoted_requests": unquoted}
    except sqlite3.OperationalError:
        # Older standalone databases may not yet contain the project workspace tables.
        return {"status": "not_assessed", "budget": 0, "known_estimate": None, "unquoted_requests": None}


def assess_offer_feasibility(*, days_left, lead_days, rating, supplier_status,
                             technical_status, stock_status, compliance_status,
                             budget_status, commercial_valid=True):
    """Return a conservative, explainable readiness result from recorded evidence."""
    blockers, unknowns, warnings = [], [], []
    delivery = "on_time" if int(lead_days) <= int(days_left) else "late"
    supplier = str(supplier_status or "").strip().lower()
    technical = str(technical_status or "not_assessed").strip().lower()
    stock = str(stock_status or "not_confirmed").strip().lower()
    compliance = str(compliance_status or "pending").strip().lower()
    if delivery == "late":
        blockers.append("Quoted lead time exceeds the remaining delivery window.")
    if supplier not in ("verified", "active"):
        blockers.append("Supplier verification is incomplete.")
    if float(rating or 0) < 2.5:
        blockers.append("Supplier rating is below the minimum 2.5/5 threshold.")
    if not commercial_valid:
        blockers.append("Commercial quotation values are incomplete or invalid.")
    if technical == "deviation":
        blockers.append("Supplier declared a technical deviation from the requirement.")
    elif technical != "compliant":
        unknowns.append("Technical compliance has not been confirmed.")
    if stock == "unavailable":
        blockers.append("Supplier reports that the required stock is unavailable.")
    elif stock != "available":
        unknowns.append("Stock availability is not fully confirmed.")
    if compliance == "non_compliant":
        blockers.append("Required regulatory or product compliance evidence is non-compliant.")
    elif compliance not in ("verified", "not_applicable"):
        unknowns.append("Certificates and regulatory compliance evidence are pending review.")
    if budget_status == "over_budget":
        blockers.append("Known project procurement estimates exceed the approved project budget.")
    elif budget_status != "within_budget":
        unknowns.append("Project budget fit cannot yet be confirmed for all linked requests.")
    if float(rating or 0) < 3.5 and float(rating or 0) >= 2.5:
        warnings.append("Supplier rating is below the preferred 3.5/5 threshold.")
    status = "infeasible" if blockers else ("conditional" if unknowns else "feasible")
    return {"status": status, "delivery": delivery, "supplier": supplier,
            "technical_compliance": technical, "stock": stock, "regulatory_compliance": compliance,
            "budget": budget_status, "blockers": blockers, "unknowns": unknowns, "warnings": warnings}


def analyse_request(request_id):
    req = one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not req:
        raise ValueError("Purchase request not found")
    offers = rows("""SELECT q.*,s.name supplier,s.rating,s.on_time,s.status supplier_status
        FROM quotes q JOIN suppliers s ON s.id=q.supplier_id
        WHERE q.request_id=?""", (request_id,))
    results = []
    required = date.fromisoformat(req["required_by"])
    days_left = max(0, (required-date.today()).days)
    for q in offers:
        qty = D(str(req["quantity"]))
        price = D(str(q["unit_price"]))
        tax = price * D(str(q["tax_rate"])) / D("100")
        financing = -(price * D("0.18") * D(str(q["payment_days"])) / D("365"))
        late_days = max(0, int(q["lead_days"]) - days_left)
        delay_risk = price * D("0.03") * D(str(late_days)) / D("30")
        reliability_risk = price * D("0.05") * (D("1") - D(str(q["on_time"])) / D("100"))
        unit_tco = price + tax + financing + delay_risk + reliability_risk
        total = unit_tco * qty + D(str(q["freight"]))
        landed_total = qty * price + qty * tax + D(str(q["freight"]))
        seller_status = str(q["supplier_status"] or "").strip().lower()
        commercial_valid = float(q["unit_price"]) > 0 and float(q["freight"]) >= 0 and 0 <= float(q["tax_rate"]) <= 100 and float(req["quantity"]) > 0
        budget = _project_budget_assessment(request_id, float(landed_total))
        feasibility = assess_offer_feasibility(
            days_left=days_left, lead_days=q["lead_days"], rating=q["rating"],
            supplier_status=seller_status, technical_status=q.get("technical_status"),
            stock_status=q.get("stock_status"), compliance_status=q.get("compliance_status"),
            budget_status=budget["status"], commercial_valid=commercial_valid)
        issues = feasibility["blockers"] + feasibility["unknowns"] + feasibility["warnings"]
        results.append({
            **q, "unit_tco": float(unit_tco), "estimated_total": float(total),
            "feasible": feasibility["status"] == "feasible",
            "basic_screen_passed": int(q["lead_days"]) <= days_left and int(float(q["rating"] or 0) * 100) >= 250,
            "award_eligible": feasibility["status"] == "feasible",
            "feasibility": {**feasibility, "budget": {**budget, "status": budget["status"]}},
            "days_left": days_left, "issues": issues,
            "components": {"base":float(price),"tax":float(tax),"financing":float(financing),"delay_risk":float(delay_risk),"reliability_risk":float(reliability_risk)}
        })
    results.sort(key=lambda x: (x["feasibility"]["status"] != "feasible", x["estimated_total"]))

    quoted_supplier_ids = {int(o["supplier_id"]) for o in offers}
    all_suppliers = rows("""SELECT id,name,city,rating,on_time,status,services,phone,email,website,address
        FROM suppliers ORDER BY CASE WHEN lower(status) IN ('verified','active') THEN 0 ELSE 1 END,
        rating DESC, name COLLATE NOCASE""")
    keywords = [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", f"{req['material']} {req['category']}")]
    alternatives = []
    for seller in all_suppliers:
        if int(seller["id"]) in quoted_supplier_ids:
            continue
        services = str(seller["services"] or "").lower()
        name_city = f"{seller['name']} {seller['city']}".lower()
        matches = [word for word in keywords if word in services or word in name_city]
        local_match = bool(req["site"] and any(part.strip().lower() in str(seller["city"] or "").lower()
                          for part in str(req["site"]).replace(",", " ").split() if len(part.strip()) > 3))
        if matches or local_match:
            alternatives.append({
                "supplier_id": int(seller["id"]), "supplier": seller["name"],
                "city": seller["city"], "status": seller["status"],
                "services": seller["services"], "rating": float(seller["rating"] or 0),
                "reason": "Service/category keyword match" if matches else "Location match only — confirm capability",
                "verification_note": "Not yet quoted for this request. Verify identity, technical fit, stock, references and bank details before use."
            })
    alternatives = alternatives[:8]
    judgement = _bid_ai_judgement(req, results, alternatives, days_left)
    review_by_id = {int(x.get("quote_id")): x for x in judgement.get("seller_reviews", [])
                    if isinstance(x, dict) and str(x.get("quote_id", "")).isdigit()}
    for offer in results:
        offer["ai_review"] = review_by_id.get(int(offer["id"]), {})
    eligible = [x for x in results if x.get("award_eligible")]
    preferred = eligible[0] if eligible else (results[0] if results else None)
    return {
        "request": req, "offers": results,
        "recommendation": eligible[0] if eligible else None,
        "preferred_offer": preferred,
        "decision_support": judgement,
        "alternatives": alternatives,
        "analysis_status": "completed",
        "disclaimer": "Feasibility is confirmed only when project budget, delivery, supplier verification, technical compliance, stock and regulatory evidence are all supported by recorded data. Unknown information remains conditional; human approval is still required."
    }

class Handler(BaseHTTPRequestHandler):
    server_version = "ProcureAI/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def send_json(self, payload, status=200):
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 1_000_000:
            raise ValueError("Request body too large")
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/dashboard":
                return self.send_json(dashboard())
            if path == "/api/analysis":
                from urllib.parse import parse_qs
                rid = int(parse_qs(urlparse(self.path).query).get("request_id", ["0"])[0])
                return self.send_json(analyse_request(rid))
            if path == "/api/requests":
                return self.send_json({"requests": rows("SELECT * FROM requests ORDER BY id DESC")})
            if path == "/api/suppliers":
                return self.send_json({"suppliers": rows("SELECT * FROM suppliers ORDER BY rating DESC")})
            if path == "/api/orders":
                return self.send_json({"orders": dashboard()["orders"]})
            if path == "/":
                return self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path.removeprefix("/static/")
                if "/" in name or "\\" in name:
                    return self.send_json({"error":"Invalid path"},400)
                mime = "text/javascript; charset=utf-8" if name.endswith(".js") else "text/css; charset=utf-8"
                return self.serve_file(STATIC / name, mime)
            return self.send_json({"error":"Not found"},404)
        except (ValueError, InvalidOperation, KeyError) as e:
            return self.send_json({"error":str(e)},400)
        except Exception as e:
            return self.send_json({"error":f"Server error: {e}"},500)

    def serve_file(self, path, mime):
        if not path.is_file():
            return self.send_json({"error":"Application asset missing"},404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self.body()
            if path == "/api/requests":
                required = ["material","category","quantity","unit","site","required_by"]
                if any(not str(data.get(k,"")).strip() for k in required):
                    raise ValueError("Complete all required purchase request fields")
                qty = float(data["quantity"])
                if qty <= 0: raise ValueError("Quantity must be greater than zero")
                with connect() as db:
                    next_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM requests").fetchone()[0]
                    code = f"PR-{next_id+141:04d}"
                    cur = db.execute("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,priority,status,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (code,data["material"].strip(),data["category"],qty,data["unit"],data["site"].strip(),
                         data["required_by"],data.get("priority","Normal"),"Open",datetime.utcnow().isoformat()))
                    rid = cur.lastrowid
                return self.send_json({"success":True,"id":rid,"code":code},201)
            if path == "/api/quotes":
                required = ["supplier_id","request_id","unit_price","freight","tax_rate","lead_days","payment_days"]
                if any(k not in data for k in required): raise ValueError("Missing quote fields")
                vals = [float(data[k]) for k in ["unit_price","freight","tax_rate"]]
                if vals[0] <= 0 or vals[1] < 0 or not 0 <= vals[2] <= 100: raise ValueError("Check quote price, freight and tax values")
                with connect() as db:
                    cur = db.execute("""INSERT INTO quotes(supplier_id,request_id,unit_price,freight,tax_rate,lead_days,payment_days,note,created_at)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (int(data["supplier_id"]),int(data["request_id"]),vals[0],vals[1],vals[2],
                         int(data["lead_days"]),int(data["payment_days"]),data.get("note",""),datetime.utcnow().isoformat()))
                    qid=cur.lastrowid
                return self.send_json({"success":True,"id":qid},201)
            if path.startswith("/api/orders/") and path.endswith("/decision"):
                oid=int(path.split("/")[3])
                action=data.get("action")
                if action not in ("approve","reject"): raise ValueError("Decision must be approve or reject")
                status="Approved" if action=="approve" else "Rejected"
                with connect() as db:
                    cur=db.execute("UPDATE orders SET status=?,decision_note=? WHERE id=? AND status='Pending approval'",
                                   (status,data.get("note",""),oid))
                    if cur.rowcount == 0: raise ValueError("Order is not awaiting approval")
                    if action == "approve":
                        db.execute("UPDATE requests SET status='Awarded' WHERE id=(SELECT request_id FROM orders WHERE id=?)",(oid,))
                return self.send_json({"success":True,"status":status})
            if path == "/api/orders":
                rid=int(data["request_id"]); sid=int(data["supplier_id"])
                req=one("SELECT * FROM requests WHERE id=?",(rid,))
                quote=one("""SELECT * FROM quotes WHERE request_id=? AND supplier_id=? ORDER BY id DESC LIMIT 1""",(rid,sid))
                supplier=one("SELECT * FROM suppliers WHERE id=?",(sid,))
                if not req or not quote or not supplier: raise ValueError("A matching request, supplier and quote are required")
                qty=D(str(req["quantity"])); price=D(str(quote["unit_price"]))
                tax=(qty*price*D(str(quote["tax_rate"]))/D("100"))
                freight=D(str(quote["freight"])); subtotal=qty*price; total=subtotal+tax+freight
                reasons=[]
                if total>D("2500000"): reasons.append("Order exceeds the automatic approval limit")
                if supplier["rating"]<3.5: reasons.append("Supplier rating is below the preferred threshold")
                if quote["lead_days"] > max(0,(date.fromisoformat(req["required_by"])-date.today()).days):
                    reasons.append("Quoted lead time misses the requested delivery date")
                # Every purchase order is a proposal until a human approves it.
                reasons.append("Human approval required before any purchase commitment")
                status="Pending approval"
                with connect() as db:
                    code=f"PO-{datetime.utcnow():%y%m%d}-{db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]+1:03d}"
                    cur=db.execute("""INSERT INTO orders(code,supplier_id,request_id,quantity,unit_price,freight,tax,total,status,required_by,created_at,decision_note)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (code,sid,rid,float(qty),float(price),float(freight),float(tax),float(total),status,
                         req["required_by"],datetime.utcnow().isoformat(),"; ".join(reasons)))
                    oid=cur.lastrowid
                return self.send_json({"success":True,"id":oid,"code":code,"status":status,"total":float(total),"reasons":reasons},201)
            return self.send_json({"error":"Not found"},404)
        except (ValueError, InvalidOperation, KeyError, TypeError) as e:
            return self.send_json({"error":str(e)},400)
        except Exception as e:
            return self.send_json({"error":f"Server error: {e}"},500)


def main():
    init_db()
    host, port = "127.0.0.1", 8000
    print("\\n  ProcureAI is running")
    print(f"  Open http://{host}:{port} in your browser")
    print("  Press Ctrl+C to stop. Data persists in procureai.db.\\n")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
