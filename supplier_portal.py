"""Secure supplier RFQ portal for ProcureAI.

Invitation tokens are random, stored only as SHA-256 hashes, expire, and are
single-use. Supplier submissions become normal dashboard quotations.
"""
import hashlib
import html
import json
import os
import secrets
import smtplib
import ssl
import sqlite3
import time
import urllib.parse
import urllib.request
import urllib.error
import base64
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import app as dashboard_module

router = APIRouter()
INVITE_DAYS = 14

def _now():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _project_brief(db, request_id):
    """Return the full managed-project context for a linked RFQ, if available."""
    linked = db.execute("""SELECT p.project_id,p.title,p.client_name,p.agency_details,
        p.executive_summary,p.deliverables_json,p.start_date,p.due_date,
        i.project_location,i.project_category,i.estimated_budget,i.expected_duration_days
        FROM project_purchase_requests pr
        JOIN projects p ON p.project_id=pr.project_id
        LEFT JOIN project_intake i ON i.project_id=p.project_id
        WHERE pr.request_id=?""", (request_id,)).fetchone()
    if not linked:
        return None
    try:
        deliverables = json.loads(linked["deliverables_json"] or "[]")
    except (TypeError, ValueError):
        deliverables = []
    return {
        "title": linked["title"] or "",
        "client": linked["client_name"] or "",
        "agency_details": linked["agency_details"] or "",
        "summary": linked["executive_summary"] or "",
        "deliverables": [str(item) for item in deliverables if str(item).strip()],
        "location": linked["project_location"] or "",
        "category": linked["project_category"] or "",
        "budget": float(linked["estimated_budget"] or 0),
        "duration_days": int(linked["expected_duration_days"] or 0),
        "start_date": linked["start_date"] or "",
        "due_date": linked["due_date"] or "",
    }

def _project_brief_html(project_brief, safe):
    if not project_brief:
        return ""
    budget = "₦{:,.2f}".format(project_brief["budget"]) if project_brief["budget"] > 0 else "Not specified"
    rows = [
        ("Project", project_brief["title"] or "Not specified"),
        ("Client / organisation", project_brief["client"] or project_brief["agency_details"] or "Not specified"),
        ("Overall project budget", budget),
        ("Project summary", project_brief["summary"] or "Not supplied"),
        ("Project category", project_brief["category"] or "Not specified"),
        ("Project location", project_brief["location"] or "Not specified"),
        ("Project timeline", (project_brief["start_date"] or "Not specified") + " to " + (project_brief["due_date"] or "Not specified")),
        ("Key deliverables", "; ".join(project_brief["deliverables"]) if project_brief["deliverables"] else "Not specified"),
    ]
    table_rows = "".join(
        '<tr><td style="padding:9px;background:#f7f9fc;width:38%">' + safe(label) +
        '</td><td style="padding:9px">' + safe(value) + '</td></tr>'
        for label, value in rows
    )
    return (
        '<h3>Project brief</h3><table style="width:100%;border-collapse:collapse">' +
        table_rows + '</table><p style="font-size:13px;color:#79520c;background:#fff8e8;padding:12px;border-radius:8px">'
        'The overall project budget applies to the entire project; it is provided as context and is not necessarily '
        'the amount allocated to this individual purchase request. Please submit a competitive, realistic offer.</p>'
    )


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS supplier_rfq_invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL,
        supplier_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        recipient TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        sent_at TEXT,
        expires_at TEXT NOT NULL,
        submitted_at TEXT,
        quote_id INTEGER,
        error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(request_id,supplier_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_supplier_rfq_request ON supplier_rfq_invites(request_id,status)")

def _send_email(recipient, subject, text_body, html_body):
    parsed = parseaddr(recipient)[1]
    if not parsed or parsed != recipient or "@" not in parsed:
        raise RuntimeError("Supplier email address is missing or invalid.")
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = recipient
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    if os.getenv("EMAIL_PROVIDER", "").strip().lower() == "gmail":
        client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
        client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
        refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
        sender = os.getenv("GMAIL_SENDER_EMAIL", "").strip()
        if not all((client_id, client_secret, refresh_token, sender)):
            raise RuntimeError("Gmail OAuth credentials are incomplete.")
        message["From"] = sender
        payload = urllib.parse.urlencode({"client_id":client_id,"client_secret":client_secret,"refresh_token":refresh_token,"grant_type":"refresh_token"}).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=payload, headers={"Content-Type":"application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as response:
            access = json.loads(response.read().decode()).get("access_token","")
        if not access:
            raise RuntimeError("Gmail OAuth did not return an access token.")
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
        req = urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", data=json.dumps({"raw":raw}).encode(), headers={"Authorization":"Bearer "+access,"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=25) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError("Gmail API rejected the RFQ email.")
        return
    host=os.getenv("SMTP_HOST","").strip()
    username=os.getenv("SMTP_USERNAME","").strip()
    password=os.getenv("SMTP_PASSWORD","")
    sender=os.getenv("SMTP_FROM_EMAIL",username).strip()
    if not all((host,username,password,sender)):
        raise RuntimeError("Supplier email is not configured. Configure the existing Gmail OAuth or SMTP settings.")
    message["From"]=sender
    port=int(os.getenv("SMTP_PORT","587"))
    if os.getenv("SMTP_USE_SSL","false").lower()=="true":
        with smtplib.SMTP_SSL(host,port,timeout=25,context=ssl.create_default_context()) as server:
            server.login(username,password); server.send_message(message)
    else:
        with smtplib.SMTP(host,port,timeout=25) as server:
            server.ehlo()
            if os.getenv("SMTP_USE_TLS","true").lower()=="true":
                server.starttls(context=ssl.create_default_context()); server.ehlo()
            server.login(username,password); server.send_message(message)

def _supplier_matches(supplier, category):
    status=(supplier["status"] or "").strip().casefold()
    name=(supplier["name"] or "").casefold()
    if status not in {"verified","active"} or "[fictional demo]" in name or "[demo" in name:
        return False
    services=(supplier["services"] or "").casefold()
    category=(category or "").strip().casefold()
    if not services or not category:
        return True
    terms=[x for x in category.replace("&"," ").replace("/"," ").split() if len(x)>2]
    return any(term in services for term in terms) or category in services

def dispatch_request_rfq(request_id: int, actor: str = "ProcureAI"):
    """Create one-time invitations and email verified, category-matched suppliers."""
    sent, skipped, failed = [], [], []
    with dashboard_module.connect() as db:
        ensure_tables(db)
        req=db.execute("SELECT * FROM requests WHERE id=?",(request_id,)).fetchone()
        if not req:
            raise HTTPException(status_code=404,detail="Purchase request not found.")
        if req["status"] != "Open":
            raise HTTPException(status_code=409,detail="This purchase request is no longer accepting supplier quotations.")
        project_brief = _project_brief(db, request_id)
        suppliers=db.execute("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE").fetchall()
        for supplier in suppliers:
            if not _supplier_matches(supplier, req["category"]):
                skipped.append(supplier["name"]); continue
            email=(supplier["email"] or "").strip()
            if not email:
                skipped.append(supplier["name"]+" (no email)"); continue
            existing=db.execute("SELECT id,status,expires_at,created_at FROM supplier_rfq_invites WHERE request_id=? AND supplier_id=?",(request_id,supplier["id"])).fetchone()
            if existing:
                state=existing["status"]
                if state=="submitted":
                    skipped.append(supplier["name"]); continue
                if state=="sent":
                    try:
                        if datetime.fromisoformat(existing["expires_at"].replace("Z","+00:00")).timestamp()>time.time():
                            skipped.append(supplier["name"]); continue
                    except (TypeError,ValueError):
                        skipped.append(supplier["name"]); continue
                if state=="sending":
                    try:
                        started=datetime.fromisoformat(existing["created_at"].replace("Z","+00:00")).timestamp()
                        if started>time.time()-600:
                            skipped.append(supplier["name"]); continue
                    except (TypeError,ValueError):
                        skipped.append(supplier["name"]); continue
            raw=secrets.token_urlsafe(36)
            expires=(datetime.utcnow()+timedelta(days=INVITE_DAYS)).replace(microsecond=0).isoformat()+"Z"
            if existing:
                invite_id=existing["id"]
                db.execute("""UPDATE supplier_rfq_invites SET token_hash=?,recipient=?,status='sending',sent_at=NULL,expires_at=?,submitted_at=NULL,quote_id=NULL,error='',created_at=? WHERE id=?""",
                    (_hash(raw),email,expires,_now(),invite_id))
            else:
                db.execute("""INSERT INTO supplier_rfq_invites(request_id,supplier_id,token_hash,recipient,status,expires_at,created_at)
                    VALUES(?,?,?,?,?,?,?)""",(request_id,supplier["id"],_hash(raw),email,"sending",expires,_now()))
                invite_id=db.execute("SELECT last_insert_rowid()").fetchone()[0]
            base=os.getenv("PROCUREAI_PUBLIC_URL",os.getenv("APP_BASE_URL","https://procureaiv2-production.up.railway.app")).split(",")[0].strip().rstrip("/")
            link=base+"/supplier-portal/"+raw
            q=dict(req); s=dict(supplier)
            safe=lambda v:html.escape(str(v or "—"),quote=True)
            subject=f"Request for Quotation {req['code']} – {req['material']}"
            project_text = ""
            if project_brief:
                project_text = "\n\\nPROJECT BRIEF\\nProject: " + str(project_brief["title"]) + "\nClient/organisation: " + str(project_brief["client"] or project_brief["agency_details"] or "Not specified") + "\nOverall project budget: " + (("₦{:,.2f}".format(project_brief["budget"])) if project_brief["budget"] > 0 else "Not specified") + "\nProject summary: " + str(project_brief["summary"] or "Not supplied") + "\nProject location: " + str(project_brief["location"] or "Not specified") + "\nProject timeline: " + str(project_brief["start_date"] or "Not specified") + " to " + str(project_brief["due_date"] or "Not specified") + "\nKey deliverables: " + ("; ".join(project_brief["deliverables"]) if project_brief["deliverables"] else "Not specified")
            text_body="Dear "+s["name"]+",\\n\\nYou are invited to submit a quotation for "+req["code"]+"."+project_text+"\n\\nPURCHASE REQUEST\\nItem: "+req["material"]+"\nCategory: "+str(req["category"])+"\nQuantity: "+str(req["quantity"])+" "+str(req["unit"])+"\nDelivery location: "+str(req["site"])+"\nRequired by: "+str(req["required_by"])+"\n\\nTechnical requirements:\\n"+str(req["additional_information"] or "See request details.")+"\n\\nPlease prepare a competitive, realistic quotation that considers the published overall project budget and the full scope. The overall project budget covers the entire project, not necessarily this individual purchase request.\\n\\nSubmit securely using your unique link (valid for "+str(INVITE_DAYS)+" days):\\n"+link+"\n\\nThis invitation is not a purchase order or award."
            body=f"""<!doctype html><html><body style="margin:0;padding:24px;background:#f3f5f8;font:14px Arial;color:#243044"><main style="max-width:640px;margin:auto;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden"><header style="background:#172b4d;color:white;padding:24px"><b style="color:#f5a623">PROCUREAI</b><h2>Request for Quotation</h2><span>{safe(req['code'])}</span></header><section style="padding:26px"><p>Dear <b>{safe(s['name'])}</b>,</p><p>You are invited to submit a quotation for this procurement requirement.</p><table style="width:100%;border-collapse:collapse"><tr><td style="padding:9px;background:#f7f9fc">Item</td><td style="padding:9px">{safe(req['material'])}</td></tr><tr><td style="padding:9px;background:#f7f9fc">Category</td><td style="padding:9px">{safe(req['category'])}</td></tr><tr><td style="padding:9px;background:#f7f9fc">Quantity</td><td style="padding:9px">{safe(str(req['quantity'])+' '+str(req['unit']))}</td></tr><tr><td style="padding:9px;background:#f7f9fc">Delivery location</td><td style="padding:9px">{safe(req['site'])}</td></tr><tr><td style="padding:9px;background:#f7f9fc">Required by</td><td style="padding:9px">{safe(req['required_by'])}</td></tr></table>{_project_brief_html(project_brief, safe)}<h3>Technical requirements</h3><p style="white-space:pre-wrap;line-height:1.6">{safe(req['additional_information'] or 'No additional specification supplied.')}</p><p style="margin:26px 0"><a href="{safe(link)}" style="background:#2457c5;color:white;text-decoration:none;padding:14px 22px;border-radius:8px;font-weight:bold">Submit quotation securely</a></p><p style="font-size:12px;color:#64748b">This unique link expires in {INVITE_DAYS} days. This RFQ is not a purchase order, award or authority to commence supply.</p></section></main></body></html>"""
            try:
                _send_email(email,subject,text_body,body)
                db.execute("UPDATE supplier_rfq_invites SET status='sent',sent_at=? WHERE id=?",( _now(),invite_id))
                sent.append({"supplier":s["name"],"email":email})
            except Exception as exc:
                db.execute("UPDATE supplier_rfq_invites SET status='failed',error=? WHERE id=?",(str(exc)[:300],invite_id))
                failed.append({"supplier":s["name"],"error":str(exc)[:240]})
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",(actor,"dispatch_supplier_rfqs","purchase_request",str(request_id),json.dumps({"sent":len(sent),"skipped":len(skipped),"failed":len(failed)}),_now()))
    return {"sent":sent,"skipped":skipped,"failed":failed,"sent_count":len(sent),"failed_count":len(failed)}

class SupplierPortalSubmission(BaseModel):
    unit_price: float = Field(gt=0)
    freight: float = Field(default=0,ge=0)
    tax_rate: float = Field(default=7.5,ge=0,le=100)
    lead_days: int = Field(ge=0,le=3650)
    payment_days: int = Field(default=30,ge=0,le=3650)
    warranty: str = Field(default="",max_length=500)
    validity_days: int = Field(default=30,ge=1,le=365)
    technical_response: str = Field(default="",max_length=8000)
    exclusions: str = Field(default="",max_length=4000)
    note: str = Field(default="",max_length=4000)

def _get_invite(token):
    if not token or len(token)>200: return None
    with dashboard_module.connect() as db:
        ensure_tables(db)
        row=db.execute("""SELECT i.*,s.name supplier_name,r.code request_code,r.material,r.category,r.quantity,r.unit,r.site,r.required_by,r.additional_information,r.status request_status
            FROM supplier_rfq_invites i JOIN suppliers s ON s.id=i.supplier_id JOIN requests r ON r.id=i.request_id
            WHERE i.token_hash=?""",(_hash(token),)).fetchone()
    return row

@router.get("/supplier-portal/{token}",response_class=HTMLResponse)
def supplier_portal_page(token: str):
    invite=_get_invite(token)
    if not invite: raise HTTPException(status_code=404,detail="This quotation link is invalid.")
    return HTMLResponse(PORTAL_HTML.replace("__TOKEN__",html.escape(token,quote=True)))

@router.get("/api/supplier-portal/{token}")
def supplier_portal_details(token: str):
    invite=_get_invite(token)
    if not invite: raise HTTPException(status_code=404,detail="Invitation not found.")
    if invite["status"]=="submitted": return {"status":"submitted","message":"Your quotation has already been received. Thank you."}
    if invite["status"]!="sent" or datetime.fromisoformat(invite["expires_at"].replace("Z","+00:00")).timestamp()<time.time():
        raise HTTPException(status_code=410,detail="This invitation has expired or is no longer active. Contact the procurement team.")
    with dashboard_module.connect() as db:
        project_brief = _project_brief(db, invite["request_id"])
    return {"status":"open","supplier":invite["supplier_name"],"project":project_brief,"request":{"code":invite["request_code"],"material":invite["material"],"category":invite["category"],"quantity":invite["quantity"],"unit":invite["unit"],"site":invite["site"],"required_by":invite["required_by"],"additional_information":invite["additional_information"]}}

@router.post("/api/supplier-portal/{token}")
def supplier_portal_submit(token: str, payload: SupplierPortalSubmission):
    # Hash-only bearer links are single-use. BEGIN IMMEDIATE serializes concurrent
    # submissions before checking the invitation and supplier/request duplicate.
    if not token or len(token) > 200:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    note=json.dumps({
        "source":"secure_supplier_portal",
        "warranty":payload.warranty,
        "validity_days":payload.validity_days,
        "technical_response":payload.technical_response,
        "exclusions":payload.exclusions,
        "supplier_note":payload.note
    },ensure_ascii=False)
    with dashboard_module.connect() as db:
        ensure_tables(db)
        db.execute("BEGIN IMMEDIATE")
        invite=db.execute("""SELECT i.*,r.status request_status
            FROM supplier_rfq_invites i JOIN requests r ON r.id=i.request_id
            WHERE i.token_hash=?""",(_hash(token),)).fetchone()
        if not invite:
            raise HTTPException(status_code=404,detail="Invitation not found.")
        if invite["status"]=="submitted" or invite["quote_id"]:
            raise HTTPException(status_code=409,detail="A quotation has already been submitted for this invitation.")
        if invite["status"]!="sent" or datetime.fromisoformat(invite["expires_at"].replace("Z","+00:00")).timestamp()<time.time():
            raise HTTPException(status_code=410,detail="This invitation has expired or is no longer active.")
        if invite["request_status"]!="Open":
            raise HTTPException(status_code=409,detail="This purchase request is no longer accepting quotations.")
        duplicate=db.execute("SELECT id FROM quotes WHERE request_id=? AND supplier_id=?",(invite["request_id"],invite["supplier_id"])).fetchone()
        if duplicate:
            raise HTTPException(status_code=409,detail="This supplier already has a quotation recorded for this purchase request. Contact the buyer if a revision is required.")
        cur=db.execute("""INSERT INTO quotes(supplier_id,request_id,unit_price,freight,tax_rate,lead_days,payment_days,note,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (invite["supplier_id"],invite["request_id"],payload.unit_price,payload.freight,payload.tax_rate,
             payload.lead_days,payload.payment_days,note,_now()))
        updated=db.execute("""UPDATE supplier_rfq_invites SET status='submitted',submitted_at=?,quote_id=?
            WHERE id=? AND status='sent' AND quote_id IS NULL""",(_now(),cur.lastrowid,invite["id"]))
        if updated.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=409,detail="This invitation was already submitted.")
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT NOT NULL,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,details_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            ("Supplier portal","supplier_quote_submitted","quotation",str(cur.lastrowid),
             json.dumps({"request_id":invite["request_id"],"supplier_id":invite["supplier_id"]}),_now()))
    return {"success":True,"message":"Your quotation has been submitted successfully. The procurement team will review it.","quote_id":cur.lastrowid}

@router.get("/api/rfq/status")
def admin_rfq_status():
    with dashboard_module.connect() as db:
        ensure_tables(db)
        rows=db.execute("""SELECT r.id request_id,r.code,
            COUNT(i.id) invited_count,
            COALESCE(SUM(CASE WHEN i.status='sent' THEN 1 ELSE 0 END),0) sent_count,
            COALESCE(SUM(CASE WHEN i.status='submitted' THEN 1 ELSE 0 END),0) submitted_count,
            COALESCE(SUM(CASE WHEN i.status='failed' THEN 1 ELSE 0 END),0) failed_count,
            (SELECT COUNT(*) FROM quotes q WHERE q.request_id=r.id) quote_count
            FROM requests r LEFT JOIN supplier_rfq_invites i ON i.request_id=r.id
            GROUP BY r.id ORDER BY r.id DESC""").fetchall()
    return {"requests":[dict(row) for row in rows]}

@router.get("/api/rfq/request/{request_id}")
def admin_rfq_request_detail(request_id: int):
    with dashboard_module.connect() as db:
        ensure_tables(db)
        req=db.execute("SELECT id,code,material,status FROM requests WHERE id=?",(request_id,)).fetchone()
        if not req:
            raise HTTPException(status_code=404,detail="Purchase request not found.")
        invites=db.execute("""SELECT i.id,i.supplier_id,s.name supplier_name,i.recipient,i.status,
            i.sent_at,i.expires_at,i.submitted_at,i.quote_id,i.error
            FROM supplier_rfq_invites i JOIN suppliers s ON s.id=i.supplier_id
            WHERE i.request_id=? ORDER BY i.created_at DESC,i.id DESC""",(request_id,)).fetchall()
        quotes=db.execute("""SELECT q.id quote_id,q.supplier_id,s.name supplier_name,q.unit_price,
            q.freight,q.tax_rate,q.lead_days,q.payment_days,q.created_at,q.technical_status
            FROM quotes q JOIN suppliers s ON s.id=q.supplier_id
            WHERE q.request_id=? ORDER BY q.created_at DESC,q.id DESC""",(request_id,)).fetchall()
    return {"request":dict(req),"invites":[dict(row) for row in invites],
        "quotes":[dict(row) for row in quotes],
        "summary":{"invited":len(invites),
            "sent":sum(1 for row in invites if row["status"]=="sent"),
            "submitted":sum(1 for row in invites if row["status"]=="submitted"),
            "failed":sum(1 for row in invites if row["status"]=="failed"),
            "quotation_count":len(quotes)}}

@router.post("/api/rfq/dispatch/{request_id}")
def admin_dispatch_rfq(request_id:int,request:Request):
    result=dispatch_request_rfq(request_id,getattr(request.state,"auth_user","ProcureAI"))
    return {"success":True,**result}

PORTAL_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Supplier quotation | ProcureAI</title><style>*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#172b4d;font:15px Arial,sans-serif}.wrap{max-width:760px;margin:36px auto;padding:16px}.card{background:#fff;border:1px solid #e1e7f0;border-radius:16px;overflow:hidden;box-shadow:0 12px 40px #172b4d12}.head{background:#172b4d;color:#fff;padding:26px}.head b{color:#f5a623;letter-spacing:1px}.body{padding:26px}.summary{background:#f7f9fc;border:1px solid #e5eaf1;border-radius:10px;padding:16px;margin:16px 0}.summary p{margin:7px 0;color:#53647a}.project-brief{border-left:4px solid #f5a623}.project-brief h2{font-size:18px;margin:0 0 10px}.project-brief .budget{font-size:24px;font-weight:800;color:#172b4d;margin:8px 0}.project-brief p{line-height:1.55;white-space:pre-wrap}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:block;font-weight:700;font-size:13px;color:#344256;margin:12px 0}input,textarea{width:100%;padding:12px;border:1px solid #d6deea;border-radius:8px;margin-top:7px;font:inherit}textarea{min-height:90px}button{border:0;border-radius:9px;padding:14px 20px;background:#2457c5;color:#fff;font-weight:700;font-size:15px;cursor:pointer}button:disabled{opacity:.55}.muted{color:#64748b;line-height:1.6}.notice{padding:13px;border-radius:8px;background:#fff8e8;color:#79520c;margin:16px 0}.status{padding:16px;border-radius:9px;background:#edf8f1;color:#176b3b;margin:16px 0}@media(max-width:560px){.wrap{margin:10px auto}.body{padding:18px}.grid{grid-template-columns:1fr}}</style></head><body><main class="wrap"><section class="card"><header class="head"><b>PROCUREAI · SUPPLIER PORTAL</b><h1>Submit your quotation</h1><p>Secure quotation submission</p></header><div class="body"><div id="loading" class="muted">Loading your invitation…</div><div id="content" hidden><div id="summary" class="summary"></div><section id="projectBrief" class="summary" hidden></section><p class="muted">Enter your commercial offer below. Your submission is linked to this invitation and cannot be submitted twice.</p><form id="form"><div class="grid"><label>Unit price (₦) *<input name="unit_price" type="number" min=".01" step=".01" required></label><label>Freight / delivery (₦)<input name="freight" type="number" min="0" step=".01" value="0"></label><label>VAT / tax (%)<input name="tax_rate" type="number" min="0" max="100" step=".01" value="7.5"></label><label>Delivery lead time (days) *<input name="lead_days" type="number" min="0" required></label><label>Payment period (days)<input name="payment_days" type="number" min="0" value="30"></label><label>Quotation validity (days)<input name="validity_days" type="number" min="1" value="30"></label></div><label>Warranty / after-sales support<textarea name="warranty" maxlength="500"></textarea></label><label>Technical response and compliance details<textarea name="technical_response" maxlength="8000" placeholder="State model, make, ratings, standards and how the offer meets the specification."></textarea></label><label>Exclusions or deviations<textarea name="exclusions" maxlength="4000"></textarea></label><label>Additional commercial notes<textarea name="note" maxlength="4000"></textarea></label><div class="notice">Submitting a quotation is not an award or purchase order. ProcureAI's buyer will review your offer.</div><p id="error" role="alert" style="color:#b42318"></p><button id="submit" type="submit">Submit quotation</button></form></div><div id="done" hidden class="status"></div></div></section></main><script>const token="__TOKEN__";const $=id=>document.getElementById(id);function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]))}async function init(){try{const r=await fetch("/api/supplier-portal/"+encodeURIComponent(token));const d=await r.json();if(!r.ok)throw Error(d.detail||"Invitation unavailable");$("loading").hidden=true;if(d.status==="submitted"){$("done").hidden=false;$("done").textContent=d.message;return}$("content").hidden=false;const q=d.request;$("summary").innerHTML="<b>"+esc(q.code)+" · "+esc(q.material)+"</b><p>Category: "+esc(q.category)+"</p><p>Required quantity: "+esc(q.quantity)+" "+esc(q.unit)+"</p><p>Delivery location: "+esc(q.site)+"</p><p>Required by: "+esc(q.required_by)+"</p><p>Technical requirements: "+esc(q.additional_information||"No additional requirements supplied.")+"</p>"const p=d.project;if(p){const brief=$("projectBrief");brief.hidden=false;brief.classList.add("project-brief");const money=p.budget>0?"₦"+Number(p.budget).toLocaleString("en-NG",{minimumFractionDigits:2,maximumFractionDigits:2}):"Not specified";const deliverables=Array.isArray(p.deliverables)&&p.deliverables.length?p.deliverables.join(" • "):"Not specified";brief.innerHTML="<h2>Project brief</h2><p><b>"+esc(p.title||"Project")+"</b></p><p>Client / organisation: "+esc(p.client||p.agency_details||"Not specified")+"</p><div class='budget'>"+esc(money)+"</div><p><b>Overall project budget</b></p><p>"+esc(p.summary||"Project summary not supplied.")+"</p><p>Category: "+esc(p.category||"Not specified")+"</p><p>Location: "+esc(p.location||"Not specified")+"</p><p>Timeline: "+esc(p.start_date||"Not specified")+" to "+esc(p.due_date||"Not specified")+"</p><p>Key deliverables: "+esc(deliverables)+"</p><p class='muted'>The overall budget covers the entire project. It is not necessarily the amount allocated to this individual purchase request. Please submit a competitive and realistic quotation.</p>"}}}catch(e){$("loading").textContent=e.message}}$("form").addEventListener("submit",async e=>{e.preventDefault();$("submit").disabled=true;$("error").textContent="";const d=Object.fromEntries(new FormData(e.target).entries());for(const k of ["unit_price","freight","tax_rate","lead_days","payment_days","validity_days"])d[k]=Number(d[k]||0);try{const r=await fetch("/api/supplier-portal/"+encodeURIComponent(token),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)});const out=await r.json();if(!r.ok)throw Error(out.detail||"Submission failed");$("content").hidden=true;$("done").hidden=false;$("done").textContent=out.message}catch(err){$("error").textContent=err.message;$("submit").disabled=false}});init();</script></body></html>"""
