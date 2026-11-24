# ProcureAI — Browser App

A lightweight browser-based procurement workspace built around the workflows in the `procureai_v1` Python CLI repository.

## Run locally

Requires Python 3.9+ and no third-party packages.

```bash
python app.py
```

Open http://127.0.0.1:8000

Stop with Ctrl+C. Data persists in `procureai.db` in this folder.

## Included workflows

- Procurement dashboard and operational metrics
- Create and search purchase requests
- Supplier directory with ratings and on-time delivery performance
- Capture supplier quotations against purchase requests
- Compare offers using deterministic total-cost calculations
- Generate purchase orders from selected supplier offers
- Human approval/rejection for orders that breach approval policy

## Important notes

- The app starts with sample suppliers, requests and offers so the screens are populated on first launch.
- Seed offers are illustrative demo data, not live market quotes.
- TCO calculations are deterministic: base price + tax - payment-term financing benefit + delivery-delay risk + supplier-reliability risk; freight is added once at order level.
- This browser app is a standalone first integration layer. It does not yet call the original CLI package modules; the existing CLI repository still needs its package-layout and syntax repairs before the two layers can share one engine.
- No real RFQs are emailed and no purchase order is sent to a supplier. Users retain final award and approval control.

## Project layout

```text
procureai_app/
  app.py
  README.md
  static/
    index.html
    styles.css
    app.js
```
