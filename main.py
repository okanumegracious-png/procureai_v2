import os
import json
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
import pypdf
import io

app = FastAPI(title="ProcureAI Backend Engine", version="2.0.0")

# CORS for Vercel Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:3000"), "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Security: Webhook / API Key Authentication ---
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
EXPECTED_API_KEY = os.getenv("PROCUREAI_WEBHOOK_SECRET", "prod_sec_procureai_2026_x99")

async def verify_api_key(api_key: str = Security(api_key_header)):
    if not api_key or api_key != EXPECTED_API_KEY:
        raise HTTPException(status_code=403, detail="Unauthorized: Invalid or missing X-API-Key")
    return api_key

# --- In-Memory State Store (Replace with SQLAlchemy / Supabase client in production) ---
DB = {
    "projects": {},
    "boq_items": {},
    "suppliers": [
        {"id": "sup_01", "name": "Dangote Cement & Building", "email": "rfq@dangotebuild.example", "category": "Cement", "rating": 4.9},
        {"id": "sup_02", "name": "African Foundries Steel", "email": "sales@africanfoundries.example", "category": "Steel", "rating": 4.7},
        {"id": "sup_03", "name": "Lafarge Africa Materials", "email": "bids@lafarge.example", "category": "Cement", "rating": 4.8},
        {"id": "sup_04", "name": "Coleman Wires & Cables", "email": "quotes@colemancables.example", "category": "Electrical", "rating": 4.6},
    ],
    "quotations": {},
    "purchase_orders": {}
}

# --- Pydantic Schemas (Strict Output Validation) ---
class BOQItem(BaseModel):
    item_code: str
    item_name: str
    specification: str
    quantity: float
    unit: str
    category: str = "General"

class BOQExtractionResponse(BaseModel):
    success: bool
    project_id: str
    project_summary: str
    items: List[BOQItem]

class SupplierRequest(BaseModel):
    project_id: str
    categories: List[str]
    dispatch_rfq: bool = True

class QuotationLineItem(BaseModel):
    item_code: str
    unit_price: float
    total_price: float

class QuotationSubmission(BaseModel):
    project_id: str
    supplier_id: str
    delivery_days: int
    currency: str = "NGN"
    line_items: List[QuotationLineItem]

class RecommendationRequest(BaseModel):
    project_id: str
    weights: Dict[str, float] = {"price": 0.6, "delivery": 0.2, "rating": 0.2}

class AwardApprovalRequest(BaseModel):
    project_id: str
    quotation_id: str
    approved_by: str

# --- Helper: Document Extraction & AI Normalization ---
def extract_text_from_pdf(file_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    return "\n".join([page.extract_text() or "" for page in reader.pages])

def parse_boq_with_llm(raw_text: str) -> dict:
    """
    Connects to OpenAI / Gemini Structured Outputs.
    Falls back to deterministic demo parser if OPENAI_API_KEY is not set.
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if openai_key:
        from openai import OpenAI
        client = OpenAI(api_key=openai_key)
        completion = client.beta.chat.completions.parse(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Extract all Bill of Quantities (BOQ) items strictly into JSON."},
                {"role": "user", "content": raw_text[:15000]}
            ],
            response_format=BOQExtractionResponse,
        )
        return completion.choices[0].message.parsed.model_dump()

    # Fallback structured response for immediate local testing
    return {
        "project_summary": "Commercial Warehouse Structural & Civil Works",
        "items": [
            {"item_code": "CIV-001", "item_name": "Portland Limestone Cement 50kg", "specification": "Grade 42.5R", "quantity": 600, "unit": "bags", "category": "Cement"},
            {"item_code": "STL-002", "item_name": "High Yield Reinforcement Steel Bars", "specification": "16mm Y16 BS4449", "quantity": 12.5, "unit": "tons", "category": "Steel"},
            {"item_code": "ELC-003", "item_name": "Armoured Copper Power Cable", "specification": "4-Core 35mm2 XLPE/SWA/PVC", "quantity": 450, "unit": "meters", "category": "Electrical"}
        ]
    }

# ==============================================================================
# WORKFLOW 1: Upload BOQ -> Extracted Materials & Quantities
# ==============================================================================
@app.post("/api/v1/workflows/upload-boq", response_model=BOQExtractionResponse, dependencies=[Depends(verify_api_key)])
async def upload_boq(
    project_id: str = Form(...),
    file: UploadFile = File(...)
):
    content = await file.read()
    raw_text = extract_text_from_pdf(content) if file.filename.lower().endswith(".pdf") else content.decode("utf-8", errors="ignore")
    
    extracted = parse_boq_with_llm(raw_text)
    DB["projects"][project_id] = {"id": project_id, "summary": extracted["project_summary"], "status": "boq_extracted"}
    DB["boq_items"][project_id] = extracted["items"]

    return {
        "success": True,
        "project_id": project_id,
        "project_summary": extracted["project_summary"],
        "items": extracted["items"]
    }

# ==============================================================================
# WORKFLOW 2: Request Suppliers -> Supplier Shortlist & RFQ Dispatch
# ==============================================================================
@app.post("/api/v1/workflows/request-suppliers", dependencies=[Depends(verify_api_key)])
async def request_suppliers(payload: SupplierRequest):
    shortlist = [
        s for s in DB["suppliers"]
        if any(cat.lower() in s["category"].lower() for cat in payload.categories)
    ]
    shortlist.sort(key=lambda x: x["rating"], reverse=True)

    rfq_logs = []
    if payload.dispatch_rfq:
        for sup in shortlist:
            # Replace with Resend / SendGrid / SMTP call in production
            rfq_logs.append({"supplier_id": sup["id"], "email": sup["email"], "status": "rfq_sent"})

    return {
        "success": True,
        "project_id": payload.project_id,
        "shortlisted_suppliers": shortlist,
        "rfq_dispatch": rfq_logs
    }

# ==============================================================================
# WORKFLOW 3: Upload Quotations -> Structured Quotation Records
# ==============================================================================
@app.post("/api/v1/workflows/upload-quotation", dependencies=[Depends(verify_api_key)])
async def upload_quotation(payload: QuotationSubmission):
    quote_id = f"qt_{uuid.uuid4().hex[:8]}"
    total_amount = sum(item.total_price for item in payload.line_items)
    record = {
        "quotation_id": quote_id,
        "project_id": payload.project_id,
        "supplier_id": payload.supplier_id,
        "delivery_days": payload.delivery_days,
        "currency": payload.currency,
        "line_items": [i.model_dump() for i in payload.line_items],
        "total_amount": total_amount,
        "status": "submitted",
        "created_at": datetime.utcnow().isoformat()
    }
    DB["quotations"].setdefault(payload.project_id, []).append(record)
    return {"success": True, "quotation": record}

# ==============================================================================
# WORKFLOW 4: Compare Offers -> Bid Comparison Report
# ==============================================================================
@app.get("/api/v1/workflows/compare-offers/{project_id}", dependencies=[Depends(verify_api_key)])
async def compare_offers(project_id: str):
    quotes = DB["quotations"].get(project_id, [])
    if not quotes:
        raise HTTPException(status_code=404, detail="No quotations found for this project")

    supplier_map = {s["id"]: s for s in DB["suppliers"]}
    lowest_total = min(q["total_amount"] for q in quotes)
    fastest_delivery = min(q["delivery_days"] for q in quotes)

    comparison = []
    for q in quotes:
        sup = supplier_map.get(q["supplier_id"], {"name": "Unknown", "rating": 0})
        comparison.append({
            "quotation_id": q["quotation_id"],
            "supplier_id": q["supplier_id"],
            "supplier_name": sup["name"],
            "supplier_rating": sup["rating"],
            "total_amount": q["total_amount"],
            "delivery_days": q["delivery_days"],
            "variance_from_lowest": round(q["total_amount"] - lowest_total, 2),
            "is_lowest_bid": q["total_amount"] == lowest_total,
            "is_fastest_delivery": q["delivery_days"] == fastest_delivery
        })

    return {"success": True, "project_id": project_id, "comparison_report": comparison}

# ==============================================================================
# WORKFLOW 5: Select a Supplier -> Weighted Procurement Recommendation
# ==============================================================================
@app.post("/api/v1/workflows/recommend-supplier", dependencies=[Depends(verify_api_key)])
async def recommend_supplier(payload: RecommendationRequest):
    quotes = DB["quotations"].get(payload.project_id, [])
    if not quotes:
        raise HTTPException(status_code=404, detail="No quotations available to evaluate")

    supplier_map = {s["id"]: s for s in DB["suppliers"]}
    min_price = min(q["total_amount"] for q in quotes)
    min_days = min(q["delivery_days"] for q in quotes)

    scored = []
    for q in quotes:
        sup = supplier_map.get(q["supplier_id"], {"name": "Unknown", "rating": 3.0})
        price_score = (min_price / q["total_amount"]) * 100
        delivery_score = (min_days / max(q["delivery_days"], 1)) * 100
        rating_score = (sup["rating"] / 5.0) * 100

        weighted_score = (
            price_score * payload.weights.get("price", 0.6)
            + delivery_score * payload.weights.get("delivery", 0.2)
            + rating_score * payload.weights.get("rating", 0.2)
        )
        scored.append({
            "quotation_id": q["quotation_id"],
            "supplier_id": q["supplier_id"],
            "supplier_name": sup["name"],
            "weighted_score": round(weighted_score, 2),
            "total_amount": q["total_amount"],
            "delivery_days": q["delivery_days"]
        })

    scored.sort(key=lambda x: x["weighted_score"], reverse=True)
    winner = scored[0]

    return {
        "success": True,
        "recommended": winner,
        "executive_justification": f"{winner['supplier_name']} achieved the highest composite score ({winner['weighted_score']}/100) balancing cost competitiveness ({winner['total_amount']}) and {winner['delivery_days']}-day lead time.",
        "ranking": scored
    }

# ==============================================================================
# WORKFLOW 6: Approve an Award -> Purchase Order Generation
# ==============================================================================
@app.post("/api/v1/workflows/approve-award", dependencies=[Depends(verify_api_key)])
async def approve_award(payload: AwardApprovalRequest):
    quotes = DB["quotations"].get(payload.project_id, [])
    selected = next((q for q in quotes if q["quotation_id"] == payload.quotation_id), None)
    if not selected:
        raise HTTPException(status_code=404, detail="Quotation not found")

    selected["status"] = "awarded"
    po_number = f"PO-{datetime.utcnow().year}-{uuid.uuid4().hex[:6].upper()}"
    po_record = {
        "po_number": po_number,
        "project_id": payload.project_id,
        "quotation_id": payload.quotation_id,
        "supplier_id": selected["supplier_id"],
        "total_amount": selected["total_amount"],
        "currency": selected["currency"],
        "line_items": selected["line_items"],
        "approved_by": payload.approved_by,
        "issued_at": datetime.utcnow().isoformat()
    }
    DB["purchase_orders"][po_number] = po_record
    return {"success": True, "purchase_order": po_record}
