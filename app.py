#!/usr/bin/env python3
"""ProcureAI browser application — standard-library edition.

Run: python app.py
Open: http://127.0.0.1:8000
Data is stored in procureai.db beside this file.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
DB = BASE / "procureai.db"
STATIC = BASE / "static"
D = Decimal


def money(value):
    return str(D(str(value)).quantize(D("0.01"), rounding=ROUND_HALF_UP))


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
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
        if db.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0] == 0:
            db.executemany(
                "INSERT INTO suppliers(name,city,rating,on_time,contact) VALUES(?,?,?,?,?)",
                [
                    ("Steelyard Ltd", "Lagos", 4.2, 88, "sales@steelyard.example"),
                    ("Metrode Supplies", "Ikeja", 3.6, 79, "quotes@metrode.example"),
                    ("BuildMart Distribution", "Lekki", 4.6, 94, "procurement@buildmart.example"),
                    ("NaijaSteel Traders", "Ikorodu", 3.1, 68, "sales@naijasteel.example"),
                    ("CrestCon Materials", "Apapa", 4.0, 85, "hello@crestcon.example"),
                ],
            )
        if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0:
            today = date.today()
            seed = [
                ("PR-0142", "Rebar T10 (12m)", "Reinforcement", 14000, "kg", "Lagos Tower", today+timedelta(days=5), "Urgent", "Open"),
                ("PR-0143", "Rebar T16 (12m)", "Reinforcement", 9000, "kg", "Lagos Tower", today+timedelta(days=5), "Normal", "Open"),
                ("PR-0144", "OPC 42.5 Cement", "Cementitious", 30000, "kg", "Lagos Tower", today+timedelta(days=3), "Urgent", "Open"),
                ("PR-0145", "Granite Aggregate 20mm", "Aggregate", 40000, "kg", "Lekki Phase II", today+timedelta(days=6), "Normal", "Open"),
                ("PR-0146", "Formwork Plywood 18mm", "Formwork", 180, "ea", "Lagos Tower", today+timedelta(days=9), "Normal", "Open"),
            ]
            db.executemany("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,priority,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                [(a,b,c,d,e,f,g.isoformat(),h,i,datetime.utcnow().isoformat()) for a,b,c,d,e,f,g,h,i in seed])
            # Seed comparable offers. Values are demo data, not live supplier quotes.
            offers = [
                (1,1,795,420000,7.5,4,30),(2,1,780,310000,7.5,6,45),(3,1,812,540000,7.5,3,60),
                (1,2,768,420000,7.5,4,30),(2,2,755,310000,7.5,6,45),(3,2,781,540000,7.5,3,60),
                (1,3,172,420000,7.5,3,30),(5,3,169,260000,7.5,5,30),(3,3,178,540000,7.5,2,60),
                (2,4,21,310000,7.5,5,45),(5,4,19,260000,7.5,4,30),
                (3,5,28500,540000,7.5,4,60),(5,5,27200,260000,7.5,6,30),
            ]
            db.executemany("""INSERT INTO quotes(supplier_id,request_id,unit_price,freight,tax_rate,lead_days,payment_days,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                [(s,r,p,f,t,l,pt,datetime.utcnow().isoformat()) for s,r,p,f,t,l,pt in offers])


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
        "recent_quotes": rows("""SELECT q.*,s.name supplier,r.code request_code,r.material
            FROM quotes q JOIN suppliers s ON s.id=q.supplier_id
            JOIN requests r ON r.id=q.request_id ORDER BY q.id DESC LIMIT 8""")
    }


def analyse_request(request_id):
    req = one("SELECT * FROM requests WHERE id=?", (request_id,))
    if not req:
        raise ValueError("Purchase request not found")
    offers = rows("""SELECT q.*,s.name supplier,s.rating,s.on_time
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
        feasible = int(q["lead_days"]) <= days_left and int(q["rating"] * 100) >= 250
        results.append({
            **q, "unit_tco": float(unit_tco), "estimated_total": float(total),
            "feasible": feasible, "days_left": days_left,
            "components": {"base":float(price),"tax":float(tax),"financing":float(financing),
                           "delay_risk":float(delay_risk),"reliability_risk":float(reliability_risk)}
        })
    results.sort(key=lambda x: (not x["feasible"], x["estimated_total"]))
    return {"request": req, "offers": results,
            "recommendation": next((x for x in results if x["feasible"]), None),
            "disclaimer": "Demo evaluation: formula-based estimate from entered quote data. Confirm supplier capacity, quote validity and commercial terms before award."}


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
                status="Pending approval" if reasons else "Approved"
                with connect() as db:
                    code=f"PO-{datetime.utcnow():%y%m%d}-{db.execute('SELECT COUNT(*) FROM orders').fetchone()[0]+1:03d}"
                    cur=db.execute("""INSERT INTO orders(code,supplier_id,request_id,quantity,unit_price,freight,tax,total,status,required_by,created_at,decision_note)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (code,sid,rid,float(qty),float(price),float(freight),float(tax),float(total),status,
                         req["required_by"],datetime.utcnow().isoformat(),"; ".join(reasons)))
                    oid=cur.lastrowid
                    db.execute("UPDATE requests SET status='Awarded' WHERE id=?",(rid,))
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
