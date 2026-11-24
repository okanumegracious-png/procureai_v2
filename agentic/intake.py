"""First agentic feature: natural-language procurement request intake.

The agent extracts structured fields, validates them, persists a draft, and
identifies missing information before supplier sourcing begins. The default
extractor is deterministic; inject an LLM-backed callable for richer parsing.
Money and quantities are parsed as Decimal, never inferred from an LLM total.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

D = Decimal
DEFAULT_LOCATION = "Port Harcourt, Rivers State, Nigeria"
CURRENCY = "NGN"


@dataclass
class ProcurementRequest:
    description: str
    item: Optional[str] = None
    quantity: Optional[Decimal] = None
    unit: Optional[str] = None
    budget: Optional[Decimal] = None
    currency: str = CURRENCY
    delivery_location: str = DEFAULT_LOCATION
    required_within_days: Optional[int] = None
    category: Optional[str] = None
    requester: Optional[str] = None


@dataclass
class IntakeResult:
    request_id: str
    status: str
    request: ProcurementRequest
    missing_fields: list[str]
    next_actions: list[str]
    sourcing_ready: bool


def _money(raw: str) -> Optional[Decimal]:
    try:
        return D(raw.replace(",", "").replace("₦", "").replace("NGN", "").strip())
    except InvalidOperation:
        return None


def deterministic_extract(text: str) -> ProcurementRequest:
    """Extract common fields from plain-language requests; no external calls."""
    if not text or not text.strip():
        raise ValueError("Describe what you need to procure.")

    item = None
    match = re.search(
        r"(?:need|buy|procure|source|purchase|require)\s+(?:about\s+|approximately\s+)?"
        r"(.+?)(?=\s+(?:for|to be delivered|delivery|within|in the next|budget|with a budget)\b|[,.]|$)",
        text, re.I,
    )
    if match:
        item = re.sub(r"^\d[\d,]*(?:\.\d+)?\s+(?:bags?|units?|pieces?|pcs?|kg|tonnes?|tons?|litres?|liters?|rolls?|sheets?|boxes?|cartons?|metres?|meters?)\s+of\s+", "", match.group(1).strip(), flags=re.I)
        item = re.sub(r"^\d[\d,]*(?:\.\d+)?\s+", "", item) or None

    quantity = None
    unit = None
    match = re.search(
        r"\b(\d[\d,]*(?:\.\d+)?)\s*(bags?|units?|pieces?|pcs?|kg|tonnes?|tons?|litres?|liters?|rolls?|sheets?|boxes?|cartons?|metres?|meters?)\b",
        text, re.I,
    )
    if match:
        quantity = D(match.group(1).replace(",", ""))
        unit = match.group(2).lower()

    budget = None
    match = re.search(
        r"(?:budget(?:\s+is|\s+of|\s+around)?|not more than|up to)\s*"
        r"(?:₦|NGN\s*)?([\d,]+(?:\.\d{1,2})?)\s*(million|m)?", text, re.I,
    )
    if match:
        budget = _money(match.group(1))
        if budget is not None and (match.group(2) or "").lower() in ("million", "m"):
            budget *= D("1000000")

    days = None
    match = re.search(r"(?:within|in)\s+(\d+)\s+days?", text, re.I)
    if match:
        days = int(match.group(1))

    location = DEFAULT_LOCATION
    match = re.search(
        r"(?:deliver(?:y)?\s+(?:to|in)|for\s+(?:a\s+)?project\s+in)\s+"
        r"([A-Za-z][A-Za-z ,'-]+?)(?=\s+(?:within|in\s+\d+\s+days?|budget|with a budget)|[,.]|$)",
        text, re.I,
    )
    if match:
        location = match.group(1).strip()
        if "port harcourt" in location.lower() and "rivers" not in location.lower():
            location += ", Rivers State, Nigeria"

    return ProcurementRequest(
        description=text.strip(), item=item, quantity=quantity, unit=unit,
        budget=budget, delivery_location=location, required_within_days=days,
    )


class SQLiteRequestStore:
    """Durable request storage. SQLite is suitable for local development."""
    def __init__(self, path: str | Path = "procureai_agent.db"):
        self.path = str(path)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS procurement_intake (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def save(self, request_id: str, status: str, request: ProcurementRequest) -> None:
        payload = asdict(request)
        payload["quantity"] = str(request.quantity) if request.quantity is not None else None
        payload["budget"] = str(request.budget) if request.budget is not None else None
        now = datetime.utcnow().isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO procurement_intake(id,status,payload,created_at,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at""",
                (request_id, status, json.dumps(payload), now, now))

    def get(self, request_id: str) -> Optional[dict]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM procurement_intake WHERE id=?", (request_id,)).fetchone()
        return ({"id": row["id"], "status": row["status"], "request": json.loads(row["payload"]) }
                if row else None)

    def list(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT id,status,payload,created_at FROM procurement_intake "
                              "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": r["id"], "status": r["status"], "request": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]


class IntakeAgent:
    """Bounded intake agent; it does not contact suppliers or spend money."""
    def __init__(self, store: SQLiteRequestStore,
                 extractor: Optional[Callable[[str], ProcurementRequest]] = None):
        self.store = store
        self.extractor = extractor or deterministic_extract

    def process(self, text: str) -> IntakeResult:
        request = self.extractor(text)
        if not isinstance(request, ProcurementRequest):
            raise TypeError("Extractor must return a ProcurementRequest")
        if request.quantity is not None and request.quantity <= 0:
            raise ValueError("Quantity must be greater than zero.")
        if request.budget is not None and request.budget <= 0:
            raise ValueError("Budget must be greater than zero.")
        if request.required_within_days is not None and request.required_within_days < 1:
            raise ValueError("Delivery deadline must be at least one day.")

        missing = []
        if not request.item: missing.append("item")
        if request.quantity is None: missing.append("quantity")
        if not request.unit: missing.append("unit")
        if not request.required_within_days: missing.append("delivery deadline")
        status = "ready_for_sourcing" if not missing else "needs_clarification"
        request_id = "PR-" + uuid.uuid4().hex[:10].upper()
        self.store.save(request_id, status, request)
        actions = (["Prepare supplier search using validated requirements."] if not missing
                   else ["Ask the requester to complete: " + ", ".join(missing) + "."])
        return IntakeResult(request_id, status, request, missing, actions, not missing)
