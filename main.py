"""Unified ProcureAI API.

Dashboard, BOQ extraction and agentic intake share one SQLite database.
External supplier contact and purchase commitments are deliberately gated.
"""
from __future__ import annotations

import json
import asyncio
import sqlite3
import shutil
import tempfile
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
from datetime import date, datetime, timedelta
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import app as dashboard_module
from supplier_portal import router as supplier_portal_router, dispatch_request_rfq
from procureai_store import connect, init_store
from agentic.intake import IntakeAgent, SQLiteRequestStore

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
EXPECTED_API_KEY = os.getenv("PROCUREAI_WEBHOOK_SECRET", "").strip()
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="ProcureAI Unified API", version="3.0.0")
app.include_router(supplier_portal_router)
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

def _database_file() -> Path:
    return Path(os.getenv("PROCUREAI_DB_PATH", str(BASE_DIR / "procureai.db")))


def _create_database_backup() -> dict:
    """Create a consistent SQLite online backup and retain the newest seven copies."""
    source_path = _database_file()
    if not source_path.is_file():
        raise RuntimeError("ProcureAI database file is not available for backup")
    backup_dir = Path(os.getenv("PROCUREAI_BACKUP_DIR", str(source_path.parent / "backups")))
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    final_path = backup_dir / f"procureai-{stamp}.sqlite3"
    temp_path = backup_dir / f".{final_path.name}.tmp"
    try:
        with sqlite3.connect(str(source_path), timeout=30) as source_db:
            with sqlite3.connect(str(temp_path), timeout=30) as backup_db:
                source_db.backup(backup_db)
                integrity = backup_db.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise RuntimeError("Database backup failed its SQLite integrity check")
        os.replace(temp_path, final_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    backups = sorted(backup_dir.glob("procureai-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    for old_backup in backups[7:]:
        old_backup.unlink(missing_ok=True)
    return {"filename": final_path.name, "size_bytes": final_path.stat().st_size,
            "created_at": datetime.utcfromtimestamp(final_path.stat().st_mtime).isoformat(timespec="seconds") + "Z",
            "integrity": "ok"}


def _list_database_backups() -> list[dict]:
    source_path = _database_file()
    backup_dir = Path(os.getenv("PROCUREAI_BACKUP_DIR", str(source_path.parent / "backups")))
    if not backup_dir.is_dir():
        return []
    files = sorted(backup_dir.glob("procureai-*.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [{"filename": path.name, "size_bytes": path.stat().st_size,
             "created_at": datetime.utcfromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds") + "Z"}
            for path in files[:7]]


async def _scheduled_database_backups():
    """Check hourly and create one integrity-checked backup every 24 hours."""
    while True:
        try:
            backups = _list_database_backups()
            latest = datetime.fromisoformat(backups[0]["created_at"].replace("Z", "+00:00")) if backups else None
            due = latest is None or (datetime.now(latest.tzinfo) - latest).total_seconds() >= 24 * 60 * 60
            if due:
                result = await asyncio.to_thread(_create_database_backup)
                print("ProcureAI database backup completed:", result["filename"], result["integrity"])
        except Exception as exc:
            print("ProcureAI database backup failed:", type(exc).__name__)
        await asyncio.sleep(60 * 60)


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
    additional_information = str(quote["additional_information"] or "").strip()
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
        *(["", "ADDITIONAL INFORMATION / TECHNICAL SPECIFICATIONS", additional_information] if additional_information else []),
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
        {"<div style='margin:18px 0;padding:15px 16px;background:#f7f9fc;border:1px solid #e2e8f0;border-radius:6px;'><div style='font-size:12px;font-weight:bold;letter-spacing:.5px;color:#172b4d;margin-bottom:7px;'>ADDITIONAL INFORMATION / TECHNICAL SPECIFICATIONS</div><div style='font-size:14px;line-height:1.7;color:#526174;white-space:pre-wrap;'>" + esc(additional_information) + "</div></div>" if additional_information else ""}
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
    if path.startswith("/api/") and not path.startswith("/api/v1/") and path != "/api/auth/login" and not path.startswith("/api/supplier-portal/") and request.method != "OPTIONS":
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
    if os.getenv("PROCUREAI_AUTO_BACKUP", "true").strip().lower() in ("1", "true", "yes", "on"):
        app.state.database_backup_task = asyncio.create_task(_scheduled_database_backups())
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS project_quotations (
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            quote_id INTEGER NOT NULL,
            linked_by TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL,
            PRIMARY KEY(project_id, quote_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_project_quotations_quote ON project_quotations(quote_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS project_purchase_requests (
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
            linked_by TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL,
            PRIMARY KEY(project_id, request_id),
            UNIQUE(request_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_project_purchase_requests_project ON project_purchase_requests(project_id)")
        db.execute("""CREATE TABLE IF NOT EXISTS project_closures (
            project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE CASCADE,
            completed_at TEXT NOT NULL,
            closed_by TEXT NOT NULL DEFAULT '',
            completion_note TEXT NOT NULL DEFAULT '',
            progress_at_close REAL NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
        )""")


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

class WorkflowTechnicalReviewPayload(BaseModel):
    status: str = Field(pattern="^(pending|compliant|deviation|non_compliant)$")
    notes: str = Field(default="", max_length=5000)


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
    project_location: str = Field(default="", max_length=240)
    project_category: str = Field(default="", max_length=120)
    estimated_budget: float = Field(default=0, ge=0)
    expected_duration_days: int = Field(default=30, ge=1, le=3650)
    priority: str = Field(default="medium", pattern="^(low|medium|high|urgent)$")
    planned_purchase_requests: int = Field(default=1, ge=0, le=500)


class ProjectDailyUpdatePayload(BaseModel):
    update_type: str = Field(default="daily", pattern="^(daily|field_note|blocker|milestone)$")
    status: str = Field(default="In progress", max_length=80)
    progress_percent: float = Field(default=0, ge=0, le=100)
    field_notes: str = Field(default="", max_length=8000)
    blockers: str = Field(default="", max_length=4000)
    next_steps: str = Field(default="", max_length=4000)
    completed_milestones: List[str] = Field(default_factory=list)


class ProjectCompletionPayload(BaseModel):
    completion_note: str = Field(default="", max_length=3000)

class ProjectTerminationPayload(BaseModel):
    termination_note: str = Field(default="", max_length=3000)


class BuyerConfirmationPayload(BaseModel):
    buyer_name: str = Field(min_length=2, max_length=200)
    organisation: str = Field(default="", max_length=200)
    channel: str = Field(pattern="^(email|whatsapp|phone)$")
    status: str = Field(pattern="^(confirmed|changes_requested|rejected)$")
    requirements_confirmed: str = Field(min_length=2, max_length=5000)
    budget_delivery_accepted: str = Field(pattern="^(yes|no|conditional|not_discussed)$")
    comments: str = Field(default="", max_length=5000)


def _require_buyer_confirmation(db, project_id: str):
    project = db.execute("SELECT status FROM projects WHERE project_id=?", (project_id,)).fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if str(project["status"] or "").strip().casefold() in ("completed", "terminated", "cancelled", "canceled", "archived"):
        raise HTTPException(status_code=409, detail="This project is closed or archived. Procurement actions are read-only.")
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='buyer_confirmations'").fetchone()
    if not table:
        raise HTTPException(status_code=409, detail="Buyer confirmation is required before AI bid analysis or PO creation.")
    latest = db.execute("SELECT status,budget_delivery_accepted FROM buyer_confirmations WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
    if not latest or latest["status"] != "confirmed" or latest["budget_delivery_accepted"] != "yes":
        raise HTTPException(status_code=409, detail="Explicit buyer confirmation of the requirements, budget and delivery is required. Record a confirmed decision and select Yes for budget and delivery acceptance in the Project Workspace.")


def _require_verified_workflow_supplier(db, supplier_id: str):
    supplier = db.execute("SELECT verification_status FROM workflow_suppliers WHERE supplier_id=?", (supplier_id,)).fetchone()
    if not supplier or supplier["verification_status"] != "verified":
        raise HTTPException(status_code=409, detail="Supplier must be verified at the point of purchase-order proposal.")
    return supplier




def _require_request_buyer_confirmation(db, request_id: int) -> str:
    """Resolve a dashboard request to its project and enforce the buyer gate."""
    linked = db.execute("""SELECT p.project_id,p.status FROM project_purchase_requests pr
        JOIN projects p ON p.project_id=pr.project_id WHERE pr.request_id=?""", (request_id,)).fetchone()
    if not linked:
        raise HTTPException(status_code=409, detail="This purchase request is not linked to a managed project. Link it to a project before analysis or purchase-order creation.")
    if linked["status"] in ("completed", "terminated", "cancelled"):
        raise HTTPException(status_code=409, detail="This project is closed or archived. Procurement actions are read-only.")
    _require_buyer_confirmation(db, linked["project_id"])
    return linked["project_id"]


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
    if project["status"] == "terminated":
        latest = db.execute("SELECT progress_percent FROM project_updates WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        daily_progress = float(latest["progress_percent"]) if latest else 0.0
        milestone_progress = sum(float(item["weight"]) for item in milestones if item["completed"])
        overall = float(project["progress_percent"] or 0)
        return {"progress_percent": overall, "milestone_progress": round(milestone_progress, 1),
                "daily_progress": daily_progress, "milestones": milestones}
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
    overall = 100.0 if project["status"] == "completed" else round(min(100.0, milestone_progress + daily_progress * 0.4), 1)
    db.execute("UPDATE projects SET milestones_json=?,progress_percent=?,updated_at=? WHERE project_id=?",
               (json.dumps(milestones), overall, _now(), project_id))
    return {"progress_percent": overall, "milestone_progress": round(milestone_progress, 1),
            "daily_progress": daily_progress, "milestones": milestones}


def _project_due_status(project: dict) -> dict:
    if project.get("status") == "completed":
        return {"due_status": "complete", "days_remaining": 0, "due_label": "Completed"}
    if project.get("status") == "terminated":
        return {"due_status": "terminated", "days_remaining": None, "due_label": "Terminated / archived"}
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
    try:
        intake = db.execute("SELECT project_location,project_category,estimated_budget,expected_duration_days,priority,planned_purchase_requests FROM project_intake WHERE project_id=?", (project_id,)).fetchone()
    except Exception:
        intake = None
    project["project_location"] = intake["project_location"] if intake else ""
    project["project_category"] = intake["project_category"] if intake else ""
    project["estimated_budget"] = intake["estimated_budget"] if intake else 0
    project["expected_duration_days"] = intake["expected_duration_days"] if intake else 30
    project["priority"] = intake["priority"] if intake else "medium"
    project["planned_purchase_requests"] = intake["planned_purchase_requests"] if intake else 0
    project["milestones"] = progress["milestones"]
    project["progress_percent"] = progress["progress_percent"]
    project["progress_breakdown"] = {"milestones": progress["milestone_progress"], "daily_updates": round(progress["daily_progress"] * 0.4, 1)}
    project.update(_project_due_status(project))
    closure = db.execute("SELECT completed_at,closed_by,completion_note,progress_at_close FROM project_closures WHERE project_id=?", (project_id,)).fetchone()
    project["completed_at"] = closure["completed_at"] if closure else None
    project["closed_by"] = closure["closed_by"] if closure else None
    project["completion_note"] = closure["completion_note"] if closure else ""
    project["progress_at_close"] = closure["progress_at_close"] if closure else None
    termination = db.execute("SELECT terminated_at,terminated_by,termination_note FROM project_terminations WHERE project_id=?", (project_id,)).fetchone() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_terminations'").fetchone() else None
    project["terminated_at"] = termination["terminated_at"] if termination else None
    project["terminated_by"] = termination["terminated_by"] if termination else None
    project["termination_note"] = termination["termination_note"] if termination else ""
    project["boq_items_count"] = db.execute("SELECT COUNT(*) FROM workflow_boq_items WHERE project_id=?", (project_id,)).fetchone()[0]
    project["quotations_count"] = db.execute("SELECT COUNT(*) FROM workflow_quotations WHERE project_id=?", (project_id,)).fetchone()[0]
    project["purchase_orders_count"] = db.execute("SELECT COUNT(*) FROM workflow_purchase_orders WHERE project_id=?", (project_id,)).fetchone()[0]
    project["purchase_requests"] = [dict(item) for item in db.execute("""SELECT r.id,r.code,r.material,r.category,r.quantity,r.unit,r.site,r.required_by,r.priority,r.status,r.created_at
        FROM project_purchase_requests pr JOIN requests r ON r.id=pr.request_id
        WHERE pr.project_id=? ORDER BY pr.linked_at DESC""", (project_id,)).fetchall()]
    project["purchase_requests_count"] = len(project["purchase_requests"])
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
async def login(request: Request, response: Response):
    # Accept both the normal JSON/AJAX request and a safe POST form fallback.
    # Never allow browser-native form submission to put credentials in the URL.
    content_type = request.headers.get("content-type", "").lower()
    is_form_submission = "application/json" not in content_type
    try:
        if not is_form_submission:
            submitted = await request.json()
        else:
            raw_form = (await request.body()).decode("utf-8", errors="replace")
            submitted = {key: values[-1] for key, values in urllib.parse.parse_qs(raw_form, keep_blank_values=True).items()}
        payload = LoginPayload(**submitted)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Enter your username and password, then try again.")
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
    if is_form_submission:
        response.status_code = 303
        response.headers["Location"] = "/"
        return response
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


@app.post("/api/v1/workflows/quotations/{quotation_id}/technical-review", dependencies=[Depends(verify_api_key)])
def review_workflow_quotation_technical_compliance(quotation_id: str, payload: WorkflowTechnicalReviewPayload):
    now = _now()
    notes = payload.notes.strip()
    if payload.status in ("compliant", "deviation", "non_compliant") and len(notes) < 10:
        raise HTTPException(status_code=422, detail="Record at least 10 characters describing specification checks, evidence or deviations.")
    with connect() as db:
        quote = db.execute("SELECT quotation_id FROM workflow_quotations WHERE quotation_id=?", (quotation_id,)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        if db.execute("SELECT 1 FROM workflow_purchase_orders WHERE quotation_id=? LIMIT 1", (quotation_id,)).fetchone():
            raise HTTPException(status_code=409, detail="This quotation is locked by a purchase-order proposal.")
        db.execute("""UPDATE workflow_quotations SET technical_status=?,technical_notes=?,technical_reviewed_by=?,technical_reviewed_at=?
            WHERE quotation_id=?""",
            (payload.status, notes, "API administrator", now if payload.status != "pending" else None, quotation_id))
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            ("API administrator", "technical_review", "workflow_quotation", quotation_id,
             json.dumps({"status": payload.status, "notes": notes}), now))
    return {"success": True, "quotation_id": quotation_id, "technical_status": payload.status,
            "reviewed_by": "API administrator", "reviewed_at": now if payload.status != "pending" else None}


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
         "verification_status":q["verification_status"],"technical_status":q["technical_status"],
         "technical_notes":q["technical_notes"],"total_amount":q["total_amount"],"currency":q["currency"],
         "delivery_days":q["delivery_days"],"variance_from_lowest":round(q["total_amount"]-low,2),
         "line_items":json.loads(q["line_items_json"])} for q in offers]}


@app.post("/api/v1/workflows/recommend-supplier", dependencies=[Depends(verify_api_key)])
def recommend_supplier(payload: RecommendationRequest):
    with connect() as db:
        _require_buyer_confirmation(db, payload.project_id)
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
    eligible = [item for item in ranked
                if item.get("verification_status") == "verified" and item.get("technical_status") == "compliant"]
    with connect() as db:
        if db.execute("SELECT 1 FROM projects WHERE project_id=?", (payload.project_id,)).fetchone():
            _complete_project_milestone(db, payload.project_id, "bid_evaluation", "Supplier comparison workflow")
            _recalculate_project(db, payload.project_id)
    return {"success":True,"recommended":eligible[0] if eligible else None,"ranking":ranked,
            "recommendation_note":"Only quotations with verified suppliers and recorded technical compliance are eligible for recommendation. If none qualify, resolve the listed checks before award.",
            "disclaimer":"Decision support only. Verify quote validity, scope, tax, freight and supplier capacity. A human approval is required before award."}


@app.post("/api/v1/workflows/approve-award/{quotation_id}", dependencies=[Depends(verify_api_key)])
def create_award_proposal(quotation_id: str, requested_by: str = Form(...)):
    now, po = _now(), "PO-" + datetime.utcnow().strftime("%Y") + "-" + uuid.uuid4().hex[:8].upper()
    with connect() as db:
        quote = db.execute("SELECT * FROM workflow_quotations WHERE quotation_id=?", (quotation_id,)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        _require_buyer_confirmation(db, quote["project_id"])
        _require_verified_workflow_supplier(db, quote["supplier_id"])
        if quote["technical_status"] != "compliant":
            raise HTTPException(status_code=409, detail="Technical compliance must be reviewed and marked compliant before creating a purchase-order proposal.")
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
        pending = db.execute("""SELECT po.*,q.technical_status FROM workflow_purchase_orders po
            JOIN workflow_quotations q ON q.quotation_id=po.quotation_id
            WHERE po.po_number=? AND po.status='pending_approval'""", (po_number,)).fetchone()
        if not pending:
            raise HTTPException(status_code=409, detail="Purchase order is not awaiting approval")
        if status == "approved":
            _require_buyer_confirmation(db, pending["project_id"])
            if pending["technical_status"] != "compliant":
                raise HTTPException(status_code=409, detail="Technical compliance must remain approved at the point of purchase-order approval.")
            project = db.execute("SELECT status FROM projects WHERE project_id=?", (pending["project_id"],)).fetchone()
            if project and project["status"] in ("completed", "terminated", "cancelled"):
                raise HTTPException(status_code=409, detail="This project is closed or archived. Purchase-order approval is blocked.")
            supplier = db.execute("SELECT verification_status FROM workflow_suppliers WHERE supplier_id=?", (pending["supplier_id"],)).fetchone()
            if not supplier or supplier["verification_status"] != "verified":
                raise HTTPException(status_code=409, detail="Supplier must remain verified at the point of purchase-order approval.")
        cur = db.execute("""UPDATE workflow_purchase_orders SET status=?,decided_by=?,decision_note=?,decided_at=?
            WHERE po_number=? AND status='pending_approval'""",
            (status,payload.decided_by,payload.note,now,po_number))
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="Purchase order is no longer awaiting approval")
        row = db.execute("SELECT * FROM workflow_purchase_orders WHERE po_number=?", (po_number,)).fetchone()
        if status == "approved":
            _complete_project_milestone(db, row["project_id"], "po_award", "Human-approved purchase order")
            _recalculate_project(db, row["project_id"])
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (payload.decided_by, "approve" if status == "approved" else "reject", "workflow_purchase_order", po_number, json.dumps({"status":status,"note":payload.note,"supplier_id":pending["supplier_id"],"project_id":pending["project_id"]}), now))
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
    unit: str = Field(min_length=1, max_length=40)
    site: str
    required_by: str
    priority: str = "Normal"
    project_id: str = Field(min_length=1, max_length=80)
    additional_information: str = Field(default="", max_length=8000)


class DashboardQuotePayload(BaseModel):
    supplier_id: int
    request_id: int
    unit_price: float = Field(gt=0)
    freight: float = Field(ge=0)
    tax_rate: float = Field(ge=0, le=100)
    lead_days: int = Field(ge=0)
    payment_days: int = Field(ge=0)
    note: str = ""


class QuoteParsePayload(BaseModel):
    message: str = Field(min_length=3, max_length=12000)


class PurchaseRequestParsePayload(BaseModel):
    message: str = Field(min_length=3, max_length=12000)


def _extract_quote_line_items(message: str) -> list[dict]:
    """Extract independently priced material lines without creating quotations."""
    items = []
    unit_pattern = r"(kgs?|kilograms?|grams?|g|tonnes?|tons?|mt|litres?|liters?|l|metres?|meters?|m|pieces?|pcs|units?|bags?|boxes?|cartons?|packs?|sets?|rolls?|sheets?|pairs?|drums?|pallets?)"
    quantity_re = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*" + unit_pattern + r"\b", re.I)
    price_re = re.compile(
        r"(?:at|@|for|unit\s+price(?:\s+is)?)\s*(?:₦\s*|NGN\s*|N\s*)?([\d,]+(?:\.\d{1,2})?)\s*(?:naira\s*)?(?:per|/)\s*" + unit_pattern + r"\b",
        re.I,
    )
    compact_price_re = re.compile(
        r"(?:₦\s*|NGN\s*|N\s*)?([\d,]+(?:\.\d{1,2})?)\s*/\s*" + unit_pattern + r"\b",
        re.I,
    )
    for raw_line in message.splitlines():
        line = " ".join(raw_line.replace("\u00a0", " ").split())
        quantity = quantity_re.search(line)
        if not quantity:
            continue
        price = price_re.search(line) or compact_price_re.search(line)
        if not price:
            continue
        description = line[:quantity.start()] + " " + line[quantity.end():price.start()] + " " + line[price.end():]
        description = re.sub(r"^\s*(?:[-*•]+\s*)?(?:i can do|i can|we can|we offer|we have|we supply|can supply|supply|quote for|quotation for)\s+", "", description, flags=re.I)
        description = re.split(r"\b(?:delivery|lead\s*time|transport|freight|vat|tax|payment|validity|available)\b", description, maxsplit=1, flags=re.I)[0]
        description = re.sub(r"^\s*(?:of|for|:|-)+\s*|\s*(?:of|for|at|:|-)+\s*$", "", description).strip(" .,:;-–—")
        items.append({
            "description": description or f"Line item {len(items) + 1}",
            "quantity": float(quantity.group(1).replace(",", "")),
            "unit": quantity.group(2).lower(),
            "unit_price": float(price.group(1).replace(",", "")),
            "price_unit": price.group(2).lower(),
            "source_line": line,
        })
    return items[:50]


def _parse_supplier_quote_message(message: str) -> dict:
    """Conservative, rule-based extraction. Never invent values."""
    text = " ".join(message.replace("\u00a0", " ").split())
    result = {"quantity": None, "unit": None, "unit_price": None, "freight": None,
              "tax_rate": None, "lead_days": None, "payment_days": None,
              "payment_terms": None, "matched": [], "line_items": _extract_quote_line_items(message)}
    unit_pattern = r"(kgs?|kilograms?|grams?|g|tonnes?|tons?|mt|litres?|liters?|l|metres?|meters?|m|pieces?|pcs|units?|bags?|boxes?|cartons?|packs?|sets?)"
    quantity_match = re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s*" + unit_pattern + r"\b", text, re.I)
    if quantity_match:
        result["quantity"] = float(quantity_match.group(1).replace(",", ""))
        result["unit"] = quantity_match.group(2).lower()
        result["matched"].append("quantity")

    money = r"(?:₦\s*|NGN\s*|N\s*)?([\d,]+(?:\.\d{1,2})?)"
    unit_price_patterns = [
        r"(?:at|@|for)\s*" + money + r"\s*(?:naira\s*)?(?:per|/)\s*" + unit_pattern,
        r"(?:unit\s+price|price\s+per\s+unit)\s*(?:is|of|:)?\s*" + money,
        r"(?:rate)\s*(?:is|of|:)?\s*" + money + r"\s*(?:per|/)\s*" + unit_pattern
    ]
    for pattern in unit_price_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            result["unit_price"] = float(match.group(1).replace(",", ""))
            result["matched"].append("unit_price")
            break

    freight_match = re.search(r"(?:transport|freight|delivery\s+(?:fee|cost|charge))\s*(?:is|of|:)?\s*" + money, text, re.I)
    if freight_match:
        result["freight"] = float(freight_match.group(1).replace(",", ""))
        result["matched"].append("freight")
    tax_match = re.search(r"(?:VAT|tax)\s*(?:is|at|of|:)?\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
    if tax_match:
        result["tax_rate"] = float(tax_match.group(1))
        result["matched"].append("tax_rate")
    lead_match = re.search(r"(?:delivery\s+in|delivery|lead\s*time|within|in)\s*(\d+)\s*(working\s+days?|days?|weeks?)\b", text, re.I)
    if lead_match:
        amount = int(lead_match.group(1))
        result["lead_days"] = amount * 7 if lead_match.group(2).lower().startswith("week") else amount
        result["matched"].append("lead_days")
    payment_match = re.search(r"(?:payment\s+terms?\s*[:=]?\s*)?(\d{1,3})\s*%\s*(?:upfront|advance|deposit)", text, re.I)
    if payment_match:
        result["payment_terms"] = payment_match.group(0)
        result["matched"].append("payment_terms")
    else:
        credit_match = re.search(r"(?:payment\s+terms?\s*[:=]?\s*)?(\d+)\s*(days?|weeks?)\s*(?:credit|to\s+pay|payment)", text, re.I)
        if credit_match:
            amount = int(credit_match.group(1))
            result["payment_days"] = amount * 7 if credit_match.group(2).lower().startswith("week") else amount
            result["matched"].append("payment_days")
    return result


class DashboardOrderPayload(BaseModel):
    request_id: int
    supplier_id: int

class AnalysisPurchaseOrderPayload(BaseModel):
    quote_id: int = Field(gt=0)



class DashboardDecisionPayload(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")
    note: str = ""


class TechnicalReviewPayload(BaseModel):
    status: str = Field(pattern="^(pending|compliant|deviation|non_compliant)$")
    notes: str = Field(default="", max_length=5000)


@app.get("/api/requests")
def dashboard_requests():
    return {"requests": dashboard_module.rows("SELECT * FROM requests ORDER BY id DESC")}


@app.put("/api/requests/{request_id}")
def dashboard_update_request(request_id: int, payload: DashboardRequestPayload, request: Request):
    try:
        date.fromisoformat(payload.required_by)
    except ValueError:
        raise HTTPException(status_code=422, detail="required_by must be an ISO date")
    now = _now()
    with dashboard_module.connect() as db:
        current = db.execute("""SELECT r.*, p.project_id AS linked_project_id, p.status AS project_status
            FROM requests r JOIN project_purchase_requests ppr ON ppr.request_id=r.id
            JOIN projects p ON p.project_id=ppr.project_id WHERE r.id=?""", (request_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Project purchase request not found")
        if current["project_status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is closed. Its purchase requests are read-only.")
        if payload.project_id != current["linked_project_id"]:
            raise HTTPException(status_code=409, detail="A purchase request cannot be moved to another project while editing.")
        before = {key: current[key] for key in ("material","category","quantity","unit","site","required_by","priority","additional_information")}
        db.execute("""UPDATE requests SET material=?,category=?,quantity=?,unit=?,site=?,required_by=?,priority=?,additional_information=? WHERE id=?""",
            (payload.material.strip(),payload.category,payload.quantity,payload.unit.strip(),payload.site.strip(),
             payload.required_by,payload.priority,payload.additional_information.strip(),request_id))
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        after = {"material":payload.material.strip(),"category":payload.category,"quantity":payload.quantity,
            "unit":payload.unit.strip(),"site":payload.site.strip(),"required_by":payload.required_by,
            "priority":payload.priority,"additional_information":payload.additional_information.strip()}
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (getattr(request.state, "auth_user", ""), "update", "purchase_request", str(request_id),
             json.dumps({"before":before,"after":after}, ensure_ascii=False), now))
        db.execute("UPDATE projects SET updated_at=? WHERE project_id=?", (now,current["linked_project_id"]))
    return {"success":True,"id":request_id,"code":current["code"],"project_id":current["linked_project_id"]}


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


@app.delete("/api/suppliers/{supplier_id}")
def dashboard_delete_supplier(supplier_id: int, request: Request):
    with dashboard_module.connect() as db:
        current = db.execute("SELECT id,name,city FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Seller not found")
        # Preserve procurement history: a supplier with quotations or purchase orders cannot be hard-deleted.
        quote_count = db.execute("SELECT COUNT(*) FROM quotes WHERE supplier_id=?", (supplier_id,)).fetchone()[0]
        if quote_count:
            order_count = db.execute("""SELECT COUNT(*) FROM orders o
                JOIN quotes q ON q.id=o.quote_id WHERE q.supplier_id=?""", (supplier_id,)).fetchone()[0]
            raise HTTPException(status_code=409, detail=f"This seller has {quote_count} linked quotation(s) and {order_count} purchase-order record(s). Keep the seller record to preserve procurement history.")
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (getattr(request.state, "auth_user", ""), "delete", "supplier", str(supplier_id),
             json.dumps({"name": current["name"], "city": current["city"]}), _now()))
        db.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
    return {"success": True, "deleted_supplier": current["name"]}


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


@app.get("/api/requests/{request_id}")
def dashboard_request_detail(request_id: int):
    with dashboard_module.connect() as db:
        row = db.execute("""SELECT r.*, p.project_id, p.title AS project_title
            FROM requests r
            LEFT JOIN project_purchase_requests pr ON pr.request_id=r.id
            LEFT JOIN projects p ON p.project_id=pr.project_id
            WHERE r.id=?
            ORDER BY pr.linked_at DESC LIMIT 1""", (request_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Purchase request not found")
    return {"request": dict(row)}


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
        if not payload.project_id.strip():
            raise HTTPException(status_code=422, detail="Select a project before creating a purchase request")
        selected_project = db.execute("SELECT status FROM projects WHERE project_id=?", (payload.project_id,)).fetchone()
        if not selected_project:
            raise HTTPException(status_code=404, detail="Selected project was not found")
        if selected_project["status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is closed. Reopen it through an authorised process before adding requests.")
        next_id = db.execute("SELECT COALESCE(MAX(id),0)+1 FROM requests").fetchone()[0]
        code = f"PR-{next_id+141:04d}"
        created_at = _now()
        cur = db.execute("""INSERT INTO requests(code,material,category,quantity,unit,site,required_by,priority,status,created_at,additional_information)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (code,payload.material.strip(),payload.category,payload.quantity,payload.unit.strip(),payload.site.strip(),
             payload.required_by,payload.priority,"Open",created_at,payload.additional_information.strip()))
        request_id = cur.lastrowid
        db.execute("""INSERT INTO project_purchase_requests(project_id,request_id,linked_by,linked_at)
            VALUES(?,?,?,?)""",
            (payload.project_id,request_id,"Workspace user",created_at))
        db.execute("UPDATE projects SET updated_at=? WHERE project_id=?", (created_at,payload.project_id))
    try:
        rfq_result = dispatch_request_rfq(request_id, "ProcureAI")
    except Exception as exc:
        rfq_result = {"sent_count":0,"failed_count":0,"error":str(exc)[:240]}
    return {"success":True,"id":request_id,"code":code,"project_id":payload.project_id,"rfq_dispatch":rfq_result}


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

@app.post("/api/quotes/{quote_id}/technical-review")
def review_quote_technical_compliance(quote_id: int, payload: TechnicalReviewPayload, request: Request):
    now = _now()
    notes = payload.notes.strip()
    if payload.status in ("compliant", "deviation", "non_compliant") and len(notes) < 10:
        raise HTTPException(status_code=422, detail="Record at least 10 characters describing the specification checks, evidence or deviations.")
    with dashboard_module.connect() as db:
        quote = db.execute("SELECT id FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not quote:
            raise HTTPException(status_code=404, detail="Quotation not found")
        if db.execute("SELECT 1 FROM orders WHERE quote_id=? LIMIT 1", (quote_id,)).fetchone():
            raise HTTPException(status_code=409, detail="This quotation is locked by a purchase-order proposal.")
        db.execute("""UPDATE quotes SET technical_status=?,technical_notes=?,technical_reviewed_by=?,technical_reviewed_at=?
            WHERE id=?""",
            (payload.status, notes, getattr(request.state, "auth_user", ""), now if payload.status != "pending" else None, quote_id))
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (getattr(request.state, "auth_user", ""), "technical_review", "quotation", str(quote_id),
             json.dumps({"status": payload.status, "notes": notes}), now))
    return {"success": True, "technical_status": payload.status,
            "reviewed_by": getattr(request.state, "auth_user", ""),
            "reviewed_at": now if payload.status != "pending" else None}


@app.post("/api/quotes/{quote_id}/send-request")
def dashboard_send_supplier_request(quote_id: int, request: Request):
    with dashboard_module.connect() as db:
        _ensure_supplier_request_email_table(db)
        quote = db.execute("""SELECT q.id AS quote_id, q.supplier_id, q.request_id, s.name AS supplier_name, s.email AS supplier_email, s.status AS supplier_status, r.code AS request_code, r.material, r.category, r.quantity, r.unit, r.site, r.required_by, r.additional_information, r.status AS request_status FROM quotes q JOIN suppliers s ON s.id=q.supplier_id JOIN requests r ON r.id=q.request_id WHERE q.id=?""", (quote_id,)).fetchone()
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

def _parse_purchase_request_message(message: str) -> dict:
    """Conservatively extract request fields from informal buyer instructions."""
    raw = message.strip()
    text = " ".join(raw.replace("\u00a0", " ").split())
    result = {"material": None, "quantity": None, "unit": None, "category": None,
              "site": None, "required_by": None, "priority": None,
              "additional_information": raw}
    unit_map = {
        "each":"ea","ea":"ea","piece":"pcs","pieces":"pcs","pcs":"pcs","unit":"unit","units":"unit","qty":"qty",
        "pair":"pair","pairs":"pair","set":"set","sets":"set","dozen":"dozen","kg":"kg","kgs":"kg",
        "kilogram":"kg","kilograms":"kg","g":"g","gram":"g","grams":"g","tonne":"tonne","tonnes":"tonnes",
        "ton":"tonne","tons":"tonnes","mm":"mm","millimetre":"mm","millimetres":"mm","cm":"cm",
        "m":"m","metre":"m","metres":"m","meter":"m","meters":"m","km":"km","litre":"litre",
        "litres":"litre","liter":"litre","liters":"litre","l":"litre","ml":"ml","pack":"pack","packs":"pack",
        "box":"box","boxes":"box","carton":"carton","cartons":"carton","roll":"roll","rolls":"roll",
        "drum":"drum","drums":"drum","pallet":"pallet","pallets":"pallet","ream":"ream","reams":"ream",
        "hour":"hour","hours":"hour","day":"day","days":"day","trip":"trip","trips":"trip","lot":"lot",
        "job":"job","jobs":"job","bag":"bag","bags":"bag"
    }
    request_core = re.split(r"\b(?:required within|delivery in|deliver within|needed within|required by|needed by)\b", text, maxsplit=1, flags=re.I)[0]
    unit_pattern = "|".join(sorted((re.escape(k) for k in unit_map), key=len, reverse=True))
    quantity_match = re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s*(" + unit_pattern + r")\b", request_core, re.I)
    if quantity_match:
        result["quantity"] = float(quantity_match.group(1).replace(",", ""))
        result["unit"] = unit_map[quantity_match.group(2).lower()]
        before = request_core[:quantity_match.start()].strip(" ,:-")
        after = request_core[quantity_match.end():].strip(" ,:-")
        before = re.sub(r"^(?:(?:we|i)\s+)?(?:need|require|want|request|procure|purchase|supply|order)(?:\s+for)?\s+", "", before, flags=re.I)
        after = re.split(r"[,;\n]|\b(?:at|in|for delivery|deliver to|required by|needed by|within)\b", after, maxsplit=1, flags=re.I)[0].strip(" ,:-")
        candidate = before if before else after
        candidate = re.sub(r"^(?:of|for|a|an|the)\s+", "", candidate, flags=re.I).strip(" .,:;-")
        if candidate and len(candidate) <= 180:
            result["material"] = candidate
    else:
        count_match = re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s+(?=(?:main|distribution|panel|board|cable|breaker|motor|pump|transformer|switchgear|conduit|tray|light|lamp|socket|generator|valve|pipe|equipment|installation)\b)", request_core, re.I)
        if count_match:
            result["quantity"] = float(count_match.group(1).replace(",", ""))
            result["unit"] = "ea"
            candidate = request_core[count_match.end():]
            candidate = re.split(r"[,;\n]|\b(?:at|in|for delivery|deliver to|required by|needed by|within)\b", candidate, maxsplit=1, flags=re.I)[0].strip(" ,:-")
            candidate = re.sub(r"^(?:of|for|a|an|the)\s+", "", candidate, flags=re.I).strip(" .,:;-")
            if candidate and len(candidate) <= 180:
                result["material"] = candidate
        if not result["material"]:
            material_match = re.search(r"\b(?:we need|we require|please supply|need|require|procure|purchase)\s+(?:a|an|the)?\s*([^,;\n.]{3,120})", request_core, re.I)
            if material_match:
                result["material"] = material_match.group(1).strip()
    lower = text.lower()
    category_terms = [
        ("Electrical",("electrical","distribution panel","mccb","circuit breaker","cable","switchgear","transformer")),
        ("Plumbing",("plumbing","pipe","valve","fitting","pump")),
        ("Reinforcement",("reinforcement","rebar","steel rod","tmt")),
        ("Cementitious",("cement","concrete","mortar")),
        ("Aggregate",("aggregate","granite","sharp sand","stone chippings")),
        ("Formwork",("formwork","shuttering","plywood"))
    ]
    for category, terms in category_terms:
        if any(term in lower for term in terms):
            result["category"] = category
            break
    site_match = re.search(r"\b(?:site|location|workshop)\s*(?:is|:|at)?\s+([^,;\n.]{2,100})", text, re.I)
    if site_match:
        result["site"] = re.sub(r"^at\s+", "", site_match.group(1).strip(), flags=re.I)
    date_match = re.search(r"\b(?:required by|needed by|delivery date|deliver by)\s*(?:is|:)?\s*(\d{4}-\d{2}-\d{2})\b", text, re.I)
    if date_match:
        try:
            date.fromisoformat(date_match.group(1))
            result["required_by"] = date_match.group(1)
        except ValueError:
            pass
    if re.search(r"\b(urgent|asap|immediately|emergency)\b", lower):
        result["priority"] = "Urgent"
    elif re.search(r"\b(normal priority|standard priority)\b", lower):
        result["priority"] = "Normal"
    return result


@app.post("/api/requests/parse")
def dashboard_parse_purchase_request(payload: PurchaseRequestParsePayload):
    """Extract clearly stated purchase-request details without creating a record."""
    return {"success": True, "extracted": _parse_purchase_request_message(payload.message)}


@app.post("/api/quotes/parse")
def dashboard_parse_quote(payload: QuoteParsePayload):
    """Extract clearly stated commercial values from an informal supplier message."""
    return {"success": True, "extracted": _parse_supplier_quote_message(payload.message)}


@app.post("/api/quotes", status_code=201)
def dashboard_create_quote(payload: DashboardQuotePayload):
    with dashboard_module.connect() as db:
        # Serialize the duplicate check and insert so simultaneous manual submissions
        # cannot create two quotations for the same supplier/request pair.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM requests WHERE id=?", (payload.request_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Purchase request not found")
        if not db.execute("SELECT 1 FROM suppliers WHERE id=?", (payload.supplier_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Supplier not found")
        if db.execute("SELECT 1 FROM quotes WHERE request_id=? AND supplier_id=?", (payload.request_id,payload.supplier_id)).fetchone():
            raise HTTPException(status_code=409, detail="This supplier already has a quotation recorded for this purchase request. Open the existing quotation instead of entering it again.")
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
        duplicate = db.execute("SELECT id FROM quotes WHERE request_id=? AND supplier_id=? AND id<>?", (payload.request_id,payload.supplier_id,quote_id)).fetchone()
        if duplicate: raise HTTPException(status_code=409, detail="This supplier already has another quotation recorded for this purchase request.")
        before = {key: current[key] for key in ("supplier_id","request_id","unit_price","freight","tax_rate","lead_days","payment_days","note")}
        db.execute("""UPDATE quotes SET supplier_id=?,request_id=?,unit_price=?,freight=?,tax_rate=?,lead_days=?,payment_days=?,note=?,technical_status='pending',technical_notes='',technical_reviewed_by='',technical_reviewed_at=NULL WHERE id=?""",
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
        _require_request_buyer_confirmation(db, int(quote["request_id"]))
        supplier_status = (quote["supplier_status"] or "").strip().casefold()
        if supplier_status not in {"verified", "active"} or "[fictional demo]" in quote["supplier_name"].casefold():
            raise HTTPException(status_code=409, detail="This seller is not verified for purchase-order generation. Verify the seller first.")
        if quote["technical_status"] != "compliant":
            raise HTTPException(status_code=409, detail="Technical compliance must be reviewed and marked compliant before creating a purchase-order proposal.")
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
def dashboard_create_order(payload: DashboardOrderPayload, request: Request):
    with dashboard_module.connect() as db:
        req = db.execute("SELECT * FROM requests WHERE id=?", (payload.request_id,)).fetchone()
        quote = db.execute("SELECT * FROM quotes WHERE request_id=? AND supplier_id=? ORDER BY id DESC LIMIT 1",
                           (payload.request_id,payload.supplier_id)).fetchone()
        supplier = db.execute("SELECT * FROM suppliers WHERE id=?", (payload.supplier_id,)).fetchone()
        if not req or not quote or not supplier:
            raise HTTPException(status_code=404, detail="A matching request, supplier and quotation are required")
        _require_request_buyer_confirmation(db, int(payload.request_id))
        supplier_status = str(supplier["status"] or "").strip().casefold()
        if supplier_status not in {"verified", "active"} or "[fictional demo]" in str(supplier["name"]).casefold():
            raise HTTPException(status_code=409, detail="This seller is not verified for purchase-order generation.")
        if quote["technical_status"] != "compliant":
            raise HTTPException(status_code=409, detail="Technical compliance must be reviewed and marked compliant before creating a purchase-order proposal.")
        if req["status"] != "Open":
            raise HTTPException(status_code=409, detail="This purchase request is no longer open.")
        _ensure_supplier_request_email_table(db)
        if not db.execute("SELECT 1 FROM supplier_request_emails WHERE quote_id=?", (quote["id"],)).fetchone():
            raise HTTPException(status_code=409, detail="Email the purchase request to this seller and receive a successful send confirmation before generating the PO proposal.")
        if db.execute("SELECT 1 FROM orders WHERE quote_id=?", (quote["id"],)).fetchone():
            raise HTTPException(status_code=409, detail="A purchase order proposal already exists for this quotation.")
        qty, price = Decimal(str(req["quantity"])), Decimal(str(quote["unit_price"]))
        tax = qty * price * Decimal(str(quote["tax_rate"])) / Decimal("100")
        freight = Decimal(str(quote["freight"]))
        total = qty * price + tax + freight
        reasons = ["Human approval required before any purchase commitment"]
        if total > Decimal("2500000"): reasons.append("Order exceeds the configured review threshold")
        if float(supplier["rating"] or 0) < 3.5: reasons.append("Supplier rating is below the preferred threshold")
        if int(quote["lead_days"]) > max(0,(date.fromisoformat(req["required_by"])-date.today()).days):
            reasons.append("Quoted lead time misses the requested delivery date")
        count = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        code = f"PO-{datetime.utcnow():%y%m%d}-{count+1:03d}"
        cur = db.execute("""INSERT INTO orders(code,supplier_id,request_id,quantity,unit_price,freight,tax,total,status,required_by,created_at,decision_note,quote_id,analysis_estimate,lead_days,payment_days)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code,payload.supplier_id,payload.request_id,float(qty),float(price),float(freight),float(tax),
             float(total),"Pending approval",req["required_by"],datetime.utcnow().isoformat(),"; ".join(reasons),
             int(quote["id"]),float(total),int(quote["lead_days"]),int(quote["payment_days"])))
    return {"success":True,"id":cur.lastrowid,"code":code,"status":"Pending approval","total":float(total),"reasons":reasons}


@app.post("/api/orders/{order_id}/decision")
def dashboard_decide_order(order_id: int, payload: DashboardDecisionPayload, request: Request):
    status = "Approved" if payload.action == "approve" else "Rejected"
    with dashboard_module.connect() as db:
        order = db.execute("""SELECT o.request_id,o.supplier_id,s.status AS supplier_status,s.name AS supplier_name,
            q.technical_status
            FROM orders o JOIN suppliers s ON s.id=o.supplier_id
            LEFT JOIN quotes q ON q.id=o.quote_id
            WHERE o.id=? AND o.status='Pending approval'""", (order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=409, detail="Purchase order is not awaiting approval")
        if payload.action == "approve":
            _require_request_buyer_confirmation(db, int(order["request_id"]))
            if str(order["supplier_status"] or "").strip().casefold() not in {"verified", "active"} or "[fictional demo]" in str(order["supplier_name"]).casefold():
                raise HTTPException(status_code=409, detail="Supplier verification must remain valid at the point of approval.")
            if order["technical_status"] != "compliant":
                raise HTTPException(status_code=409, detail="Technical compliance must remain approved at the point of purchase-order approval.")
        cur = db.execute("UPDATE orders SET status=?,decision_note=? WHERE id=? AND status='Pending approval'",
                         (status,payload.note,order_id))
        if cur.rowcount != 1:
            raise HTTPException(status_code=409, detail="Purchase order is no longer awaiting approval")
        if payload.action == "approve":
            db.execute("UPDATE requests SET status='Awarded' WHERE id=?",(order["request_id"],))
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (getattr(request.state,"auth_user",""), "approve" if payload.action=="approve" else "reject", "purchase_order", str(order_id), json.dumps({"status":status,"note":payload.note}), _now()))
    return {"success":True,"status":status}


# Reuse the existing dashboard database and calculations; the browser UI is served by this API.

def _ensure_invoice_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL UNIQUE,
        invoice_number TEXT NOT NULL UNIQUE,
        issue_date TEXT NOT NULL,
        due_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Unpaid',
        created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")

@app.get("/api/invoices")
def dashboard_invoices():
    with dashboard_module.connect() as db:
        _ensure_invoice_table(db)
        rows = db.execute("""SELECT i.id,i.order_id,i.invoice_number,i.issue_date,i.due_date,i.status,i.created_at,
            o.code AS order_code,o.total,r.material,o.quantity,r.unit,s.name AS supplier
            FROM invoices i JOIN orders o ON o.id=i.order_id
            JOIN requests r ON r.id=o.request_id JOIN suppliers s ON s.id=o.supplier_id
            ORDER BY i.id DESC""").fetchall()
    return {"invoices": [dict(row) for row in rows]}

@app.post("/api/orders/{order_id}/invoice", status_code=201)
def dashboard_create_invoice(order_id: int, request: Request):
    now = datetime.utcnow().isoformat()
    with dashboard_module.connect() as db:
        _ensure_invoice_table(db)
        order = db.execute("SELECT id,code,status,payment_days FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Purchase order not found")
        if order["status"] != "Approved":
            raise HTTPException(status_code=409, detail="An invoice can only be generated from an approved purchase order.")
        existing = db.execute("SELECT id,invoice_number FROM invoices WHERE order_id=?", (order_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail=f"Invoice {existing['invoice_number']} already exists for this purchase order.")
        count = db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        issue_date = date.today()
        due_date = issue_date + timedelta(days=max(0, int(order["payment_days"] or 0)))
        invoice_number = f"INV-{datetime.utcnow():%y%m%d}-{count+1:03d}"
        cur = db.execute("""INSERT INTO invoices
            (order_id,invoice_number,issue_date,due_date,status,created_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (order_id,invoice_number,issue_date.isoformat(),due_date.isoformat(),"Unpaid",
             getattr(request.state,"auth_user",""),now,now))
        invoice_id = cur.lastrowid
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (getattr(request.state,"auth_user",""),"create","invoice",str(invoice_id),
             json.dumps({"invoice_number":invoice_number,"order_code":order["code"],"total":None}),now))
    return {"success":True,"invoice_id":invoice_id,"invoice_number":invoice_number,"status":"Unpaid"}

@app.get("/api/invoices/{invoice_id}")
def dashboard_invoice_detail(invoice_id: int):
    with dashboard_module.connect() as db:
        _ensure_invoice_table(db)
        row = db.execute("""SELECT i.id AS invoice_id,i.invoice_number,i.issue_date,i.due_date,i.status AS invoice_status,
            i.created_at AS invoice_created_at,o.id AS order_id,o.code AS order_code,o.quantity,o.unit_price,
            o.freight,o.tax,o.total,o.required_by,o.status AS order_status,o.lead_days,o.payment_days,
            s.name AS supplier,s.email AS supplier_email,s.phone AS supplier_phone,
            s.contact AS supplier_contact,s.address AS supplier_address,s.city AS supplier_city,
            r.code AS request_code,r.material,r.category,r.unit,r.site,r.priority,
            COALESCE((SELECT company_name FROM company_profile WHERE id=1),'Gestro Global Consult Ltd') AS buyer_name
            FROM invoices i JOIN orders o ON o.id=i.order_id
            JOIN suppliers s ON s.id=o.supplier_id JOIN requests r ON r.id=o.request_id
            WHERE i.id=?""",(invoice_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404,detail="Invoice not found")
    return {"invoice":dict(row)}

@app.post("/api/invoices/{invoice_id}/paid")
def dashboard_mark_invoice_paid(invoice_id: int, request: Request):
    now = datetime.utcnow().isoformat()
    with dashboard_module.connect() as db:
        _ensure_invoice_table(db)
        cur = db.execute("UPDATE invoices SET status='Paid',updated_at=? WHERE id=? AND status!='Paid'",(now,invoice_id))
        if cur.rowcount != 1:
            row = db.execute("SELECT status FROM invoices WHERE id=?",(invoice_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404,detail="Invoice not found")
            raise HTTPException(status_code=409,detail="This invoice is already marked as paid.")
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (getattr(request.state,"auth_user",""),"mark_paid","invoice",str(invoice_id),"{}",now))
    return {"success":True,"status":"Paid"}

def _database_inventory(path: Path) -> dict:
    """Read aggregate table counts from a SQLite database without modifying it."""
    if not path.is_file():
        return {"integrity": "missing", "table_count": 0, "row_count": 0, "table_counts": {}}
    tracked_tables = (
        "projects", "workflow_projects", "project_updates", "project_quotations",
        "requests", "quotes", "orders", "suppliers", "workflow_suppliers",
        "workflow_quotations", "workflow_purchase_orders", "invoices", "audit_events",
    )
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=30) as db:
        integrity_row = db.execute("PRAGMA integrity_check").fetchone()
        table_names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        counts = {}
        for table in tracked_tables:
            if table in table_names:
                safe_name = table.replace('"', '""')
                counts[table] = int(db.execute(f'SELECT COUNT(*) FROM "{safe_name}"').fetchone()[0])
            else:
                counts[table] = 0
        return {
            "integrity": integrity_row[0] if integrity_row else "unknown",
            "table_count": len(table_names),
            "row_count": sum(counts.values()),
            "table_counts": counts,
        }


_RECOVERY_COMPARE_TABLES = (
    "projects", "workflow_projects", "project_updates", "project_quotations",
    "requests", "quotes", "orders", "suppliers", "workflow_suppliers",
    "workflow_quotations", "workflow_purchase_orders", "invoices",
)


def _restore_database_backup(filename: str) -> dict:
    """Restore a verified, data-richer backup after preserving the live database snapshot."""
    backup_path = _resolve_database_backup(filename)
    verification = _verify_database_backup(filename)
    live_path = _database_file()
    current_inventory = _database_inventory(live_path)
    backup_inventory = _database_inventory(backup_path)
    if backup_inventory["integrity"] != "ok":
        raise RuntimeError("Selected backup failed SQLite integrity verification")
    current_counts = current_inventory["table_counts"]
    backup_counts = backup_inventory["table_counts"]
    improved = [table for table in _RECOVERY_COMPARE_TABLES
                if backup_counts.get(table, 0) > current_counts.get(table, 0)]
    if not improved:
        raise ValueError("This backup does not contain more records in the tracked procurement tables than the live database.")

    # Keep a full, independent snapshot of the current live database before restoring.
    snapshot_dir = live_path.parent / "recovery-snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    snapshot_path = snapshot_dir / f"before-restore-{stamp}.sqlite3"
    with sqlite3.connect(str(live_path), timeout=30) as current_db:
        current_db.execute("PRAGMA wal_checkpoint(FULL)")
        with sqlite3.connect(str(snapshot_path), timeout=30) as snapshot_db:
            current_db.backup(snapshot_db)
            snapshot_integrity = snapshot_db.execute("PRAGMA integrity_check").fetchone()
            if not snapshot_integrity or snapshot_integrity[0] != "ok":
                snapshot_path.unlink(missing_ok=True)
                raise RuntimeError("Could not create a verified safety snapshot of the live database")

    # SQLite's online backup API replaces database contents transactionally; no raw file copy.
    with sqlite3.connect(str(backup_path), timeout=30) as source_db:
        with sqlite3.connect(str(live_path), timeout=30) as target_db:
            source_db.backup(target_db)
            restored_integrity = target_db.execute("PRAGMA integrity_check").fetchone()
            if not restored_integrity or restored_integrity[0] != "ok":
                raise RuntimeError("Restored database failed SQLite integrity verification")
            target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    restored_inventory = _database_inventory(live_path)
    return {
        "filename": filename,
        "verification": verification,
        "safety_snapshot": snapshot_path.name,
        "restored_counts": restored_inventory["table_counts"],
        "previous_counts": current_counts,
        "improved_tables": improved,
        "integrity": restored_inventory["integrity"],
    }


@app.get("/api/admin/database/backups")
def list_database_backups():
    current = _database_inventory(_database_file())
    backups = []
    for backup in _list_database_backups():
        inventory = _database_inventory(_resolve_database_backup(backup["filename"]))
        backup.update(inventory)
        backup["recoverable"] = (
            inventory["integrity"] == "ok"
            and any(inventory["table_counts"].get(table, 0) > current["table_counts"].get(table, 0)
                    for table in _RECOVERY_COMPARE_TABLES)
        )
        backups.append(backup)
    return {
        "backups": backups,
        "current_counts": current["table_counts"],
        "current_integrity": current["integrity"],
        "retention": 7,
        "automatic_backup": os.getenv("PROCUREAI_AUTO_BACKUP", "true").strip().lower() in ("1", "true", "yes", "on"),
        "storage_note": "Backups are stored beside the configured SQLite database on the Railway volume. Copy them off-volume for disaster recovery.",
    }


@app.post("/api/admin/database/backups/{filename}/restore")
def restore_database_backup(filename: str, payload: dict, request: Request):
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required before restoring a database backup.")
    try:
        result = _restore_database_backup(filename)
        # Bring older snapshots forward to the current safe schema. The retired fixture reset is non-destructive.
        dashboard_module.init_db()
        init_store()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"Database restore failed safely: {type(exc).__name__}")
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (getattr(request.state,"auth_user",""), "restore_database_backup", "database_backup", filename,
             json.dumps({"safety_snapshot":result["safety_snapshot"],"improved_tables":result["improved_tables"]}), _now()))
    result["post_migration_counts"] = _database_inventory(_database_file())["table_counts"]
    return {"success": True, "restore": result}


def _resolve_database_backup(filename: str) -> Path:
    if (Path(filename).name != filename or not filename.startswith("procureai-")
            or not filename.endswith(".sqlite3")):
        raise HTTPException(status_code=404, detail="Database backup not found")
    backup_dir = Path(os.getenv("PROCUREAI_BACKUP_DIR", str(_database_file().parent / "backups"))).resolve()
    candidate = (backup_dir / filename).resolve()
    if candidate.parent != backup_dir or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Database backup not found")
    return candidate


def _verify_database_backup(filename: str) -> dict:
    """Copy a backup to an isolated temporary location and exercise a read-only restore check."""
    source = _resolve_database_backup(filename)
    with tempfile.TemporaryDirectory(prefix="procureai-restore-check-") as temp_dir:
        restored_path = Path(temp_dir) / "restore-test.sqlite3"
        shutil.copy2(source, restored_path)
        with sqlite3.connect(str(restored_path), timeout=30) as restored:
            integrity = restored.execute("PRAGMA integrity_check").fetchone()
            foreign_key_violations = restored.execute("PRAGMA foreign_key_check").fetchall()
            tables = restored.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError("Backup failed SQLite integrity verification")
            if foreign_key_violations:
                raise RuntimeError("Backup contains foreign-key violations")
            row_count = 0
            for (table_name,) in tables:
                safe_name = table_name.replace('"', '""')
                row_count += restored.execute(f'SELECT COUNT(*) FROM "{safe_name}"').fetchone()[0]
        return {"filename": filename, "restore_test": "passed", "integrity": "ok",
                "foreign_key_violations": 0, "table_count": len(tables),
                "row_count": row_count, "live_database_changed": False}


@app.get("/api/admin/database/backups/{filename}/download")
def download_database_backup(filename: str):
    path = _resolve_database_backup(filename)
    return FileResponse(path, media_type="application/vnd.sqlite3", filename=filename)


@app.post("/api/admin/database/backups/{filename}/verify-restore")
def verify_database_backup_restore(filename: str, request: Request):
    try:
        result = _verify_database_backup(filename)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"Backup restore verification failed: {type(exc).__name__}")
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (getattr(request.state,"auth_user",""), "verify_database_backup", "database_backup", filename,
             json.dumps({"integrity":result["integrity"],"table_count":result["table_count"],
                         "row_count":result["row_count"],"live_database_changed":False}), _now()))
    return {"success": True, "verification": result}


@app.post("/api/admin/database/backups", status_code=201)
def create_database_backup(request: Request):
    try:
        result = _create_database_backup()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=f"Database backup could not be completed: {type(exc).__name__}")
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (getattr(request.state,"auth_user",""), "create_database_backup", "database_backup", result["filename"], json.dumps({"size_bytes":result["size_bytes"],"integrity":result["integrity"]}), _now()))
    return {"success": True, "backup": result}


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
        db.execute("""CREATE TABLE IF NOT EXISTS project_deletions (
            project_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL,
            deleted_by TEXT NOT NULL,
            deletion_note TEXT NOT NULL DEFAULT ''
        )""")
        records = db.execute("""SELECT p.project_id FROM projects p
            LEFT JOIN project_deletions d ON d.project_id=p.project_id
            WHERE d.project_id IS NULL ORDER BY p.updated_at DESC""").fetchall()
        projects = [_project_overview(db, row["project_id"]) for row in records]
    return {"projects": projects}


@app.delete("/api/projects/{project_id}")
def delete_completed_project(project_id: str, request: Request):
    """Hide a completed project from the workspace while retaining its audit and finance records."""
    actor = getattr(request.state, "auth_user", "") or "Workspace user"
    now = _now()
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS project_deletions (
            project_id TEXT PRIMARY KEY,
            deleted_at TEXT NOT NULL,
            deleted_by TEXT NOT NULL,
            deletion_note TEXT NOT NULL DEFAULT ''
        )""")
        project = db.execute(
            "SELECT project_id,title,status FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] != "completed":
            raise HTTPException(status_code=409, detail="Only completed projects can be deleted. Close the project first.")
        if db.execute("SELECT 1 FROM project_deletions WHERE project_id=?", (project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
        db.execute(
            "INSERT INTO project_deletions(project_id,deleted_at,deleted_by,deletion_note) VALUES(?,?,?,?)",
            (project_id, now, actor, "Removed from the project workspace by an administrator.")
        )
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (actor, "delete", "project", project_id,
             json.dumps({"project_title": project["title"], "deletion_type": "soft_delete",
                         "records_retained": True}), now))
    return {"success": True, "project_id": project_id,
            "message": "Completed project removed from the workspace. Historical procurement and audit records were retained."}


@app.post("/api/projects/{project_id}/unarchive")
def unarchive_project(project_id: str, request: Request):
    """Restore a terminated project to active status without discarding its closure history."""
    now = _now()
    actor = getattr(request.state, "auth_user", "") or "Workspace user"
    with connect() as db:
        project = db.execute("SELECT project_id,title,status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] != "terminated":
            raise HTTPException(status_code=409, detail="Only archived (terminated) projects can be unarchived.")
        db.execute("UPDATE projects SET status='active',updated_at=? WHERE project_id=?", (now, project_id))
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (actor, "unarchive", "project", project_id,
             json.dumps({"project_title": project["title"], "previous_status": "terminated",
                         "restored_status": "active", "termination_history_retained": True}), now))
        overview = _project_overview(db, project_id)
    return {"success": True, "project": overview,
            "message": "Project unarchived and restored to the active workspace. Previous termination history was retained."}


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
        db.execute("""CREATE TABLE IF NOT EXISTS project_intake (
            project_id TEXT PRIMARY KEY,
            project_location TEXT NOT NULL DEFAULT '',
            project_category TEXT NOT NULL DEFAULT '',
            estimated_budget REAL NOT NULL DEFAULT 0,
            expected_duration_days INTEGER NOT NULL DEFAULT 30,
            priority TEXT NOT NULL DEFAULT 'medium',
            planned_purchase_requests INTEGER NOT NULL DEFAULT 1
        )""")
        db.execute("""INSERT INTO projects
            (project_id,title,client_name,agency_details,executive_summary,procurement_lead,deliverables_json,
             start_date,due_date,status,milestones_json,progress_percent,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (project_id,payload.title.strip(),payload.client_name.strip(),payload.agency_details.strip(),
             payload.executive_summary.strip(),payload.procurement_lead.strip(),
             json.dumps([x.strip() for x in payload.deliverables if x.strip()]),
             payload.start_date,payload.due_date,payload.status,json.dumps(milestones),now,now))
        db.execute("""INSERT INTO project_intake
            (project_id,project_location,project_category,estimated_budget,expected_duration_days,priority,planned_purchase_requests)
            VALUES(?,?,?,?,?,?,?)""",
            (project_id,payload.project_location.strip(),payload.project_category.strip(),
             payload.estimated_budget,payload.expected_duration_days,payload.priority,payload.planned_purchase_requests))
        db.execute("""INSERT OR IGNORE INTO workflow_projects
            (project_id,summary,status,created_at,updated_at) VALUES(?,?,?,?,?)""",
            (project_id,payload.executive_summary.strip(), "created", now, now))
        result = _project_overview(db, project_id)
    return {"success": True, "project": result}


@app.post("/api/projects/{project_id}/complete")
def complete_project(project_id: str, payload: ProjectCompletionPayload, request: Request):
    now = _now()
    actor = getattr(request.state, "auth_user", "") or "Workspace user"
    with connect() as db:
        project = db.execute("SELECT project_id,title,status,progress_percent FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is already closed or archived.")
        if project["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="A cancelled project cannot be marked as completed.")
        db.execute("""INSERT INTO project_closures(project_id,completed_at,closed_by,completion_note,progress_at_close)
            VALUES(?,?,?,?,?)""",
            (project_id,now,actor,payload.completion_note.strip(),float(project["progress_percent"] or 0)))
        db.execute("UPDATE projects SET status='completed',progress_percent=100,updated_at=? WHERE project_id=?",
                   (now,project_id))
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (actor,"complete","project",project_id,json.dumps({
                "project_title":project["title"],"completion_note":payload.completion_note.strip(),
                "progress_at_close":float(project["progress_percent"] or 0)
            }),now))
        overview = _project_overview(db, project_id)
    return {"success":True,"project":overview}


@app.post("/api/projects/{project_id}/terminate")
def terminate_project(project_id: str, payload: ProjectTerminationPayload, request: Request):
    now = _now()
    actor = getattr(request.state, "auth_user", "") or "Workspace user"
    note = payload.termination_note.strip()
    with connect() as db:
        project = db.execute("SELECT project_id,title,status,progress_percent FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] == "terminated":
            raise HTTPException(status_code=409, detail="This project has already been terminated.")
        if project["status"] == "completed":
            raise HTTPException(status_code=409, detail="A completed project cannot be terminated. Its completion record is already final.")
        db.execute("""CREATE TABLE IF NOT EXISTS project_terminations (
            project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
            terminated_at TEXT NOT NULL,
            terminated_by TEXT NOT NULL,
            termination_note TEXT NOT NULL DEFAULT '',
            progress_at_termination REAL NOT NULL DEFAULT 0
        )""")
        db.execute("""INSERT INTO project_terminations
            (project_id,terminated_at,terminated_by,termination_note,progress_at_termination)
            VALUES(?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET
                terminated_at=excluded.terminated_at,
                terminated_by=excluded.terminated_by,
                termination_note=excluded.termination_note,
                progress_at_termination=excluded.progress_at_termination""",
            (project_id,now,actor,note,float(project["progress_percent"] or 0)))
        db.execute("UPDATE projects SET status='terminated',updated_at=? WHERE project_id=?", (now,project_id))
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (actor,"terminate","project",project_id,json.dumps({
                "project_title":project["title"],"termination_note":note,
                "progress_at_termination":float(project["progress_percent"] or 0),
                "records_retained":True
            }),now))
        overview = _project_overview(db,project_id)
    return {"success":True,"project":overview}


@app.get("/api/projects/trends")
def project_completion_trends():
    today = date.today()
    cursor = today.replace(day=1)
    months = []
    for _ in range(12):
        key = cursor.strftime("%Y-%m")
        rows = []
        with connect() as db:
            rows = db.execute("""SELECT p.project_id,p.title,p.client_name,p.procurement_lead,
                c.completed_at,c.closed_by,c.completion_note
                FROM project_closures c JOIN projects p ON p.project_id=c.project_id
                WHERE substr(c.completed_at,1,7)=?
                ORDER BY c.completed_at DESC,p.title ASC""", (key,)).fetchall()
        months.append({"month":key,"label":cursor.strftime("%b %Y"),"count":len(rows),
                       "projects":[dict(row) for row in rows]})
        cursor = (cursor.replace(day=1) - __import__("datetime").timedelta(days=1)).replace(day=1)
    current_key = today.strftime("%Y-%m")
    current_month = next((item for item in months if item["month"] == current_key), None)
    featured = current_month["projects"][0] if current_month and current_month["projects"] else None
    return {"months":list(reversed(months)),"current_month":today.strftime("%B %Y"),
            "completed_this_month":current_month["count"] if current_month else 0,
            "project_of_the_month":featured}


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
        project = db.execute("SELECT title,status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is closed. Linked quotations are read-only.")
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
        project = db.execute("SELECT status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is closed or archived. Linked quotations are read-only.")
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


@app.get("/api/projects/{project_id}/buyer-confirmations")
def list_buyer_confirmations(project_id: str):
    with connect() as db:
        project = db.execute("SELECT project_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        db.execute("""CREATE TABLE IF NOT EXISTS buyer_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            buyer_name TEXT NOT NULL, organisation TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL, status TEXT NOT NULL,
            requirements_confirmed TEXT NOT NULL,
            budget_delivery_accepted TEXT NOT NULL,
            comments TEXT NOT NULL DEFAULT '', recorded_by TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""")
        latest = db.execute(
            "SELECT * FROM buyer_confirmations WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
        ).fetchone()
        record = dict(latest) if latest else None
    return {"confirmation": record, "latest": record, "confirmations": [record] if record else []}


@app.post("/api/projects/{project_id}/buyer-confirmations")
def record_buyer_confirmation(project_id: str, payload: BuyerConfirmationPayload, request: Request):
    now = _now()
    actor = getattr(request.state, "auth_user", "") or "Workspace user"
    with connect() as db:
        project = db.execute("SELECT project_id,title,status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is closed. Buyer confirmation is read-only.")
        db.execute("""CREATE TABLE IF NOT EXISTS buyer_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
            buyer_name TEXT NOT NULL, organisation TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL, status TEXT NOT NULL,
            requirements_confirmed TEXT NOT NULL,
            budget_delivery_accepted TEXT NOT NULL,
            comments TEXT NOT NULL DEFAULT '', recorded_by TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""")
        existing = db.execute(
            "SELECT id FROM buyer_confirmations WHERE project_id=? ORDER BY id DESC LIMIT 1", (project_id,)
        ).fetchone()
        values = (payload.buyer_name.strip(),payload.organisation.strip(),payload.channel,
                  payload.status,payload.requirements_confirmed.strip(),payload.budget_delivery_accepted,
                  payload.comments.strip(),actor,now)
        if existing:
            confirmation_id = existing["id"]
            db.execute("""UPDATE buyer_confirmations SET buyer_name=?,organisation=?,channel=?,status=?,
                requirements_confirmed=?,budget_delivery_accepted=?,comments=?,recorded_by=?,recorded_at=?
                WHERE id=?""", (*values,confirmation_id))
            action = "update_buyer_confirmation"
        else:
            cur = db.execute("""INSERT INTO buyer_confirmations
                (project_id,buyer_name,organisation,channel,status,requirements_confirmed,
                 budget_delivery_accepted,comments,recorded_by,recorded_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (project_id,*values))
            confirmation_id = cur.lastrowid
            action = "record_buyer_confirmation"
        # Keep a single current confirmation per project. Older duplicate rows from
        # the previous history-based implementation are removed; audit events remain.
        db.execute("DELETE FROM buyer_confirmations WHERE project_id=? AND id<>?", (project_id,confirmation_id))
        db.execute("""INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at)
            VALUES(?,?,?,?,?,?)""",
            (actor,action,"project",project_id,json.dumps({
                "confirmation_id":confirmation_id,"buyer_name":payload.buyer_name.strip(),
                "channel":payload.channel,"status":payload.status,
                "budget_delivery_accepted":payload.budget_delivery_accepted
            }),now))
        saved = dict(db.execute("SELECT * FROM buyer_confirmations WHERE id=?", (confirmation_id,)).fetchone())
    return {"success":True,"confirmation":saved,"confirmations":[saved],"updated":bool(existing)}


@app.post("/api/projects/{project_id}/updates", status_code=201)
def add_project_daily_update(project_id: str, payload: ProjectDailyUpdatePayload, request: Request):
    allowed = {item["key"] for item in PROJECT_MILESTONE_TEMPLATE}
    invalid = sorted(set(payload.completed_milestones) - allowed)
    if invalid:
        raise HTTPException(status_code=422, detail="Unknown milestone key: " + ", ".join(invalid))
    now = _now()
    author = getattr(request.state, "auth_user", "") or "Workspace user"
    with connect() as db:
        project_row = db.execute("SELECT status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project_row:
            raise HTTPException(status_code=404, detail="Project not found")
        if project_row["status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is closed or archived. Daily updates are read-only.")
        db.execute("""INSERT INTO project_updates
            (project_id,author,update_type,status,progress_percent,field_notes,blockers,next_steps,
             completed_milestones_json,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (project_id,author,payload.update_type,payload.status,payload.progress_percent,
             payload.field_notes.strip(),payload.blockers.strip(),payload.next_steps.strip(),
             json.dumps(payload.completed_milestones),now))
        for key in payload.completed_milestones:
            _complete_project_milestone(db, project_id, key, "Daily team update")
        project_row = db.execute("SELECT status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project_row and project_row["status"] in ("completed","terminated"):
            raise HTTPException(status_code=409, detail="This project is closed or archived. Updates are read-only.")
        project = _project_overview(db, project_id)
    return {"success": True, "project": project}


@app.post("/api/projects/{project_id}/recalculate")
def recalculate_project_progress(project_id: str):
    with connect() as db:
        current = db.execute("SELECT status FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Project not found")
        if current["status"] in ("completed", "terminated"):
            raise HTTPException(status_code=409, detail="This project is closed or archived. Progress is read-only.")
        progress = _recalculate_project(db, project_id)
        project = _project_overview(db, project_id)
    return {"success": True, "progress": progress, "project": project}


@app.get("/api/dashboard")
def get_dashboard():
    return dashboard_module.dashboard()


@app.get("/api/analysis")
def get_dashboard_analysis(request_id: int):
    with dashboard_module.connect() as db:
        _require_request_buyer_confirmation(db, int(request_id))
    try:
        return dashboard_module.analyse_request(request_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/manifest.webmanifest")
def web_app_manifest():
    return FileResponse(BASE_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def web_app_service_worker():
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# --- Project job completion reports (before / after evidence) ---
@app.get("/api/projects/{project_id}/job-completion-report")
def get_job_completion_report(project_id: str):
    with connect() as db:
        project = db.execute("SELECT project_id FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        db.execute("""CREATE TABLE IF NOT EXISTS project_job_completion_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL UNIQUE REFERENCES projects(project_id) ON DELETE CASCADE,
            report_json TEXT NOT NULL,
            before_photo_json TEXT,
            after_photo_json TEXT,
            status TEXT NOT NULL DEFAULT 'Draft',
            recorded_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        row = db.execute("SELECT * FROM project_job_completion_reports WHERE project_id=?", (project_id,)).fetchone()
        report = None
        if row:
            report = json.loads(row["report_json"])
            report.update({"before_photo":json.loads(row["before_photo_json"]) if row["before_photo_json"] else None,
                           "after_photo":json.loads(row["after_photo_json"]) if row["after_photo_json"] else None,
                           "status":row["status"],"recorded_by":row["recorded_by"],
                           "created_at":row["created_at"],"updated_at":row["updated_at"]})
    return {"report":report}


@app.post("/api/projects/{project_id}/job-completion-report")
def save_job_completion_report(project_id: str, payload: dict, request: Request):
    allowed = ("job","job_date","location","equipment","purpose","initial_condition","work_carried_out",
               "final_condition","technician","supervisor","remarks")
    values = {key:str(payload.get(key,"") or "").strip() for key in allowed}
    status = str(payload.get("status","Draft"))
    if status not in ("Draft","Submitted"):
        raise HTTPException(status_code=422, detail="Report status must be Draft or Submitted.")
    for key in ("job","job_date","location","equipment","purpose","initial_condition","work_carried_out"):
        if status == "Submitted" and not values[key]:
            raise HTTPException(status_code=422, detail=f"{key.replace('_',' ').capitalize()} is required before submission.")
    def photo(value, label):
        if value is None: return None
        if not isinstance(value,dict) or not str(value.get("data_url","")).startswith("data:image/"):
            raise HTTPException(status_code=422, detail=f"Invalid {label} photograph.")
        data_url=str(value["data_url"])
        if len(data_url) > 7 * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"{label.capitalize()} photograph is too large. Maximum size is 5 MB.")
        mime=str(value.get("mime_type",""))
        if mime not in ("image/jpeg","image/png","image/webp") or not data_url.startswith("data:"+mime+";base64,"):
            raise HTTPException(status_code=422, detail=f"{label.capitalize()} photograph must be JPG, PNG or WebP.")
        return {"name":str(value.get("name",""))[:180],"mime_type":mime,"data_url":data_url}
    before=photo(payload.get("before_photo"),"before")
    after=photo(payload.get("after_photo"),"after")
    if status=="Submitted" and (not before or not after):
        raise HTTPException(status_code=422, detail="Both before and after photographs are required before submission.")
    now=_now(); actor=getattr(request.state,"auth_user","") or "Workspace user"
    with connect() as db:
        project=db.execute("SELECT project_id,status FROM projects WHERE project_id=?",(project_id,)).fetchone()
        if not project: raise HTTPException(status_code=404,detail="Project not found")
        if project["status"] == "terminated":
            raise HTTPException(status_code=409,detail="This project is archived. Job completion reports are read-only.")
        db.execute("""CREATE TABLE IF NOT EXISTS project_job_completion_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL UNIQUE REFERENCES projects(project_id) ON DELETE CASCADE,
            report_json TEXT NOT NULL,
            before_photo_json TEXT,
            after_photo_json TEXT,
            status TEXT NOT NULL DEFAULT 'Draft',
            recorded_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        existing=db.execute("SELECT created_at FROM project_job_completion_reports WHERE project_id=?",(project_id,)).fetchone()
        created=existing["created_at"] if existing else now
        db.execute("""INSERT INTO project_job_completion_reports
            (project_id,report_json,before_photo_json,after_photo_json,status,recorded_by,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(project_id) DO UPDATE SET report_json=excluded.report_json,
            before_photo_json=excluded.before_photo_json,after_photo_json=excluded.after_photo_json,
            status=excluded.status,recorded_by=excluded.recorded_by,updated_at=excluded.updated_at""",
            (project_id,json.dumps(values),json.dumps(before) if before else None,json.dumps(after) if after else None,status,actor,created,now))
        db.execute("""CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO audit_events(actor,action,entity_type,entity_id,details_json,created_at) VALUES(?,?,?,?,?,?)",
                   (actor,"submit_job_completion_report" if status=="Submitted" else "save_job_completion_report",
                    "project",project_id,json.dumps({"status":status,"job":values["job"]}),now))
        row=db.execute("SELECT * FROM project_job_completion_reports WHERE project_id=?",(project_id,)).fetchone()
        report=json.loads(row["report_json"])
        report.update({"before_photo":json.loads(row["before_photo_json"]) if row["before_photo_json"] else None,
                       "after_photo":json.loads(row["after_photo_json"]) if row["after_photo_json"] else None,
                       "status":row["status"],"recorded_by":row["recorded_by"],
                       "created_at":row["created_at"],"updated_at":row["updated_at"]})
    return {"success":True,"report":report}
