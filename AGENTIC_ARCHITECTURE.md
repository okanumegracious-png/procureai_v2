# ProcureAI — Agentic Enterprise Architecture

## Business configuration
- Operating base: Port Harcourt, Rivers State, Nigeria.
- Currency: NGN.
- Supplier sourcing: external discovery first, enriched by the internal supplier register.
- Autonomy: agents may perform reversible routine work; human approval is required before supplier award, purchase-order release, payment, or any binding commitment.

## Architecture

```text
Web/mobile dashboard
        |
Python API + authentication + audit log
        |
Procurement Orchestrator (state machine)
        |
  +-----+----------------+------------------+
  |                      |                  |
Intake Agent        Supplier Discovery   Document Agent
                    Agent                (BOQ/RFQ/quotes)
  +----------------------+------------------+
                         |
                Evaluation / TCO engine
                (deterministic Python)
                         |
                Recommendation Agent
                         |
              Human approval checkpoint
                         |
              PO / ERP / notifications
```

## First implemented vertical slice: Request Intake Agent

- Accepts a plain-language procurement description.
- Extracts item, quantity, unit, budget, delivery location and deadline.
- Uses Port Harcourt as the configured operating location when no destination is specified.
- Identifies missing fields and labels the request `needs_clarification` or `ready_for_sourcing`.
- Persists each request in SQLite and exposes JSON API endpoints.
- Does not contact suppliers or create purchase orders yet.

## Run

```bash
python -m agentic.api
```

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/intake \
  -H "Content-Type: application/json" \
  -d '{"description":"I need 500 bags of cement for a project in Port Harcourt, delivery within 5 days, budget NGN 8 million."}'
```

List saved requests: `curl http://127.0.0.1:8000/api/requests`

## Safety policy

Intake only creates a draft request. Supplier discovery, quotation requests, supplier selection, purchase orders and payments are separate capabilities. Record the actor, input, tool calls, result and approval state. Financial commitments and supplier awards always stop at a human approval gate.

## Next stages

1. Supplier discovery connectors with source URLs, evidence timestamps and verification status.
2. RFQ generation and outbound email after user confirmation.
3. BOQ and quotation extraction with confidence scores and human review for low-confidence fields.
4. Integrate the deterministic TCO and allocation engine.
5. Recommendation and approval work queue.
6. Purchase-order generation, delivery tracking, spend analytics and role-based access.
