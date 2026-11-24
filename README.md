# ProcureAI — Construction Procurement Workspace

**Live Demo:** [https://procureaiv2-production.up.railway.app/](https://procureaiv2-production.up.railway.app/)

ProcureAI is a zero-dependency, full-stack procurement application tailored for the construction industry. It replaces scattered spreadsheets and WhatsApp messages with a mathematically rigorous, auditable sourcing pipeline.

## 🧠 Core Philosophy
**The AI reads. The math calculates. People approve.**
Decisions are driven by a deterministic Total Cost of Ownership (TCO) engine, ensuring that variables like lead time, payment terms, and supplier reliability are factored into every award recommendation.

## 🛠️ Tech Stack
* **Backend:** Pure Python 3 (`http.server`, `sqlite3`). No external frameworks.
* **Frontend:** Vanilla HTML5, CSS3, JavaScript (ES6).
* **Database:** SQLite (persisted in memory per deployment).
* **Deployment:** Railway.

## ⚙️ How It Works
1. **Demand:** Site managers raise purchase requests (PRs).
2. **Sourcing:** Procurement logs supplier quotations via the UI.
3. **Analysis:** The TCO engine calculates the true landed cost, factoring in:
   - Base price + Tax
   - Financing (Cost of capital vs. payment terms)
   - Delay Risk (Lead time vs. Required-by date)
   - Reliability Risk (Supplier historical on-time rate)
4. **Approval Gate:** POs over ₦2.5M or from low-rated suppliers are routed to the dashboard for human approval.

## 🚀 Run Locally
```bash
python app.py
