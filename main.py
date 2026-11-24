"""Unified ProcureAI API.

Dashboard, BOQ extraction and agentic intake share one SQLite database.
External supplier contact and purchase commitments are deliberately gated.
"""
from __future__ import annotations

import json
import html
import os
import uuid
import smtplib
import ssl
import urllib.request
import urllib.error
import urllib.parse
from email.message import EmailMessage
from email.utils import parseaddr
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
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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

def _ensure_supplier_request_email_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS supplier_request_emails (id INTEGER PRIMARY KEY AUTOINCREMENT, quote_id INTEGER NOT NULL UNIQUE, supplier_id INTEGER NOT NULL, request_id INTEGER NOT NULL, recipient TEXT NOT NULL, sent_by TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL)""")

def _send_supplier_request_email(quote):
    host = os.getenv("SMTP_HOST", "").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.getenv("SMTP_FROM_EMAIL", username).strip()
    recipient = (quote["supplier_email"] or "").strip()
    parsed = parseaddr(recipient)[1]
    if not parsed or parsed != recipient or "@" not in parsed:
        raise HTTPException(status_code=409, detail="This seller has no valid email address. Update the seller record before sending the request.")

    message = EmailMessage()
    message["Subject"] = f"Request for Quotation (RFQ) {quote['request_code']} – {quote['material']}"
    message["To"] = recipient

    # Build both a readable plain-text fallback and a styled HTML email.
    supplier_name = str(quote["supplier_name"] or "Supplier")
    request_code = str(quote["request_code"] or "—")
    material = str(quote["material"] or "—")
    category = str(quote["category"] or "—")
    quantity = f"{quote['quantity']} {quote['unit'] or ''}".strip()
    site = str(quote["site"] or "To be confirmed")
    required_by = str(quote["required_by"] or "To be confirmed")
    esc = lambda value: html.escape(str(value), quote=True)

    body_text = "\n".join([
        f"Dear {supplier_name},",
        "",
        "Gestro Global Consult Ltd invites you to review the procurement requirement below and submit or confirm your quotation.",
        "",
        "REQUEST SUMMARY",
        f"RFQ reference: {request_code}",
        f"Item / material: {material}",
        f"Category: {category}",
        f"Required quantity: {quantity}",
        f"Delivery location: {site}",
        f"Required delivery date: {required_by}",
        "",
        "Please reply with your unit price, total price, applicable taxes and charges, delivery lead time, payment terms, and quotation validity. Clearly state any exclusions or deviations from the requirement.",
        "",
        "IMPORTANT: This RFQ is an invitation to submit or confirm a quotation only. It is not a purchase order, award notice, contract, or authority to commence work or supply goods.",
        "",
        "Kind regards,",
        "Procurement Team",
        "Gestro Global Consult Ltd",
    ])
    html_body = f"""<!doctype html>
<html lang="en">
  <body style="margin:0;padding:24px 12px;background:#f3f5f8;font-family:Arial,Helvetica,sans-serif;color:#243044;">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:680px;margin:0 auto;background:#ffffff;border:1px solid #e3e8ef;border-radius:10px;overflow:hidden;">
      <tr><td style="padding:24px 30px;background:#172b4d;color:#ffffff;">
        <div style="font-size:12px;letter-spacing:1.5px;font-weight:bold;color:#f5a623;">GESTRO GLOBAL CONSULT LTD</div>
        <div style="font-size:22px;line-height:1.3;font-weight:bold;margin-top:8px;">Request for Quotation</div>
        <div style="font-size:13px;color:#dce5f2;margin-top:6px;">Supplier procurement enquiry</div>
      </td></tr>
      <tr><td style="padding:28px 30px 12px;">
        <p style="margin:0 0 14px;font-size:15px;">Dear <strong>{esc(supplier_name)}</strong>,</p>
        <p style="margin:0 0 20px;font-size:14px;line-height:1.7;color:#526174;">Gestro Global Consult Ltd invites you to review the procurement requirement below and submit or confirm your quotation.</p>
        <div style="font-size:12px;font-weight:bold;letter-spacing:1px;color:#64748b;margin:0 0 10px;">REQUEST SUMMARY</div>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;font-size:14px;border:1px solid #e2e8f0;">
          <tr style="background:#f7f9fc;"><td style="padding:11px 13px;width:38%;color:#64748b;border-bottom:1px solid #e2e8f0;">RFQ reference</td><td style="padding:11px 13px;font-weight:bold;color:#172b4d;border-bottom:1px solid #e2e8f0;">{esc(request_code)}</td></tr>
          <tr><td style="padding:11px 13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Item / material</td><td style="padding:11px 13px;font-weight:bold;border-bottom:1px solid #e2e8f0;">{esc(material)}</td></tr>
          <tr style="background:#f7f9fc;"><td style="padding:11px 13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Category</td><td style="padding:11px 13px;border-bottom:1px solid #e2e8f0;">{esc(category)}</td></tr>
          <tr><td style="padding:11px 13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Required quantity</td><td style="padding:11px 13px;font-weight:bold;border-bottom:1px solid #e2e8f0;">{esc(quantity)}</td></tr>
          <tr style="background:#f7f9fc;"><td style="padding:11px 13px;color:#64748b;border-bottom:1px solid #e2e8f0;">Delivery location</td><td style="padding:11px 13px;border-bottom:1px solid #e2e8f0;">{esc(site)}</td></tr>
          <tr><td style="padding:11px 13px;color:#64748b;">Required delivery date</td><td style="padding:11px 13px;">{esc(required_by)}</td></tr>
        </table>
        <p style="margin:22px 0 8px;font-size:14px;font-weight:bold;color:#172b4d;">Quotation submission</p>
        <p style="margin:0;font-size:14px;line-height:1.7;color:#526174;">Please reply with your unit price, total price, applicable taxes and charges, delivery lead time, payment terms, and quotation validity. Clearly state any exclusions or deviations from the requirement.</p>
        <div style="margin:22px 0 8px;padding:14px 16px;background:#fff8e8;border-left:4px solid #e5a12a;border-radius:4px;">
          <div style="font-size:12px;font-weight:bold;letter-spacing:.5px;color:#8a5a08;margin-bottom:5px;">IMPORTANT PROCUREMENT NOTICE</div>
          <div style="font-size:13px;line-height:1.6;color:#624b22;">This RFQ is an invitation to submit or confirm a quotation only. It is not a purchase order, award notice, contract, or authority to commence work or supply goods.</div>
        </div>
        <p style="margin:22px 0 4px;font-size:14px;color:#526174;">Kind regards,</p>
        <p style="margin:0 0 24px;font-size:14px;line-height:1.5;font-weight:bold;color:#172b4d;">Procurement Team<br>Gestro Global Consult Ltd</p>
      </td></tr>
      <tr><td style="padding:14px 30px;background:#f7f9fc;border-top:1px solid #e8edf3;font-size:11px;line-height:1.5;color:#8793a3;">This message was generated by ProcureAI for procurement communication. Please reply directly to this email with your quotation or clarification.</td></tr>
    </table>
  </body>
</html>"""
    message.set_content(body_text)
    message.add_alternative(html_body, subtype="html")

    # Gmail API uses HTTPS, avoiding Railway's restricted outbound SMTP ports.
    if os.getenv("EMAIL_PROVIDER", "").strip().lower() == "gmail":
        client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
        client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
        refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
        gmail_sender = os.getenv("GMAIL_SENDER_EMAIL", "").strip()
        if not all((client_id, client_secret, refresh_token, gmail_sender)):
            raise HTTPException(status_code=503, detail="Gmail delivery is selected but its OAuth credentials are incomplete. Configure GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN and GMAIL_SENDER_EMAIL in Railway.")
        message["From"] = gmail_sender
        token_payload = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        token_request = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        stage = "OAuth refresh-token exchange"
        try:
            with urllib.request.urlopen(token_request, timeout=25) as response:
                token_data = json.loads(response.read().decode("utf-8"))
            access_token = token_data.get("access_token", "")
            if not access_token:
                raise HTTPException(status_code=502, detail="Google OAuth token refresh succeeded without an access token. Reconnect the Gmail account.")
            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
            gmail_payload = json.dumps({"raw": raw_message}).encode("utf-8")
            gmail_request = urllib.request.Request(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                data=gmail_payload,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                method="POST",
            )
            stage = "Gmail API send"
            with urllib.request.urlopen(gmail_request, timeout=25) as response:
                if response.status < 200 or response.status >= 300:
                    raise HTTPException(status_code=502, detail=f"Gmail API returned HTTP {response.status}.")
            return
        except urllib.error.HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8", "replace"))
            except Exception:
                error_body = {}
            error_info = error_body.get("error", {}) if isinstance(error_body, dict) else {}
            if isinstance(error_info, str):
                error_code = error_info
                error_description = error_body.get("error_description", "")
            elif isinstance(error_info, dict):
                error_code = error_info.get("status") or error_info.get("code") or error_info.get("error", "")
                error_description = error_info.get("message", "")
            else:
                error_code = ""
                error_description = ""
            safe_description = " ".join(str(error_description).split())[:240]
            diagnostic = f"{stage} failed with HTTP {exc.code}"
            if error_code:
                diagnostic += f" ({str(error_code)[:80]})"
            if safe_description:
                diagnostic += f": {safe_description}"
            raise HTTPException(status_code=502, detail=diagnostic)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HTTPException(status_code=502, detail="Could not reach Google Gmail over HTTPS. Check Railway outbound connectivity and try again.")

    # Optional Resend HTTPS provider remains available when explicitly selected.
    if os.getenv("EMAIL_PROVIDER", "").strip().lower() == "resend" or (not os.getenv("EMAIL_PROVIDER", "").strip() and os.getenv("RESEND_API_KEY", "").strip()):
        resend_key = os.getenv("RESEND_API_KEY", "").strip()
        resend_sender = os.getenv("RESEND_FROM_EMAIL", "").strip()
        if not resend_key or not resend_sender:
            raise HTTPException(status_code=503, detail="Resend delivery is selected but its API key or verified sender is missing.")
        payload = json.dumps({
            "from": resend_sender,
            "to": [recipient],
            "subject": str(message["Subject"]),
            "text": body_text,
            "html": html_body,
        }).encode("utf-8")
        request = urllib.request.Request(
            "https://api.resend.com/emails",
            data=payload,
            headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                if response.status < 200 or response.status >= 300:
                    raise HTTPException(status_code=502, detail=f"Email provider returned HTTP {response.status}. Check the Resend API key and verified sender.")
            return
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                detail = "Resend rejected the API key. Check RESEND_API_KEY in Railway Variables."
            elif status == 422:
                detail = "Resend rejected the sender or recipient. Verify RESEND_FROM_EMAIL and the sending domain in Resend."
            else:
                detail = f"Resend email request failed with HTTP {status}. Check the Resend dashboard."
            raise HTTPException(status_code=502, detail=detail)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HTTPException(status_code=502, detail="Could not reach the email provider over HTTPS. Check Railway outbound connectivity and try again.")

    if not all((host, username, password, sender)):
        raise HTTPException(status_code=503, detail="Supplier email is not configured. Select EMAIL_PROVIDER=gmail and add Gmail OAuth credentials, or configure another supported email provider.")
    message["From"] = sender
    port = int(os.getenv("SMTP_PORT", "587"))
    use_ssl = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=25, context=ssl.create_default_context()) as server:
                server.login(username, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=25) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(username, password)
                server.send_message(message)
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(status_code=502, detail="SMTP authentication failed. Check the mailbox username and app password.")
    except smtplib.SMTPResponseException as exc:
        raise HTTPException(status_code=502, detail=f"SMTP server rejected the request with code {exc.smtp_code}. Check the SMTP account and sender settings.")
    except (smtplib.SMTPException, OSError, ValueError):
        raise HTTPException(status_code=502, detail="SMTP connection failed before the email could be sent. Your hosting provider may restrict outbound SMTP; use Gmail API or another HTTPS email provider.")

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
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS project_quotations (
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            quote_id INTEGER NOT NULL,
            linked_by TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL,
            PRIMARY KEY(project_id, quote_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_project_quotations_quote ON project_quotations(quote_id)")


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


class ProjectCreatePayload(BaseModel):
    title: str = Field(min_length=2, max_length=220)
    client_name: str = Field(default="", max_length=200)
    agency_details: str = Field(default="", max_length=1200)
    executive_summary: str = Field(default="", max_length=5000)
    procurement_lead: str = Field(default="", max_length=160)
    deliverables: List[str] = Field(default_factory=list)
    start_date: str
    due_date: str
    status: str = Field(default="active", pattern="^(active|on_hold|completed|cancelled)$")


class ProjectDailyUpdatePayload(BaseModel):
    update_type: str = Field(default="daily", pattern="^(daily|field_note|blocker|milestone)$")
    status: str = Field(default="In progress", max_length=80)
    progress_percent: float = Field(default=0, ge=0, le=100)
    field_notes: str = Field(default="", max_length=8000)
    blockers: str = Field(default="", max_length=4000)
    next_steps: str = Field(default="", max_length=4000)
    completed_milestones: List[str] = Field(default_factory=list)


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


PROJECT_MILESTONE_TEMPLATE = [
    {"key": "boq_extraction", "label": "BOQ extraction", "weight": 15},
    {"key": "rfq_dispatch", "label": "RFQ dispatch", "weight": 15},
    {"key": "bid_evaluation", "label": "Bid evaluation", "weight": 15},
    {"key": "po_award", "label": "PO award", "weight": 15},
]


def _project_milestones(raw: str) -> list:
    try:
        saved = json.loads(raw or "[]")
    except (TypeError, ValueError):
        saved = []
    by_key = {item.get("key"): item for item in saved if isinstance(item, dict)}
    result = []
    for template in PROJECT_MILESTONE_TEMPLATE:
        item = {**template, **by_key.get(template["key"], {})}
        item["completed"] = bool(item.get("completed", False))
        result.append(item)
    return result


def _complete_project_milestone(db, project_id: str, key: str, source: str = "workflow") -> None:
    row = db.execute("SELECT milestones_json FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if not row:
        return
    milestones = _project_milestones(row["milestones_json"])
    for item in milestones:
        if item["key"] == key and not item["completed"]:
            item["completed"] = True
            item["completed_at"] = _now()
            item["source"] = source
    db.execute("UPDATE projects SET milestones_json=?,updated_at=? WHERE project_id=?",
               (json.dumps(milestones), _now(), project_id))


def _recalculate_project(db, project_id: str) -> dict:
    project = db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    milestones = _project_milestones(project["milestones_json"])
    boq_count = db.execute("SELECT COUNT(*) FROM workflow_boq_items WHERE project_id=?", (project_id,)).fetchone()[0]
    approved_po_count = db.execute("SELECT COUNT(*) FROM workflow_purchase_orders WHERE project_id=? AND status='approved'", (project_id,)).fetchone()[0]
    if boq_count:
        for item in milestones:
            if item["key"] == "boq_extraction" and not item["completed"]:
                item.update({"completed": True, "completed_at": _now(), "source": "BOQ records"})
    if approved_po_count:
        for item in milestones:
            if item["key"] == "po_award" and not item["completed"]:
                item.update({"completed": True, "completed_at": _now(), "source": "Approved purchase order"})
    latest = db.execute("SELECT progress_percent FROM project_updates WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    daily_progress = float(latest["progress_percent"]) if latest else 0.0
    milestone_progress = sum(float(item["weight"]) for item in milestones if item["completed"])
    overall = round(min(100.0, milestone_progress + daily_progress * 0.4), 1)
    db.execute("UPDATE projects SET milestones_json=?,progress_percent=?,updated_at=? WHERE project_id=?",
               (json.dumps(milestones), overall, _now(), project_id))
    return {"progress_percent": overall, "milestone_progress": round(milestone_progress, 1),
            "daily_progress": daily_progress, "milestones": milestones}


def _project_due_status(project: dict) -> dict:
    if project.get("status") == "completed":
        return {"due_status": "complete", "days_remaining": 0, "due_label": "Completed"}
    if not project.get("due_date"):
        return {"due_status": "unscheduled", "days_remaining": None, "due_label": "No due date"}
    try:
        days = (date.fromisoformat(project["due_date"]) - date.today()).days
    except ValueError:
        return {"due_status": "unscheduled", "days_remaining": None, "due_label": "Check due date"}
    if days < 0:
        return {"due_status": "overdue", "days_remaining": days, "due_label": f"{abs(days)} days overdue"}
    if days == 0:
        return {"due_status": "due_today", "days_remaining": 0, "due_label": "Due today"}
    if days <= 7:
        return {"due_status": "due_soon", "days_remaining": days, "due_label": f"{days} days remaining"}
    return {"due_status": "on_track", "days_remaining": days, "due_label": f"{days} days remaining"}


def _project_overview(db, project_id: str) -> dict:
    row = db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    project = dict(row)
    progress = _recalculate_project(db, project_id)
    project = dict(db.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone())
    project["deliverables"] = json.loads(project.pop("deliverables_json") or "[]")
    project["milestones"] = progress["milestones"]
    project["progress_percent"] = progress["progress_percent"]
    project["progress_breakdown"] = {"milestones": progress["milestone_progress"], "daily_updates": round(progress["daily_progress"] * 0.4, 1)}
    project.update(_project_due_status(project))
    project["boq_items_count"] = db.execute("SELECT COUNT(*) FROM workflow_boq_items WHERE project_id=?", (project_id,)).fetchone()[0]
    project["quotations_count"] = db.execute("SELECT COUNT(*) FROM workflow_quotations WHERE project_id=?", (project_id,)).fetchone()[0]
    project["purchase_orders_count"] = db.execute("SELECT COUNT(*) FROM workflow_purchase_orders WHERE project_id=?", (project_id,)).fetchone()[0]
    linked_quotes = db.execute("""SELECT q.id,q.unit_price,q.freight,q.tax_rate,q.lead_days,q.payment_days,q.note,q.created_at,
        s.name AS supplier,s.email AS supplier_email,r.code AS request_code,r.material,r.category,r.quantity,r.unit
        FROM project_quotations pq JOIN quotes q ON q.id=pq.quote_id
        JOIN suppliers s ON s.id=q.supplier_id JOIN requests r ON r.id=q.request_id
        WHERE pq.project_id=? ORDER BY pq.linked_at DESC""", (project_id,)).fetchall()
    project["linked_quotations"] = []
    for quote in linked_quotes:
        item = dict(quote)
        subtotal = Decimal(str(item["quantity"])) * Decimal(str(item["unit_price"]))
        tax = subtotal * Decimal(str(item["tax_rate"])) / Decimal("100")
        item["total_landed"] = float(subtotal + tax + Decimal(str(item["freight"])))
        project["linked_quotations"].append(item)
    updates = db.execute("""SELECT id,author,update_type,status,progress_percent,field_notes,blockers,next_steps,
        completed_milestones_json,created_at FROM project_updates
        WHERE project_id=? ORDER BY id DESC LIMIT 40""", (project_id,)).fetchall()
    project["updates"] = []
    for update in updates:
        item = dict(update)
        item["completed_milestones"] = json.loads(item.pop("completed_milestones_json") or "[]")
        project["updates"].append(item)
    return project


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
        db.execute("""INSERT OR IGNORE INTO projects
            (project_id,title,executive_summary,status,created_at,updated_at)
            VALUES(?,?,?,'active',?,?)""",
            (project_id, project_id, extracted["project_summary"], now, now))
        db.execute("DELETE FROM workflow_boq_items WHERE project_id=?", (project_id,))
        for item in extracted["items"]:
            db.execute("""INSERT INTO workflow_boq_items(project_id,item_code,item_name,specification,quantity,unit,category,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (project_id, item.get("item_code",""), item["item_name"], item.get("specification",""),
                 item["quantity"], item.get("unit",""), item.get("category","General"), now))
        if db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            _complete_project_milestone(db, project_id, "boq_extraction", "BOQ extraction workflow")
            _recalculate_project(db, project_id)
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
    with connect() as db:
        if db.execute("SELECT 1 FROM projects WHERE project_id=?", (payload.project_id,)).fetchone():
            _complete_project_milestone(db, payload.project_id, "bid_evaluation", "Supplier comparison workflow")
            _recalculate_project(db, payload.project_id)
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
        if status == "approved":
            _complete_project_milestone(db, row["project_id"], "po_award", "Human-approved purchase order")
            _recalculate_project(db, row["project_id"])
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


class DashboardSupplierPayload(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    city: str = Field(min_length=2, max_length=120)
    contact: str = Field(default="", max_length=500)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=80)
    website: str = Field(default="", max_length=240)
    address: str = Field(default="", max_length=300)
    services: str = Field(default="", max_length=1200)
    rating: float = Field(default=0, ge=0, le=5)
    on_time: float = Field(default=0, ge=0, le=100)
    status: str = Field(default="Pending verification", max_length=60)


class CompanyProfilePayload(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    registration_number: str = Field(default="", max_length=80)
    industry: str = Field(default="", max_length=160)
    services: List[str] = Field(default_factory=list)
    service_modes: List[str] = Field(default_factory=list)
    client_segments: List[str] = Field(default_factory=list)
    currency: str = Field(default="NGN", min_length=3, max_length=3)


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

class AnalysisPurchaseOrderPayload(BaseModel):
    quote_id: int = Field(gt=0)



class DashboardDecisionPayload(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    note: str = ""


@app.get("/api/requests")
def dashboard_requests():
    return {"requests": dashboard_module.rows("SELECT * FROM requests ORDER BY id DESC")}


@app.get("/api/suppliers")
def dashboard_suppliers():
    return {"suppliers": dashboard_module.rows("SELECT * FROM suppliers ORDER BY name COLLATE NOCASE")}


@app.post("/api/suppliers", status_code=201)
def dashboard_create_supplier(payload: DashboardSupplierPayload, request: Request):
    now = _now()
    with dashboard_module.connect() as db:
        cur = db.execute("""INSERT INTO suppliers
            (name,city,rating,on_time,contact,status,email,phone,website,address,services)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (payload.name.strip(), payload.city.strip(), payload.rating, payload.on_time,
             payload.contact.strip(), payload.status.strip(), payload.email.strip(),
             payload.phone.strip(), payload.website.strip(), payload.address.strip(),
             payload.services.strip()))
        supplier_id = cur.lastrowid
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (getattr(request.state, "auth_user", ""), "create", "supplier", str(supplier_id),
             json.dumps({"name": payload.name, "city": payload.city}), now))
    return {"success": True, "supplier_id": supplier_id, "status": payload.status}



@app.put("/api/suppliers/{supplier_id}")
def dashboard_update_supplier(supplier_id: int, payload: DashboardSupplierPayload, request: Request):
    now = _now()
    with dashboard_module.connect() as db:
        existing = db.execute("SELECT id FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Seller not found")
        db.execute("""UPDATE suppliers SET name=?,city=?,rating=?,on_time=?,contact=?,status=?,email=?,phone=?,website=?,address=?,services=? WHERE id=?""",
            (payload.name.strip(), payload.city.strip(), payload.rating, payload.on_time,
             payload.contact.strip(), payload.status.strip(), payload.email.strip(),
             payload.phone.strip(), payload.website.strip(), payload.address.strip(),
             payload.services.strip(), supplier_id))
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (getattr(request.state, "auth_user", ""), "update", "supplier", str(supplier_id),
             json.dumps({"name": payload.name, "city": payload.city}), now))
    return {"success": True, "supplier_id": supplier_id, "status": payload.status}


@app.get("/api/quotes")
def dashboard_quotes():
    quotes = dashboard_module.rows("""SELECT q.*,s.name supplier,s.city supplier_city,
        s.contact supplier_contact,r.code request_code,r.material,r.category,
        EXISTS(SELECT 1 FROM orders o WHERE o.quote_id=q.id) AS has_purchase_order
        FROM quotes q JOIN suppliers s ON s.id=q.supplier_id
        JOIN requests r ON r.id=q.request_id ORDER BY q.id DESC""")
    return {"quotes": quotes}


@app.get("/api/orders")
def dashboard_orders():
    return {"orders": dashboard_module.dashboard()["orders"]}


@app.get("/api/orders/{order_id}")
def dashboard_order_detail(order_id: int):
    with dashboard_module.connect() as db:
        row = db.execute("""SELECT
            o.id, o.code, o.quantity, o.unit_price, o.freight, o.tax, o.total,
            o.status, o.required_by, o.created_at, o.decision_note,
            o.analysis_estimate, o.lead_days, o.payment_days,
            s.name AS supplier, s.email AS supplier_email, s.phone AS supplier_phone,
            s.contact AS supplier_contact, s.address AS supplier_address,
            s.city AS supplier_city,
            r.code AS request_code, r.material, r.category, r.unit,
            r.site, r.priority, r.required_by AS request_required_by,
            q.tax_rate, q.note AS quotation_note
            FROM orders o
            JOIN suppliers s ON s.id=o.supplier_id
            JOIN requests r ON r.id=o.request_id
            LEFT JOIN quotes q ON q.id=o.quote_id
            WHERE o.id=?""", (order_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Purchase order not found")
    return {"order": dict(row)}


@app.delete("/api/requests/{request_id}")
def dashboard_delete_request(request_id: int, request: Request):
    username = getattr(request.state, "auth_user", "")
    with dashboard_module.connect() as db:
        row = db.execute("SELECT code,material FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Purchase request not found")
        quote_count = db.execute("SELECT COUNT(*) FROM quotes WHERE request_id=?", (request_id,)).fetchone()[0]
        order_count = db.execute("SELECT COUNT(*) FROM orders WHERE request_id=?", (request_id,)).fetchone()[0]
        db.execute("DELETE FROM orders WHERE request_id=?", (request_id,))
        db.execute("DELETE FROM project_quotations WHERE quote_id IN (SELECT id FROM quotes WHERE request_id=?)", (request_id,))
        db.execute("DELETE FROM quotes WHERE request_id=?", (request_id,))
        db.execute("DELETE FROM requests WHERE id=?", (request_id,))
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (username, "delete", "purchase_request", str(request_id),
             json.dumps({"request_code": row["code"], "material": row["material"], "deleted_quotations": quote_count, "deleted_purchase_orders": order_count}), _now()))
    return {"success": True, "deleted_request": row["code"], "deleted_quotations": quote_count, "deleted_purchase_orders": order_count}

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


@app.get("/api/quotes/{quote_id}")
def dashboard_quote_detail(quote_id: int):
    with dashboard_module.connect() as db:
        _ensure_supplier_request_email_table(db)
        row = db.execute("""SELECT q.*, s.name AS supplier, s.email AS supplier_email,
            s.city AS supplier_city, s.contact AS supplier_contact, s.rating AS supplier_rating,
            r.code AS request_code, r.material, r.category, r.quantity, r.unit, r.site,
            r.required_by, r.priority, r.status AS request_status,
            EXISTS(SELECT 1 FROM supplier_request_emails e WHERE e.quote_id=q.id) AS request_email_sent,
            EXISTS(SELECT 1 FROM orders o WHERE o.quote_id=q.id) AS has_purchase_order
            FROM quotes q
            JOIN suppliers s ON s.id=q.supplier_id
            JOIN requests r ON r.id=q.request_id
            WHERE q.id=?""", (quote_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Quotation not found")
    quote = dict(row)
    quantity = Decimal(str(quote["quantity"]))
    subtotal = quantity * Decimal(str(quote["unit_price"]))
    tax = subtotal * Decimal(str(quote["tax_rate"])) / Decimal("100")
    quote["subtotal"] = float(subtotal)
    quote["tax_amount"] = float(tax)
    quote["total_landed"] = float(subtotal + tax + Decimal(str(quote["freight"])))
    return quote

@app.post("/api/quotes/{quote_id}/send-request")
def dashboard_send_supplier_request(quote_id: int, request: Request):
    with dashboard_module.connect() as db:
        _ensure_supplier_request_email_table(db)
        quote = db.execute("""SELECT q.id AS quote_id, q.supplier_id, q.request_id, s.name AS supplier_name, s.email AS supplier_email, s.status AS supplier_status, r.code AS request_code, r.material, r.category, r.quantity, r.unit, r.site, r.required_by, r.status AS request_status FROM quotes q JOIN suppliers s ON s.id=q.supplier_id JOIN requests r ON r.id=q.request_id WHERE q.id=?""", (quote_id,)).fetchone()
        if not quote: raise HTTPException(status_code=404, detail="Quotation not found")
        if (quote["supplier_status"] or "").strip().casefold() not in {"verified", "active"} or "[fictional demo]" in quote["supplier_name"].casefold():
            raise HTTPException(status_code=409, detail="Only verified or active real sellers can receive purchase requests.")
        if quote["request_status"] != "Open": raise HTTPException(status_code=409, detail="This purchase request is no longer open.")
        existing = db.execute("SELECT sent_at,recipient FROM supplier_request_emails WHERE quote_id=?", (quote_id,)).fetchone()
        if existing: return {"success": True, "already_sent": True, "sent_at": existing["sent_at"], "recipient": existing["recipient"]}
        _send_supplier_request_email(quote)
        sent_at = datetime.utcnow().isoformat(); recipient = quote["supplier_email"].strip()
        db.execute("INSERT INTO supplier_request_emails(quote_id,supplier_id,request_id,recipient,sent_by,sent_at) VALUES(?,?,?,?,?,?)", (quote_id, quote["supplier_id"], quote["request_id"], recipient, getattr(request.state, "auth_user", ""), sent_at))
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)", (getattr(request.state, "auth_user", ""), "send_supplier_request_email", "quotation", str(quote_id), json.dumps({"recipient": recipient, "request_code": quote["request_code"]}), sent_at))
    return {"success": True, "already_sent": False, "recipient": recipient, "sent_at": sent_at}

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



@app.put("/api/quotes/{quote_id}")
def dashboard_update_quote(quote_id: int, payload: DashboardQuotePayload, request: Request):
    now = datetime.utcnow().isoformat()
    with dashboard_module.connect() as db:
        current = db.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not current: raise HTTPException(status_code=404, detail="Quotation not found")
        if db.execute("SELECT 1 FROM orders WHERE quote_id=? LIMIT 1", (quote_id,)).fetchone():
            raise HTTPException(status_code=409, detail="This quotation already has a purchase-order proposal. It is locked to preserve approval and audit history.")
        if not db.execute("SELECT 1 FROM requests WHERE id=?", (payload.request_id,)).fetchone(): raise HTTPException(status_code=404, detail="Purchase request not found")
        if not db.execute("SELECT 1 FROM suppliers WHERE id=?", (payload.supplier_id,)).fetchone(): raise HTTPException(status_code=404, detail="Supplier not found")
        before = {key: current[key] for key in ("supplier_id","request_id","unit_price","freight","tax_rate","lead_days","payment_days","note")}
        db.execute("""UPDATE quotes SET supplier_id=?,request_id=?,unit_price=?,freight=?,tax_rate=?,lead_days=?,payment_days=?,note=? WHERE id=?""",
            (payload.supplier_id,payload.request_id,payload.unit_price,payload.freight,payload.tax_rate,payload.lead_days,payload.payment_days,payload.note,quote_id))
        _ensure_supplier_request_email_table(db)
        email_invalidated = db.execute("DELETE FROM supplier_request_emails WHERE quote_id=?", (quote_id,)).rowcount > 0
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (getattr(request.state, "auth_user", ""), "update", "quotation", str(quote_id),
             json.dumps({"before": before, "after": payload.model_dump(), "supplier_email_status_reset": email_invalidated}), now))
    return {"success": True, "id": quote_id, "supplier_email_reset": email_invalidated}


@app.delete("/api/quotes/{quote_id}")
def dashboard_delete_quote(quote_id: int, request: Request):
    with dashboard_module.connect() as db:
        current = db.execute("""SELECT q.id,q.unit_price,r.code AS request_code,s.name AS supplier_name
            FROM quotes q JOIN requests r ON r.id=q.request_id JOIN suppliers s ON s.id=q.supplier_id WHERE q.id=?""", (quote_id,)).fetchone()
        if not current: raise HTTPException(status_code=404, detail="Quotation not found")
        if db.execute("SELECT 1 FROM orders WHERE quote_id=? LIMIT 1", (quote_id,)).fetchone():
            raise HTTPException(status_code=409, detail="This quotation has a linked purchase-order proposal and cannot be deleted. Keep it for approval and audit history.")
        _ensure_supplier_request_email_table(db)
        email_removed = db.execute("DELETE FROM supplier_request_emails WHERE quote_id=?", (quote_id,)).rowcount
        project_links_removed = db.execute("DELETE FROM project_quotations WHERE quote_id=?", (quote_id,)).rowcount
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (getattr(request.state, "auth_user", ""), "delete", "quotation", str(quote_id),
             json.dumps({"request_code": current["request_code"], "supplier": current["supplier_name"], "unit_price": current["unit_price"], "supplier_email_records_removed": email_removed, "project_links_removed": project_links_removed}), _now()))
        db.execute("DELETE FROM quotes WHERE id=?", (quote_id,))
    return {"success": True, "deleted_quote_id": quote_id}


@app.post("/api/orders/from-analysis", status_code=201)
def dashboard_create_order_from_analysis(payload: AnalysisPurchaseOrderPayload, request: Request):
    now = datetime.utcnow().isoformat()
    with dashboard_module.connect() as db:
        quote = db.execute("""SELECT q.*, s.name AS supplier_name, s.status AS supplier_status,
            s.rating AS supplier_rating, r.code AS request_code, r.material, r.category,
            r.quantity, r.unit, r.site, r.required_by, r.status AS request_status
            FROM quotes q JOIN suppliers s ON s.id=q.supplier_id
            JOIN requests r ON r.id=q.request_id WHERE q.id=?""",
            (payload.quote_id,)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        supplier_status = (quote["supplier_status"] or "").strip().casefold()
        if supplier_status not in {"verified", "active"} or "[fictional demo]" in quote["supplier_name"].casefold():
            raise HTTPException(status_code=409, detail="This seller is not verified for purchase-order generation. Verify the seller first.")
        if quote["request_status"] != "Open":
            raise HTTPException(status_code=409, detail="This purchase request is no longer open.")
        _ensure_supplier_request_email_table(db)
        email_record = db.execute("SELECT recipient,sent_at FROM supplier_request_emails WHERE quote_id=?", (payload.quote_id,)).fetchone()
        if not email_record:
            raise HTTPException(status_code=409, detail="Email the purchase request to this seller and receive a successful send confirmation before generating the PO proposal.")
        existing = db.execute("SELECT code,status FROM orders WHERE quote_id=?", (payload.quote_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"A purchase order proposal ({existing['code']}) already exists for this quotation.")
        qty, unit_price = Decimal(str(quote["quantity"])), Decimal(str(quote["unit_price"]))
        subtotal = qty * unit_price
        tax = subtotal * Decimal(str(quote["tax_rate"])) / Decimal("100")
        freight = Decimal(str(quote["freight"]))
        total = subtotal + tax + freight
        analysis = dashboard_module.analyse_request(int(quote["request_id"]))
        analysed_offer = next((offer for offer in analysis["offers"] if int(offer["id"]) == payload.quote_id), None)
        analysis_estimate = Decimal(str(analysed_offer["estimated_total"])) if analysed_offer else total
        reasons = ["Generated from quotation analysis; human approval required before any purchase commitment."]
        if total > Decimal("2500000"): reasons.append("Order exceeds the configured review threshold.")
        if float(quote["supplier_rating"] or 0) < 3.5: reasons.append("Supplier rating is below the preferred threshold.")
        if int(quote["lead_days"]) > max(0, (date.fromisoformat(quote["required_by"]) - date.today()).days):
            reasons.append("Quoted lead time misses the requested delivery date.")
        count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        code = f"PO-{datetime.utcnow():%y%m%d}-{count+1:03d}"
        cur = db.execute("""INSERT INTO orders
            (code,supplier_id,request_id,quantity,unit_price,freight,tax,total,status,
             required_by,created_at,decision_note,quote_id,analysis_estimate,lead_days,payment_days)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, quote["supplier_id"], quote["request_id"], float(qty), float(unit_price),
             float(freight), float(tax), float(total), "Pending approval", quote["required_by"],
             now, "; ".join(reasons), payload.quote_id, float(analysis_estimate),
             int(quote["lead_days"]), int(quote["payment_days"])))
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (getattr(request.state, "auth_user", ""), "create_from_analysis", "purchase_order",
             str(cur.lastrowid), json.dumps({"po_number": code, "quote_id": payload.quote_id,
             "request_code": quote["request_code"], "supplier": quote["supplier_name"],
             "commercial_total": float(total), "analysis_estimate": float(analysis_estimate)}), now))
    return {"success": True, "id": cur.lastrowid, "code": code,
            "status": "Pending approval", "total": float(total),
            "analysis_estimate": float(analysis_estimate), "reasons": reasons}


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
@app.get("/api/admin/company-profile")
def get_company_profile():
    with connect() as db:
        row = db.execute("SELECT * FROM company_profile WHERE id=1").fetchone()
    if not row:
        return {
            "company_name": "", "registration_number": "", "industry": "",
            "services": [], "service_modes": [], "client_segments": [], "currency": "NGN"
        }
    return {
        "company_name": row["company_name"],
        "registration_number": row["registration_number"],
        "industry": row["industry"],
        "services": json.loads(row["services_json"]),
        "service_modes": json.loads(row["service_modes_json"]),
        "client_segments": json.loads(row["client_segments_json"]),
        "currency": row["currency"],
        "updated_at": row["updated_at"],
    }


@app.put("/api/admin/company-profile")
def save_company_profile(payload: CompanyProfilePayload):
    now = _now()
    with connect() as db:
        db.execute("""INSERT INTO company_profile
            (id,company_name,registration_number,industry,services_json,service_modes_json,client_segments_json,currency,updated_at)
            VALUES(1,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              company_name=excluded.company_name,
              registration_number=excluded.registration_number,
              industry=excluded.industry,
              services_json=excluded.services_json,
              service_modes_json=excluded.service_modes_json,
              client_segments_json=excluded.client_segments_json,
              currency=excluded.currency,
              updated_at=excluded.updated_at""",
            (payload.company_name.strip(), payload.registration_number.strip(), payload.industry.strip(),
             json.dumps([x.strip() for x in payload.services if x.strip()]),
             json.dumps([x.strip() for x in payload.service_modes if x.strip()]),
             json.dumps([x.strip() for x in payload.client_segments if x.strip()]),
             payload.currency.upper(), now))
    return {"success": True, "profile": payload.model_dump(), "updated_at": now}


@app.get("/api/projects")
def list_project_workspaces():
    with connect() as db:
        records = db.execute("SELECT project_id FROM projects ORDER BY updated_at DESC").fetchall()
        projects = [_project_overview(db, row["project_id"]) for row in records]
    return {"projects": projects}


@app.post("/api/projects", status_code=201)
def create_project_workspace(payload: ProjectCreatePayload, request: Request):
    try:
        start = date.fromisoformat(payload.start_date)
        due = date.fromisoformat(payload.due_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Project start and due dates must use YYYY-MM-DD")
    if due < start:
        raise HTTPException(status_code=422, detail="Project due date cannot be earlier than its start date")
    project_id = "prj_" + uuid.uuid4().hex[:12]
    now = _now()
    milestones = [{**item, "completed": False} for item in PROJECT_MILESTONE_TEMPLATE]
    with connect() as db:
        db.execute("""INSERT INTO projects
            (project_id,title,client_name,agency_details,executive_summary,procurement_lead,deliverables_json,
             start_date,due_date,status,milestones_json,progress_percent,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (project_id,payload.title.strip(),payload.client_name.strip(),payload.agency_details.strip(),
             payload.executive_summary.strip(),payload.procurement_lead.strip(),
             json.dumps([x.strip() for x in payload.deliverables if x.strip()]),
             payload.start_date,payload.due_date,payload.status,json.dumps(milestones),now,now))
        db.execute("""INSERT OR IGNORE INTO workflow_projects
            (project_id,summary,status,created_at,updated_at) VALUES(?,?,?,?,?)""",
            (project_id,payload.executive_summary.strip(), "created", now, now))
        result = _project_overview(db, project_id)
    return {"success": True, "project": result}


@app.get("/api/projects/{project_id}/quotations")
def list_project_quotation_candidates(project_id: str):
    with connect() as db:
        if not db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
        rows = db.execute("""SELECT q.id,q.unit_price,q.freight,q.tax_rate,q.lead_days,q.payment_days,q.note,q.created_at,
            s.name AS supplier,r.code AS request_code,r.material,r.category,r.quantity,r.unit,
            EXISTS(SELECT 1 FROM project_quotations pq WHERE pq.project_id=? AND pq.quote_id=q.id) AS already_linked
            FROM quotes q JOIN suppliers s ON s.id=q.supplier_id JOIN requests r ON r.id=q.request_id
            ORDER BY q.id DESC""", (project_id,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        subtotal = Decimal(str(item["quantity"])) * Decimal(str(item["unit_price"]))
        tax = subtotal * Decimal(str(item["tax_rate"])) / Decimal("100")
        item["total_landed"] = float(subtotal + tax + Decimal(str(item["freight"])))
        result.append(item)
    return {"quotations": result}


@app.post("/api/projects/{project_id}/quotations/{quote_id}", status_code=201)
def link_quotation_to_project(project_id: str, quote_id: int, request: Request):
    now = _now()
    actor = getattr(request.state, "auth_user", "") or "Workspace user"
    with connect() as db:
        project = db.execute("SELECT title FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        quote = db.execute("""SELECT q.id,s.name AS supplier,r.code AS request_code,r.material
            FROM quotes q JOIN suppliers s ON s.id=q.supplier_id JOIN requests r ON r.id=q.request_id
            WHERE q.id=?""", (quote_id,)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        try:
            db.execute("INSERT INTO project_quotations(project_id,quote_id,linked_by,linked_at) VALUES(?,?,?,?)",
                       (project_id,quote_id,actor,now))
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise HTTPException(status_code=409, detail="This quotation is already linked to this project.")
            raise
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (actor,"link","project_quotation",f"{project_id}:{quote_id}",
             json.dumps({"project_title":project["title"],"quote_id":quote_id,"supplier":quote["supplier"],
                         "request_code":quote["request_code"],"material":quote["material"]}),now))
        overview = _project_overview(db, project_id)
    return {"success":True,"project":overview}


@app.delete("/api/projects/{project_id}/quotations/{quote_id}")
def unlink_quotation_from_project(project_id: str, quote_id: int, request: Request):
    actor = getattr(request.state, "auth_user", "") or "Workspace user"
    with connect() as db:
        cur = db.execute("DELETE FROM project_quotations WHERE project_id=? AND quote_id=?", (project_id,quote_id))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="This quotation is not linked to the project.")
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (actor,"unlink","project_quotation",f"{project_id}:{quote_id}",json.dumps({"quote_id":quote_id}),_now()))
        overview = _project_overview(db, project_id)
    return {"success":True,"project":overview}


@app.get("/api/projects/{project_id}/overview")
def get_project_workspace(project_id: str):
    with connect() as db:
        return {"project": _project_overview(db, project_id)}


@app.post("/api/projects/{project_id}/updates", status_code=201)
def add_project_daily_update(project_id: str, payload: ProjectDailyUpdatePayload, request: Request):
    allowed = {item["key"] for item in PROJECT_MILESTONE_TEMPLATE}
    invalid = sorted(set(payload.completed_milestones) - allowed)
    if invalid:
        raise HTTPException(status_code=422, detail="Unknown milestone key: " + ", ".join(invalid))
    now = _now()
    author = getattr(request.state, "auth_user", "") or "Workspace user"
    with connect() as db:
        if not db.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
        db.execute("""INSERT INTO project_updates
            (project_id,author,update_type,status,progress_percent,field_notes,blockers,next_steps,
             completed_milestones_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (project_id,author,payload.update_type,payload.status,payload.progress_percent,
             payload.field_notes.strip(),payload.blockers.strip(),payload.next_steps.strip(),
             json.dumps(payload.completed_milestones),now))
        for key in payload.completed_milestones:
            _complete_project_milestone(db, project_id, key, "Daily team update")
        project = _project_overview(db, project_id)
    return {"success": True, "project": project}


@app.post("/api/projects/{project_id}/recalculate")
def recalculate_project_progress(project_id: str):
    with connect() as db:
        progress = _recalculate_project(db, project_id)
        project = _project_overview(db, project_id)
    return {"success": True, "progress": progress, "project": project}


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
