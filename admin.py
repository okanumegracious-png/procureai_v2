"""Administrator module for ProcureAI (standard library only).

Provides authentication (scrypt password hashes, DB-backed sessions in an
HttpOnly cookie), role checks, user / supplier / request / order management,
runtime approval-policy settings and an audit log.  All routes live under
/api/admin and are wired into app.py through ``handle``.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

SESSION_COOKIE = "procureai_admin"
SESSION_HOURS = 8
ROLES = ("admin", "manager", "viewer")
SUPPLIER_STATUSES = ("Active", "Suspended")
REQUEST_STATUSES = ("Open", "Cancelled")
PRIORITIES = ("Normal", "Urgent")
DEFAULT_SETTINGS = {"approval_limit": "2500000", "min_supplier_rating": "3.5"}
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300

_connect = None  # set by configure(); returns a sqlite3 connection (row_factory=Row)
_attempts: dict = {}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def configure(connect_fn):
    global _connect
    _connect = connect_fn


def now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


@contextmanager
def tx():
    db = _connect()
    try:
        with db:
            yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- schema
def init_schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE COLLATE NOCASE, password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'viewer', active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL, last_login TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions(
      token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
      action TEXT NOT NULL, target TEXT DEFAULT '', detail TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    db.executemany("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", DEFAULT_SETTINGS.items())


def get_setting(key: str) -> str:
    with tx() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS[key]


def ensure_admin(email=None, password=None):
    """Create the first administrator if none exists. Returns the generated
    password when one had to be generated (shown once on the console)."""
    with tx() as db:
        if db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
            return None
        email = (email or os.getenv("PROCUREAI_ADMIN_EMAIL") or "admin@procureai.local").strip().lower()
        password = password or os.getenv("PROCUREAI_ADMIN_PASSWORD")
        generated = None
        if not password:
            password = generated = secrets.token_urlsafe(12)
        check_password(password)
        db.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                   ("Administrator", email, hash_password(password), "admin", now()))
        audit(db, "system", "user.bootstrap", email, "First administrator created")
    if generated:
        print(f"\n  First administrator created\n  Email:    {email}\n  Password: {generated}\n"
              "  (shown once - sign in at /admin and change it)\n")
    return generated


# --------------------------------------------------------------------------- passwords
def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), dk)
    except Exception:
        return False


_DUMMY_HASH = hash_password("timing-equaliser")


def check_password(pw: str):
    if len(pw) < 10:
        raise ApiError(400, "Password must be at least 10 characters")
    if len(pw) > 200:
        raise ApiError(400, "Password is too long")


# --------------------------------------------------------------------------- helpers
def audit(db, actor, action, target="", detail=""):
    db.execute("INSERT INTO audit_log(at,actor,action,target,detail) VALUES(?,?,?,?,?)",
               (now(), actor, action, str(target), str(detail)[:500]))


def text(data, key, label, maxlen=120, required=True):
    v = str(data.get(key, "") or "").strip()
    if required and not v:
        raise ApiError(400, f"{label} is required")
    if len(v) > maxlen:
        raise ApiError(400, f"{label} is too long")
    return v


def number(data, key, label, lo, hi):
    try:
        v = float(data[key])
    except (KeyError, TypeError, ValueError):
        raise ApiError(400, f"{label} must be a number")
    if not (lo <= v <= hi):
        raise ApiError(400, f"{label} must be between {lo:g} and {hi:g}")
    return v


def choice(data, key, label, options):
    v = data.get(key)
    if v not in options:
        raise ApiError(400, f"{label} must be one of: {', '.join(options)}")
    return v


def update(db, table, row_id, fields):
    """`fields` keys come from code, never from user input."""
    if not fields:
        raise ApiError(400, "Nothing to update")
    cols = ", ".join(f"{k}=?" for k in fields)
    cur = db.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))
    if cur.rowcount == 0:
        raise ApiError(404, "Record not found")


def public_user(u):
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
            "active": bool(u["active"]), "created_at": u["created_at"], "last_login": u["last_login"]}


def body(h):
    data = h.body()
    if not isinstance(data, dict):
        raise ApiError(400, "A JSON object is required")
    return data


# --------------------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cookie(token, max_age, h):
    secure = "; Secure" if h.headers.get("X-Forwarded-Proto") == "https" else ""
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}{secure}"


def _session_token(h):
    raw = h.headers.get("Cookie", "")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(SESSION_COOKIE)
    return morsel.value if morsel else None


def require_admin(h):
    token = _session_token(h)
    if not token:
        raise ApiError(401, "Sign in required")
    with tx() as db:
        u = db.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                          WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
                       (_token_hash(token), now())).fetchone()
    if not u:
        raise ApiError(401, "Session expired - sign in again")
    if u["role"] != "admin":
        raise ApiError(403, "Administrator access required")
    return dict(u)


def check_origin(h):
    origin = h.headers.get("Origin")
    if origin and urlparse(origin).netloc != h.headers.get("Host", ""):
        raise ApiError(403, "Cross-origin request blocked")


# --------------------------------------------------------------------------- routes
def handle(h, method, path):
    """Dispatch /api/admin/* requests. Returns True when the path was handled."""
    if not path.startswith("/api/admin"):
        return False
    try:
        status, payload, headers = route(h, method, path)
    except ApiError as e:
        status, payload, headers = e.status, {"error": e.message}, None
    except sqlite3.IntegrityError as e:
        status, payload, headers = 409, {"error": f"Conflict: {e}"}, None
    except ValueError as e:
        status, payload, headers = 400, {"error": str(e)}, None
    except Exception:
        status, payload, headers = 500, {"error": "Server error"}, None
    h.send_json(payload, status, headers)
    return True


def route(h, method, path):
    parts = [p for p in path.split("/")[3:] if p]
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        check_origin(h)
        if not h.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise ApiError(415, "Content-Type must be application/json")

    if parts == ["login"] and method == "POST":
        return login(h)
    if parts == ["logout"] and method == "POST":
        return logout(h)

    me = require_admin(h)
    actor = me["email"]
    n = len(parts)

    if parts == ["me"] and method == "GET":
        return 200, {"user": public_user(me)}, None
    if parts == ["overview"] and method == "GET":
        return 200, overview(), None
    if parts == ["password"] and method == "POST":
        return change_password(h, me)

    # users
    if parts == ["users"] and method == "GET":
        with tx() as db:
            rows = [public_user(r) for r in db.execute("SELECT * FROM users ORDER BY id")]
        return 200, {"users": rows}, None
    if parts == ["users"] and method == "POST":
        return create_user(h, actor)
    if n == 2 and parts[0] == "users" and method == "PATCH":
        return patch_user(h, me, _id(parts[1]))
    if n == 3 and parts[0] == "users" and parts[2] == "reset-password" and method == "POST":
        return reset_password(h, actor, _id(parts[1]))

    # suppliers
    if parts == ["suppliers"] and method == "GET":
        with tx() as db:
            rows = [dict(r) for r in db.execute("""
                SELECT s.*, (SELECT COUNT(*) FROM quotes q WHERE q.supplier_id=s.id) quote_count,
                       (SELECT COUNT(*) FROM orders o WHERE o.supplier_id=s.id) order_count
                FROM suppliers s ORDER BY s.id""")]
        return 200, {"suppliers": rows}, None
    if parts == ["suppliers"] and method == "POST":
        return create_supplier(h, actor)
    if n == 2 and parts[0] == "suppliers" and method == "PATCH":
        return patch_supplier(h, actor, _id(parts[1]))
    if n == 2 and parts[0] == "suppliers" and method == "DELETE":
        return delete_supplier(actor, _id(parts[1]))

    # purchase requests
    if parts == ["requests"] and method == "GET":
        with tx() as db:
            rows = [dict(r) for r in db.execute("""
                SELECT r.*, (SELECT COUNT(*) FROM quotes q WHERE q.request_id=r.id) quote_count,
                       (SELECT COUNT(*) FROM orders o WHERE o.request_id=r.id) order_count
                FROM requests r ORDER BY r.id DESC""")]
        return 200, {"requests": rows}, None
    if n == 2 and parts[0] == "requests" and method == "PATCH":
        return patch_request(h, actor, _id(parts[1]))
    if n == 2 and parts[0] == "requests" and method == "DELETE":
        return delete_request(actor, _id(parts[1]))

    # orders
    if parts == ["orders"] and method == "GET":
        with tx() as db:
            rows = [dict(r) for r in db.execute("""
                SELECT o.*, s.name supplier, r.material, r.code request_code FROM orders o
                JOIN suppliers s ON s.id=o.supplier_id JOIN requests r ON r.id=o.request_id
                ORDER BY o.id DESC""")]
        return 200, {"orders": rows}, None
    if n == 3 and parts[0] == "orders" and parts[2] == "decision" and method == "POST":
        return decide_order(h, actor, _id(parts[1]))

    # settings + audit
    if parts == ["settings"] and method == "GET":
        with tx() as db:
            s = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")}
        return 200, {"settings": {**DEFAULT_SETTINGS, **s}}, None
    if parts == ["settings"] and method == "PUT":
        return put_settings(h, actor)
    if parts == ["audit"] and method == "GET":
        q = parse_qs(urlparse(h.path).query)
        try:
            limit = min(max(int(q.get("limit", ["200"])[0]), 1), 1000)
        except ValueError:
            raise ApiError(400, "limit must be a number")
        with tx() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]
        return 200, {"audit": rows}, None

    raise ApiError(404, "Not found")


def _id(raw):
    try:
        return int(raw)
    except ValueError:
        raise ApiError(404, "Not found")


# --------------------------------------------------------------------------- auth routes
def login(h):
    data = body(h)
    email = str(data.get("email", "")).strip().lower()[:200]
    pw = str(data.get("password", ""))[:200]
    key = (h.client_address[0], email)
    st = _attempts.get(key)
    if st and st["until"] > time.time():
        raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
    with tx() as db:
        u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    ok = verify_password(pw, u["password_hash"] if u else _DUMMY_HASH)
    if not (u and ok and u["active"]):
        if len(_attempts) > 10000:
            _attempts.clear()
        st = _attempts.setdefault(key, {"count": 0, "until": 0})
        st["count"] += 1
        if st["count"] >= MAX_FAILED_LOGINS:
            st["count"], st["until"] = 0, time.time() + LOCKOUT_SECONDS
        with tx() as db:
            audit(db, email or "unknown", "login.failed", email)
        raise ApiError(401, "Incorrect email or password")
    if u["role"] != "admin":
        raise ApiError(403, "This account does not have administrator access")
    _attempts.pop(key, None)
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
    with tx() as db:
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (now(),))
        db.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                   (_token_hash(token), u["id"], expires))
        db.execute("UPDATE users SET last_login=? WHERE id=?", (now(), u["id"]))
        audit(db, u["email"], "login", u["email"])
    return 200, {"user": public_user(u)}, {"Set-Cookie": _cookie(token, SESSION_HOURS * 3600, h)}


def logout(h):
    token = _session_token(h)
    if token:
        with tx() as db:
            row = db.execute("""SELECT u.email FROM sessions s JOIN users u ON u.id=s.user_id
                                WHERE s.token_hash=?""", (_token_hash(token),)).fetchone()
            db.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
            if row:
                audit(db, row["email"], "logout", row["email"])
    return 200, {"success": True}, {"Set-Cookie": _cookie("", 0, h)}


def change_password(h, me):
    data = body(h)
    if not verify_password(str(data.get("current", "")), me["password_hash"]):
        raise ApiError(400, "Current password is incorrect")
    new = str(data.get("new", ""))
    check_password(new)
    keep = _token_hash(_session_token(h))
    with tx() as db:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new), me["id"]))
        db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (me["id"], keep))
        audit(db, me["email"], "password.change", me["email"])
    return 200, {"success": True}, None


# --------------------------------------------------------------------------- users
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_user(h, actor):
    data = body(h)
    name = text(data, "name", "Name")
    email = text(data, "email", "Email", 200).lower()
    if not EMAIL_RE.match(email):
        raise ApiError(400, "Enter a valid email address")
    role = choice(data, "role", "Role", ROLES)
    pw = str(data.get("password") or "")
    temp = None
    if pw:
        check_password(pw)
    else:
        pw = temp = secrets.token_urlsafe(9)
    with tx() as db:
        try:
            cur = db.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                             (name, email, hash_password(pw), role, now()))
        except sqlite3.IntegrityError:
            raise ApiError(409, "A user with that email already exists")
        audit(db, actor, "user.create", email, f"role={role}")
        u = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return 201, {"user": public_user(u), "temporary_password": temp}, None


def patch_user(h, me, uid):
    data = body(h)
    with tx() as db:
        t = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not t:
            raise ApiError(404, "User not found")
        fields = {}
        if "name" in data:
            fields["name"] = text(data, "name", "Name")
        if "role" in data:
            fields["role"] = choice(data, "role", "Role", ROLES)
        if "active" in data:
            fields["active"] = 1 if data["active"] else 0
        new_role = fields.get("role", t["role"])
        new_active = fields.get("active", t["active"])
        if uid == me["id"] and (new_role != t["role"] or not new_active):
            raise ApiError(400, "You cannot change your own role or deactivate your own account")
        if t["role"] == "admin" and t["active"] and (new_role != "admin" or not new_active):
            others = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id!=?", (uid,)).fetchone()[0]
            if others == 0:
                raise ApiError(400, "At least one active administrator is required")
        update(db, "users", uid, fields)
        if not new_active or new_role != "admin":
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(db, me["email"], "user.update", t["email"], ", ".join(f"{k}={v}" for k, v in fields.items()))
        u = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return 200, {"user": public_user(u)}, None


def reset_password(h, actor, uid):
    data = body(h)
    pw = str(data.get("password") or "")
    temp = None
    if pw:
        check_password(pw)
    else:
        pw = temp = secrets.token_urlsafe(9)
    with tx() as db:
        t = db.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
        if not t:
            raise ApiError(404, "User not found")
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(pw), uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        audit(db, actor, "user.reset_password", t["email"])
    return 200, {"success": True, "temporary_password": temp}, None


# --------------------------------------------------------------------------- suppliers
def _supplier_fields(data, creating):
    f = {}
    if creating or "name" in data:
        f["name"] = text(data, "name", "Name")
    if creating or "city" in data:
        f["city"] = text(data, "city", "City")
    if "contact" in data:
        f["contact"] = text(data, "contact", "Contact", 200, required=False)
    if "rating" in data:
        f["rating"] = number(data, "rating", "Rating", 0, 5)
    if "on_time" in data:
        f["on_time"] = number(data, "on_time", "On-time rate", 0, 100)
    if "status" in data:
        f["status"] = choice(data, "status", "Status", SUPPLIER_STATUSES)
    return f


def create_supplier(h, actor):
    f = _supplier_fields(body(h), True)
    with tx() as db:
        cur = db.execute("INSERT INTO suppliers(name,city,rating,on_time,contact,status) VALUES(?,?,?,?,?,?)",
                         (f["name"], f["city"], f.get("rating", 0), f.get("on_time", 0),
                          f.get("contact", ""), f.get("status", "Active")))
        audit(db, actor, "supplier.create", f["name"])
        row = db.execute("SELECT * FROM suppliers WHERE id=?", (cur.lastrowid,)).fetchone()
    return 201, {"supplier": dict(row)}, None


def patch_supplier(h, actor, sid):
    f = _supplier_fields(body(h), False)
    with tx() as db:
        update(db, "suppliers", sid, f)
        row = db.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        audit(db, actor, "supplier.update", row["name"], ", ".join(f"{k}={v}" for k, v in f.items()))
    return 200, {"supplier": dict(row)}, None


def delete_supplier(actor, sid):
    with tx() as db:
        row = db.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if not row:
            raise ApiError(404, "Supplier not found")
        used = db.execute("""SELECT (SELECT COUNT(*) FROM quotes WHERE supplier_id=?) +
                                    (SELECT COUNT(*) FROM orders WHERE supplier_id=?)""", (sid, sid)).fetchone()[0]
        if used:
            raise ApiError(409, "Supplier has quotations or orders on record - suspend it instead of deleting")
        db.execute("DELETE FROM suppliers WHERE id=?", (sid,))
        audit(db, actor, "supplier.delete", row["name"])
    return 200, {"success": True}, None


# --------------------------------------------------------------------------- requests / orders
def patch_request(h, actor, rid):
    data = body(h)
    with tx() as db:
        r = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "Request not found")
        f = {}
        if "priority" in data:
            f["priority"] = choice(data, "priority", "Priority", PRIORITIES)
        if "status" in data:
            f["status"] = choice(data, "status", "Status", REQUEST_STATUSES)
            live = db.execute("SELECT COUNT(*) FROM orders WHERE request_id=? AND status IN ('Approved','Pending approval')",
                              (rid,)).fetchone()[0]
            if live:
                raise ApiError(409, "Request has an active purchase order and cannot change status")
        update(db, "requests", rid, f)
        audit(db, actor, "request.update", r["code"], ", ".join(f"{k}={v}" for k, v in f.items()))
        row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    return 200, {"request": dict(row)}, None


def delete_request(actor, rid):
    with tx() as db:
        r = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not r:
            raise ApiError(404, "Request not found")
        used = db.execute("""SELECT (SELECT COUNT(*) FROM quotes WHERE request_id=?) +
                                    (SELECT COUNT(*) FROM orders WHERE request_id=?)""", (rid, rid)).fetchone()[0]
        if used:
            raise ApiError(409, "Request has quotations or orders on record - cancel it instead of deleting")
        db.execute("DELETE FROM requests WHERE id=?", (rid,))
        audit(db, actor, "request.delete", r["code"])
    return 200, {"success": True}, None


def decide_order(h, actor, oid):
    data = body(h)
    action = data.get("action")
    if action not in ("approve", "reject"):
        raise ApiError(400, "Decision must be approve or reject")
    note = text(data, "note", "Note", 300, required=False)
    status = "Approved" if action == "approve" else "Rejected"
    with tx() as db:
        o = db.execute("SELECT code FROM orders WHERE id=?", (oid,)).fetchone()
        cur = db.execute("UPDATE orders SET status=?, decision_note=? WHERE id=? AND status='Pending approval'",
                         (status, f"{note} (by {actor})" if note else f"Decided by {actor}", oid))
        if cur.rowcount == 0:
            raise ApiError(409, "Order is not awaiting approval")
        audit(db, actor, f"order.{action}", o["code"], note)
    return 200, {"success": True, "status": status}, None


# --------------------------------------------------------------------------- settings / overview
def put_settings(h, actor):
    data = body(h)
    new = {}
    if "approval_limit" in data:
        new["approval_limit"] = f"{number(data, 'approval_limit', 'Approval limit', 1, 1e12):.2f}"
    if "min_supplier_rating" in data:
        new["min_supplier_rating"] = f"{number(data, 'min_supplier_rating', 'Minimum supplier rating', 0, 5):g}"
    if not new:
        raise ApiError(400, "Nothing to update")
    with tx() as db:
        for k, v in new.items():
            old = db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
            audit(db, actor, "settings.update", k, f"{old['value'] if old else '-'} -> {v}")
        s = {r["key"]: r["value"] for r in db.execute("SELECT key,value FROM settings")}
    return 200, {"settings": {**DEFAULT_SETTINGS, **s}}, None


def overview():
    with tx() as db:
        def counts(table, col):
            return {r[0]: r[1] for r in db.execute(f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}")}
        spend = db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status='Approved'").fetchone()[0]
        pending = [dict(r) for r in db.execute("""
            SELECT o.id, o.code, o.total, o.decision_note, s.name supplier FROM orders o
            JOIN suppliers s ON s.id=o.supplier_id WHERE o.status='Pending approval' ORDER BY o.id DESC LIMIT 5""")]
        recent = [dict(r) for r in db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 8")]
        return {
            "users": counts("users", "role"), "suppliers": counts("suppliers", "status"),
            "requests": counts("requests", "status"), "orders": counts("orders", "status"),
            "approved_spend": spend, "pending_orders": pending, "recent_audit": recent,
        }
