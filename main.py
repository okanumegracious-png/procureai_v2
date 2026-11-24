"""Unified ProcureAI API.

Dashboard, BOQ extraction and agentic intake share one SQLite database.
External supplier contact and purchase commitments are deliberately gated.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional

import pypdf
import io
import base64
import hashlib
import hmac
import re
import secrets
import time
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, Security, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import app as dashboard_module
from procureai_store import connect, init_store
from agentic.intake import IntakeAgent, SQLiteRequestStore

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
EXPECTED_API_KEY = os.getenv("PROCUREAI_WEBHOOK_SECRET", "").strip()
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="ProcureAI Unified API", version="3.0.0")
allowed_origins = [v.strip().rstrip("/") for v in os.getenv("FRONTEND_URL", "http://localhost:3000,http://127.0.0.1:3000").split(",") if v.strip()]
VERCEL_ORIGIN_RE = r"https://[a-z0-9-]+\.vercel\.app"
SESSION_COOKIE = "procureai_session"
CSRF_COOKIE = "procureai_csrf"
SESSION_TTL_SECONDS = 12 * 60 * 60
COOKIE_SECURE = os.getenv("PROCUREAI_COOKIE_SECURE", "true").lower() == "true"
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_origin_regex=VERCEL_ORIGIN_RE,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", API_KEY_NAME, "X-CSRF-Token"],
)

def _init_auth_store():
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
            token_hash TEXT PRIMARY KEY, username TEXT NOT NULL, csrf_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL, created_at TEXT NOT NULL
        )""")
        db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (int(time.time()),))
        db.execute("""CREATE TABLE IF NOT EXISTS auth_login_attempts (
            ip_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_started INTEGER NOT NULL
        )""")

def _password_matches(password: str) -> bool:
    encoded = os.getenv("PROCUREAI_ADMIN_PASSWORD_HASH", "")
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _session_record(token: str):
    if not token:
        return None
    with connect() as db:
        return db.execute(
            "SELECT username,csrf_hash,expires_at FROM auth_sessions WHERE token_hash=? AND expires_at>?",
            (_token_hash(token), int(time.time())),
        ).fetchone()

def _origin_allowed(origin: str | None) -> bool:
    if not origin:
        return True
    normalized = origin.rstrip("/")
    return normalized in allowed_origins or bool(re.fullmatch(VERCEL_ORIGIN_RE, normalized))

@app.middleware("http")
async def require_browser_session(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/v1/") and path != "/api/auth/login" and request.method != "OPTIONS":
        token = request.cookies.get(SESSION_COOKIE, "")
        session = _session_record(token)
        if not session:
            return JSONResponse({"detail": "Authentication required"}, status_code=401, headers={"Cache-Control": "no-store"})
        request.state.auth_user = session["username"]
        if request.method not in ("GET", "HEAD"):
            csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
            csrf_header = request.headers.get("X-CSRF-Token", "")
            if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403, headers={"Cache-Control": "no-store"})
            supplied_hash = hashlib.sha256(csrf_header.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(supplied_hash, session["csrf_hash"]):
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403, headers={"Cache-Control": "no-store"})
    elif path == "/api/auth/login" and request.method == "POST":
        if not _origin_allowed(request.headers.get("origin")):
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    if not EXPECTED_API_KEY:
        raise HTTPException(status_code=503, detail="Server authentication is not configured")
    if not api_key or api_key != EXPECTED_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return True


@app.on_event("startup")
def startup():
    dashboard_module.init_db()
    init_store()
    _init_auth_store()


class BOQItem(BaseModel):
    item_code: str = ""
    item_name: str = Field(min_length=1)
    specification: str = ""
    quantity: float = Field(ge=0)
    unit: str = ""
    category: str = "General"


class BOQExtractionResponse(BaseModel):
    success: bool
    project_id: str
    project_summary: str
    items: List[BOQItem]


class SupplierCreate(BaseModel):
    supplier_id: Optional[str] = None
    name: str = Field(min_length=2, max_length=200)
    email: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    categories: List[str] = Field(default_factory=list)


class SupplierRequest(BaseModel):
    project_id: str
    categories: List[str]
    dispatch_rfq: bool = False

class SupplierVerificationPayload(BaseModel):
    status: str = Field(pattern="^(verified|rejected|pending)$")
    notes: str = ""


class QuotationLineItem(BaseModel):
    item_code: str
    unit_price: float = Field(gt=0)
    total_price: float = Field(ge=0)


class QuotationSubmission(BaseModel):
    project_id: str
    supplier_id: str
    delivery_days: int = Field(ge=0)
    currency: str = "NGN"
    line_items: List[QuotationLineItem] = Field(min_length=1)


class RecommendationRequest(BaseModel):
    project_id: str
    weights: Dict[str, float] = {"price": 0.6, "delivery": 0.2, "rating": 0.2}


class AwardApprovalRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    decided_by: str = Field(min_length=2, max_length=160)
    note: str = ""


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _extract_pdf_text(raw: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(raw))
    return "\n".join(page.extract_text() or "" for page in reader.pages).strip()


def _parse_boq(raw_text: str) -> dict:
    if not raw_text.strip():
        raise HTTPException(status_code=422, detail="No readable text found in the uploaded document")
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="BOQ extraction is not configured. Set OPENAI_API_KEY; no sample items will be substituted.")
    from openai import OpenAI
    from pydantic import create_model
    class StructuredBOQ(BaseModel):
        project_summary: str
        items: List[BOQItem]
    client = OpenAI(api_key=key)
    result = client.beta.chat.completions.parse(
        model=os.getenv("PROCUREAI_BOQ_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "Extract every BOQ line item from the supplied text. Preserve source wording, item codes, specifications, quantities and units. Never invent missing values. Use an empty string for missing text and zero only when quantity is explicitly absent. Do not omit line items."},
            {"role": "user", "content": raw_text[:100000]},
        ],
        response_format=StructuredBOQ,
    )
    parsed = result.choices[0].message.parsed
    if parsed is None:
        raise HTTPException(status_code=422, detail="The document could not be converted into structured BOQ data")
    return parsed.model_dump()


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)

@app.post("/api/auth/login")
def login(payload: LoginPayload, request: Request, response: Response):
    configured_username = os.getenv("PROCUREAI_ADMIN_USERNAME", "").strip()
    if not configured_username or not os.getenv("PROCUREAI_ADMIN_PASSWORD_HASH"):
        raise HTTPException(status_code=503, detail="Administrator login is not configured")
    client_ip = request.client.host if request.client else "unknown"
    ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest()
    now_epoch = int(time.time())
    with connect() as db:
        attempt = db.execute("SELECT attempts,window_started FROM auth_login_attempts WHERE ip_hash=?", (ip_hash,)).fetchone()
        if attempt and now_epoch - attempt["window_started"] < 900 and attempt["attempts"] >= 5:
            raise HTTPException(status_code=429, detail="Too many sign-in attempts. Wait 15 minutes and try again.")
        if not attempt or now_epoch - attempt["window_started"] >= 900:
            db.execute("INSERT INTO auth_login_attempts(ip_hash,attempts,window_started) VALUES(?,0,?) ON CONFLICT(ip_hash) DO UPDATE SET attempts=0,window_started=excluded.window_started", (ip_hash, now_epoch))
    if not hmac.compare_digest(payload.username, configured_username) or not _password_matches(payload.password):
        with connect() as db:
            db.execute("INSERT INTO auth_login_attempts(ip_hash,attempts,window_started) VALUES(?,1,?) ON CONFLICT(ip_hash) DO UPDATE SET attempts=attempts+1", (ip_hash, now_epoch))
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    with connect() as db:
        db.execute("DELETE FROM auth_login_attempts WHERE ip_hash=?", (ip_hash,))
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    expires = int(time.time()) + SESSION_TTL_SECONDS
    with connect() as db:
        db.execute("INSERT INTO auth_sessions(token_hash,username,csrf_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
                   (_token_hash(token), configured_username, hashlib.sha256(csrf.encode()).hexdigest(), expires, _now()))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_SECONDS, httponly=True,
                        secure=COOKIE_SECURE, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=SESSION_TTL_SECONDS, httponly=False,
                        secure=COOKIE_SECURE, samesite="lax", path="/")
    return {"success": True, "username": configured_username, "expires_in": SESSION_TTL_SECONDS}

@app.get("/api/auth/me")
def auth_me(request: Request):
    return {"authenticated": True, "username": getattr(request.state, "auth_user", "")}

@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=COOKIE_SECURE, httponly=False, samesite="lax")
    return {"success": True}

@app.get("/health")
def health():
    return {"status": "ok", "service": "ProcureAI Unified API"}


@app.post("/api/v1/workflows/upload-boq", response_model=BOQExtractionResponse, dependencies=[Depends(verify_api_key)])
async def upload_boq(project_id: str = Form(...), file: UploadFile = File(...)):
    if not project_id.strip():
        raise HTTPException(status_code=422, detail="project_id is required")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="BOQ file exceeds the 15 MB limit")
    filename = (file.filename or "").lower()
    if filename.endswith(".pdf") or file.content_type == "application/pdf":
        text = _extract_pdf_text(raw)
    elif filename.endswith((".txt", ".csv")) or file.content_type in ("text/plain", "text/csv"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        raise HTTPException(status_code=415, detail="Upload a text-based PDF, TXT or CSV BOQ file")
    extracted = _parse_boq(text)
    now = _now()
    with connect() as db:
        db.execute("""INSERT INTO workflow_projects(project_id,summary,status,source_filename,created_at,updated_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
            summary=excluded.summary,status='boq_extracted',source_filename=excluded.source_filename,updated_at=excluded.updated_at""",
            (project_id, extracted["project_summary"], "boq_extracted", file.filename, now, now))
        db.execute("DELETE FROM workflow_boq_items WHERE project_id=?", (project_id,))
        for item in extracted["items"]:
            db.execute("""INSERT INTO workflow_boq_items(project_id,item_code,item_name,specification,quantity,unit,category,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, item.get("item_code",""), item["item_name"], item.get("specification",""),
                 item["quantity"], item.get("unit",""), item.get("category","General"), now))
    return {"success": True, "project_id": project_id, "project_summary": extracted["project_summary"], "items": extracted["items"]}


@app.get("/api/v1/workflows/projects/{project_id}/boq", dependencies=[Depends(verify_api_key)])
def get_project_boq(project_id: str):
    with connect() as db:
        project = db.execute("SELECT * FROM workflow_projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        items = db.execute("SELECT item_code,item_name,specification,quantity,unit,category,source_page FROM workflow_boq_items WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    return {"project": dict(project), "items": [dict(row) for row in items]}


@app.post("/api/v1/suppliers", dependencies=[Depends(verify_api_key)])
def add_supplier(payload: SupplierCreate):
    now = _now()
    sid = payload.supplier_id or "sup_" + uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("""INSERT INTO workflow_suppliers
            (supplier_id,name,email,phone,website,address,categories_json,verification_status,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'unverified',?,?)""",
            (sid,payload.name,payload.email,payload.phone,payload.website,payload.address,json.dumps(payload.categories),now,now))
    return {"success": True, "supplier_id": sid, "verification_status": "unverified"}


@app.post("/api/v1/suppliers/{supplier_id}/verification", dependencies=[Depends(verify_api_key)])
def set_supplier_verification(supplier_id: str, payload: SupplierVerificationPayload):
    with connect() as db:
        cur = db.execute("""UPDATE workflow_suppliers SET verification_status=?,verification_notes=?,updated_at=?
            WHERE supplier_id=?""", (payload.status,payload.notes,_now(),supplier_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="Supplier not found")
        row = db.execute("SELECT supplier_id,name,verification_status,verification_notes FROM workflow_suppliers WHERE supplier_id=?", (supplier_id,)).fetchone()
    return {"success":True,"supplier":dict(row)}


@app.get("/api/v1/suppliers", dependencies=[Depends(verify_api_key)])
def list_suppliers():
    with connect() as db:
        records = db.execute("SELECT * FROM workflow_suppliers ORDER BY name").fetchall()
    return {"suppliers": [{**dict(row), "categories": json.loads(row["categories_json"])} for row in records]}


@app.post("/api/v1/workflows/request-suppliers", dependencies=[Depends(verify_api_key)])
def request_suppliers(payload: SupplierRequest):
    with connect() as db:
        project = db.execute("SELECT project_id FROM workflow_projects WHERE project_id=?", (payload.project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        rows = db.execute("SELECT * FROM workflow_suppliers WHERE verification_status='verified' ORDER BY name").fetchall()
    wanted = {c.strip().casefold() for c in payload.categories}
    shortlist = []
    for row in rows:
        categories = json.loads(row["categories_json"])
        if not wanted or any(c.casefold() in wanted for c in categories):
            shortlist.append({"supplier_id": row["supplier_id"], "name": row["name"], "email": row["email"], "categories": categories, "verification_status": row["verification_status"]})
    if payload.dispatch_rfq:
        raise HTTPException(status_code=409, detail="RFQ dispatch is not enabled. Connect and configure an approved email provider before sending supplier messages.")
    return {"success": True, "project_id": payload.project_id, "shortlisted_suppliers": shortlist, "rfq_dispatch": [], "note": "Only manually verified supplier records are included."}


@app.post("/api/v1/workflows/upload-quotation", dependencies=[Depends(verify_api_key)])
def upload_quotation(payload: QuotationSubmission):
    total = round(sum(item.total_price for item in payload.line_items), 2)
    qid, now = "qt_" + uuid.uuid4().hex[:12], _now()
    with connect() as db:
        if not db.execute("SELECT 1 FROM workflow_projects WHERE project_id=?", (payload.project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
        supplier = db.execute("SELECT verification_status FROM workflow_suppliers WHERE supplier_id=?", (payload.supplier_id,)).fetchone()
        if not supplier or supplier["verification_status"] != "verified":
            raise HTTPException(status_code=409, detail="Quotation supplier must exist and be verified")
        db.execute("""INSERT INTO workflow_quotations
            (quotation_id,project_id,supplier_id,currency,delivery_days,line_items_json,total_amount,status,created_at)
            VALUES(?,?,?,?,?,?,?,'submitted',?)""",
            (qid,payload.project_id,payload.supplier_id,payload.currency,json.dumps([i.model_dump() for i in payload.line_items]),payload.delivery_days,total,now))
    return {"success": True, "quotation_id": qid, "total_amount": total, "currency": payload.currency}


@app.get("/api/v1/workflows/compare-offers/{project_id}", dependencies=[Depends(verify_api_key)])
def compare_offers(project_id: str):
    with connect() as db:
        rows = db.execute("""SELECT q.*,s.name supplier_name,s.verification_status
            FROM workflow_quotations q JOIN workflow_suppliers s ON s.supplier_id=q.supplier_id
            WHERE q.project_id=? ORDER BY q.total_amount""", (project_id,)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="No quotations found for this project")
    offers = [dict(row) for row in rows]
    low = min(row["total_amount"] for row in offers)
    return {"success": True, "project_id": project_id, "comparison_report": [
        {"quotation_id":q["quotation_id"],"supplier_id":q["supplier_id"],"supplier_name":q["supplier_name"],
         "verification_status":q["verification_status"],"total_amount":q["total_amount"],"currency":q["currency"],
         "delivery_days":q["delivery_days"],"variance_from_lowest":round(q["total_amount"]-low,2),
         "line_items":json.loads(q["line_items_json"])} for q in offers]}


@app.post("/api/v1/workflows/recommend-supplier", dependencies=[Depends(verify_api_key)])
def recommend_supplier(payload: RecommendationRequest):
    comparison = compare_offers(payload.project_id)
    offers = comparison["comparison_report"]
    min_price = min(q["total_amount"] for q in offers)
    min_days = min(q["delivery_days"] for q in offers)
    ranked = []
    for q in offers:
        score = (min_price / q["total_amount"] * 100 if q["total_amount"] else 0) * payload.weights.get("price",0.6)
        score += (min_days / max(q["delivery_days"],1) * 100) * payload.weights.get("delivery",0.2)
        ranked.append({**q,"weighted_score":round(score,2)})
    ranked.sort(key=lambda x:x["weighted_score"], reverse=True)
    return {"success":True,"recommended":ranked[0],"ranking":ranked,
            "disclaimer":"Decision support only. Verify quote validity, scope, tax, freight and supplier capacity. A human approval is required before award."}


@app.post("/api/v1/workflows/approve-award/{quotation_id}", dependencies=[Depends(verify_api_key)])
def create_award_proposal(quotation_id: str, requested_by: str = Form(...)):
    now, po = _now(), "PO-" + datetime.utcnow().strftime("%Y") + "-" + uuid.uuid4().hex[:8].upper()
    with connect() as db:
        quote = db.execute("SELECT * FROM workflow_quotations WHERE quotation_id=?", (quotation_id,)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        db.execute("""INSERT INTO workflow_purchase_orders
            (po_number,project_id,quotation_id,supplier_id,total_amount,currency,status,requested_by,created_at)
            VALUES(?,?,?,?,?,?,'pending_approval',?,?)""",
            (po,quote["project_id"],quotation_id,quote["supplier_id"],quote["total_amount"],quote["currency"],requested_by,now))
    return {"success":True,"purchase_order":{"po_number":po,"status":"pending_approval","total_amount":quote["total_amount"],"currency":quote["currency"]}}


@app.post("/api/v1/workflows/purchase-orders/{po_number}/decision", dependencies=[Depends(verify_api_key)])
def decide_award(po_number: str, payload: AwardApprovalRequest):
    now = _now()
    status = "approved" if payload.action == "approve" else "rejected"
    with connect() as db:
        cur = db.execute("""UPDATE workflow_purchase_orders SET status=?,decided_by=?,decision_note=?,decided_at=?
            WHERE po_number=? AND status='pending_approval'""",
            (status,payload.decided_by,payload.note,now,po_number))
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="Purchase order is not awaiting approval")
        row = db.execute("SELECT * FROM workflow_purchase_orders WHERE po_number=?", (po_number,)).fetchone()
    return {"success":True,"purchase_order":dict(row)}


# Agentic intake is mounted on the same database path as the dashboard and BOQ workflow.
intake_store = SQLiteRequestStore(os.getenv("PROCUREAI_DB_PATH", str(BASE_DIR / "procureai.db")))
intake_agent = IntakeAgent(intake_store)


class IntakePayload(BaseModel):
    description: str = Field(min_length=1, max_length=10000)


@app.post("/api/intake")
def create_intake(payload: IntakePayload):
    try:
        result = intake_agent.process(payload.description)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    request = result.request
    return {"request_id":result.request_id,"status":result.status,
            "request":{"description":request.description,"item":request.item,
            "quantity":str(request.quantity) if request.quantity is not None else None,
            "unit":request.unit,"budget":str(request.budget) if request.budget is not None else None,
            "currency":request.currency,"delivery_location":request.delivery_location,
            "required_within_days":request.required_within_days,"category":request.category},
            "missing_fields":result.missing_fields,"next_actions":result.next_actions,
            "sourcing_ready":result.sourcing_ready}


@app.get("/api/agentic/requests")
def list_intake_requests():
    return {"requests":intake_store.list()}


class DashboardRequestPayload(BaseModel):
    material: str
    category: str
    quantity: float = Field(gt=0)
    unit: str
    site: str
    required_by: str
    priority: str = "Normal"


class DashboardQuotePayload(BaseModel):
    supplier_id: int
    request_id: int
    unit_price: float = Field(gt=0)
    freight: float = Field(ge=0)
    tax_rate: float = Field(ge=0, le=100)
    lead_days: int = Field(ge=0)
    payment_days: int = Field(ge=0)
    note: str = ""


class DashboardOrderPayload(BaseModel):
    request_id: int
    supplier_id: int


class DashboardDecisionPayload(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    note: str = ""


@app.get("/api/requests")
def dashboard_requests():
    return {"requests": dashboard_module.rows("SELECT * FROM requests ORDER BY id DESC")}


@app.get("/api/suppliers")
def dashboard_suppliers():
    return {"suppliers": dashboard_module.rows("SELECT * FROM suppliers ORDER BY rating DESC")}


@app.get("/api/orders")
def dashboard_orders():
    return {"orders": dashboard_module.dashboard()["orders"]}


@app.post("/api/requests", status_code=201)
def dashboard_create_request(payload: DashboardRequestPayload):
    try:
        date.fromisoformat(payload.required_by)
    except ValueError:
        raise HTTPException(status_code=422, detail="required_by must be an ISO date")
    with dashboard_module.connect() as db:
        next_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM requests").fetchone()[0]
        code = f"PR-{next_id+141:04d}"
        cur = db.execute("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,priority,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (code,payload.material.strip(),payload.category,payload.quantity,payload.unit,payload.site.strip(),
             payload.required_by,payload.priority,"Open",datetime.utcnow().isoformat()))
    return {"success":True,"id":cur.lastrowid,"code":code}


@app.post("/api/quotes", status_code=201)
def dashboard_create_quote(payload: DashboardQuotePayload):
    with dashboard_module.connect() as db:
        if not db.execute("SELECT 1 FROM requests WHERE id=?", (payload.request_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Purchase request not found")
        if not db.execute("SELECT 1 FROM suppliers WHERE id=?", (payload.supplier_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Supplier not found")
        cur = db.execute("""INSERT INTO quotes(supplier_id,request_id,unit_price,freight,tax_rate,lead_days,payment_days,note,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (payload.supplier_id,payload.request_id,payload.unit_price,payload.freight,payload.tax_rate,
             payload.lead_days,payload.payment_days,payload.note,datetime.utcnow().isoformat()))
    return {"success":True,"id":cur.lastrowid}


@app.post("/api/orders", status_code=201)
def dashboard_create_order(payload: DashboardOrderPayload):
    req = dashboard_module.one("SELECT * FROM requests WHERE id=?", (payload.request_id,))
    quote = dashboard_module.one("SELECT * FROM quotes WHERE request_id=? AND supplier_id=? ORDER BY id DESC LIMIT 1",
                                 (payload.request_id,payload.supplier_id))
    supplier = dashboard_module.one("SELECT * FROM suppliers WHERE id=?", (payload.supplier_id,))
    if not req or not quote or not supplier:
        raise HTTPException(status_code=404, detail="A matching request, supplier and quotation are required")
    qty, price = Decimal(str(req["quantity"])), Decimal(str(quote["unit_price"]))
    tax = qty * price * Decimal(str(quote["tax_rate"])) / Decimal("100")
    freight = Decimal(str(quote["freight"]))
    total = qty * price + tax + freight
    reasons = ["Human approval required before any purchase commitment"]
    if total > Decimal("2500000"): reasons.append("Order exceeds the configured review threshold")
    if supplier["rating"] < 3.5: reasons.append("Supplier rating is below the preferred threshold")
    if quote["lead_days"] > max(0,(date.fromisoformat(req["required_by"])-date.today()).days):
        reasons.append("Quoted lead time misses the requested delivery date")
    with dashboard_module.connect() as db:
        count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        code = f"PO-{datetime.utcnow():%y%m%d}-{count+1:03d}"
        cur = db.execute("""INSERT INTO orders(code,supplier_id,request_id,quantity,unit_price,freight,tax,total,status,required_by,created_at,decision_note)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code,payload.supplier_id,payload.request_id,float(qty),float(price),float(freight),float(tax),
             float(total),"Pending approval",req["required_by"],datetime.utcnow().isoformat(),"; ".join(reasons)))
    return {"success":True,"id":cur.lastrowid,"code":code,"status":"Pending approval","total":float(total),"reasons":reasons}


@app.post("/api/orders/{order_id}/decision")
def dashboard_decide_order(order_id: int, payload: DashboardDecisionPayload):
    status = "Approved" if payload.action == "approve" else "Rejected"
    with dashboard_module.connect() as db:
        cur = db.execute("UPDATE orders SET status=?,decision_note=? WHERE id=? AND status='Pending approval'",
                         (status,payload.note,order_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="Order is not awaiting approval")
        if payload.action == "approve":
            db.execute("UPDATE requests SET status='Awarded' WHERE id=(SELECT request_id FROM orders WHERE id=?)",(order_id,))
    return {"success":True,"status":status}


# Reuse the existing dashboard database and calculations; the browser UI is served by this API.
@app.get("/api/dashboard")
def get_dashboard():
    return dashboard_module.dashboard()


@app.get("/api/analysis")
def get_dashboard_analysis(request_id: int):
    try:
        return dashboard_module.analyse_request(request_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/")
def index():
    from fastapi.responses import FileResponse
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
