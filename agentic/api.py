"""HTTP API for the ProcureAI intake agent. Run: python -m agentic.api"""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .intake import IntakeAgent, SQLiteRequestStore

STORE = SQLiteRequestStore()
AGENT = IntakeAgent(STORE)

class APIHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, data: dict) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def do_POST(self):
        if urlparse(self.path).path != "/api/intake":
            return self._send(404, {"error": "Not found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 100_000:
                return self._send(413, {"error": "Request too large"})
            data = json.loads(self.rfile.read(size) or b"{}")
            result = AGENT.process(data.get("description", ""))
            req = result.request
            return self._send(201, {
                "request_id": result.request_id, "status": result.status,
                "request": {"description": req.description, "item": req.item,
                    "quantity": str(req.quantity) if req.quantity is not None else None,
                    "unit": req.unit, "budget": str(req.budget) if req.budget is not None else None,
                    "currency": req.currency, "delivery_location": req.delivery_location,
                    "required_within_days": req.required_within_days, "category": req.category},
                "missing_fields": result.missing_fields,
                "next_actions": result.next_actions, "sourcing_ready": result.sourcing_ready})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._send(400, {"error": str(exc)})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/requests":
            return self._send(200, {"requests": STORE.list()})
        if path.startswith("/api/requests/"):
            result = STORE.get(path.rsplit("/", 1)[-1])
            return self._send(200, result) if result else self._send(404, {"error": "Request not found"})
        return self._send(200, {"service": "ProcureAI Agentic Intake API",
            "location": "Port Harcourt, Rivers State, Nigeria",
            "available_endpoints": ["POST /api/intake", "GET /api/requests", "GET /api/requests/{id}"]})

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

def main():
    print("ProcureAI Intake API: http://127.0.0.1:8000")
    ThreadingHTTPServer(("127.0.0.1", 8000), APIHandler).serve_forever()

if __name__ == "__main__":
    main()
