import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import admin  # noqa: E402
import app  # noqa: E402

ADMIN_EMAIL, ADMIN_PW = "boss@example.com", "correct-horse-battery"


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def call(self, method, path, payload=None, headers=None, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Content-Type": "application/json"} if method != "GET" else {}
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        conn.request(method, path, None if method == "GET" else json.dumps(payload or {}), h)
        res = conn.getresponse()
        data = res.read()
        sc = res.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0] if not sc.split(";")[0].endswith("=") else None
        conn.close()
        return res.status, (data if raw else json.loads(data)), res

    def login(self, email=ADMIN_EMAIL, pw=ADMIN_PW):
        return self.call("POST", "/api/admin/login", {"email": email, "password": pw})


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app.DB = Path(cls.tmp.name) / "t.db"
        app.init_db()
        admin.ensure_admin(ADMIN_EMAIL, ADMIN_PW)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        admin._attempts.clear()
        self.c = Client(self.port)

    def admin_client(self):
        self.assertEqual(self.c.login()[0], 200)
        return self.c

    # ---- auth
    def test_requires_login(self):
        for m, p in [("GET", "/api/admin/users"), ("GET", "/api/admin/overview"), ("GET", "/api/admin/audit"),
                     ("POST", "/api/admin/users"), ("PUT", "/api/admin/settings")]:
            self.assertEqual(self.c.call(m, p)[0], 401, (m, p))

    def test_login_wrong_password_and_lockout(self):
        for _ in range(5):
            self.assertEqual(self.c.login(pw="wrong-password-1")[0], 401)
        self.assertEqual(self.c.login()[0], 429)  # even the right password is locked out

    def test_login_logout_session(self):
        c = self.admin_client()
        self.assertEqual(c.call("GET", "/api/admin/me")[1]["user"]["email"], ADMIN_EMAIL)
        old = c.cookie
        self.assertEqual(c.call("POST", "/api/admin/logout")[0], 200)
        c.cookie = old  # replay the old cookie: must be dead server-side
        self.assertEqual(c.call("GET", "/api/admin/me")[0], 401)

    def test_cookie_flags(self):
        _, _, res = self.c.login()
        sc = res.getheader("Set-Cookie")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Strict", sc)

    def test_csrf_protections(self):
        c = self.admin_client()
        self.assertEqual(c.call("POST", "/api/admin/users", {}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(c.call("POST", "/api/admin/users", {}, {"Origin": "http://evil.example"})[0], 403)

    def test_non_admin_cannot_sign_in(self):
        c = self.admin_client()
        self.assertEqual(c.call("POST", "/api/admin/users", {"name": "V", "email": "v@example.com", "role": "viewer",
                                                            "password": "viewer-password-1"})[0], 201)
        other = Client(self.port)
        self.assertEqual(other.login("v@example.com", "viewer-password-1")[0], 403)
        self.assertEqual(other.call("GET", "/api/admin/users")[0], 401)

    # ---- users
    def test_user_management_rules(self):
        c = self.admin_client()
        s, d, _ = c.call("POST", "/api/admin/users", {"name": "Temp", "email": "Temp@Example.com", "role": "manager"})
        self.assertEqual(s, 201)
        self.assertTrue(len(d["temporary_password"]) >= 10)
        self.assertNotIn("password_hash", d["user"])
        uid = d["user"]["id"]
        self.assertEqual(c.call("POST", "/api/admin/users", {"name": "Dup", "email": "temp@example.com", "role": "viewer"})[0], 409)
        self.assertEqual(c.call("POST", "/api/admin/users", {"name": "W", "email": "w@example.com", "role": "viewer", "password": "short"})[0], 400)
        self.assertEqual(c.call("POST", "/api/admin/users", {"name": "W", "email": "bad", "role": "viewer"})[0], 400)
        self.assertEqual(c.call("POST", "/api/admin/users", {"name": "W", "email": "w@example.com", "role": "root"})[0], 400)
        self.assertEqual(c.call("PATCH", f"/api/admin/users/{uid}", {"role": "viewer"})[1]["user"]["role"], "viewer")
        me_id = c.call("GET", "/api/admin/me")[1]["user"]["id"]
        self.assertEqual(c.call("PATCH", f"/api/admin/users/{me_id}", {"active": False})[0], 400)
        self.assertEqual(c.call("PATCH", f"/api/admin/users/{me_id}", {"role": "viewer"})[0], 400)
        self.assertEqual(c.call("POST", f"/api/admin/users/{uid}/reset-password")[1]["temporary_password"][:1] != "", True)

    def test_cannot_remove_last_admin(self):
        c = self.admin_client()
        s, d, _ = c.call("POST", "/api/admin/users", {"name": "Second", "email": "second@example.com", "role": "admin", "password": "second-admin-pw"})
        sid = d["user"]["id"]
        # second admin signs in and tries to demote the first (the only *other* admin) -> allowed, then first is gone
        c2 = Client(self.port)
        self.assertEqual(c2.login("second@example.com", "second-admin-pw")[0], 200)
        first = c.call("GET", "/api/admin/me")[1]["user"]["id"]
        self.assertEqual(c2.call("PATCH", f"/api/admin/users/{first}", {"active": False})[0], 200)
        # now second is the last active admin and cannot be removed by anyone (self-protection)
        self.assertEqual(c2.call("PATCH", f"/api/admin/users/{sid}", {"role": "viewer"})[0], 400)
        # first admin's session is dead
        self.assertEqual(c.call("GET", "/api/admin/me")[0], 401)
        c2.call("PATCH", f"/api/admin/users/{first}", {"active": True})  # restore for other tests
        c2.call("POST", "/api/admin/logout")

    # ---- suppliers / requests
    def test_supplier_crud(self):
        c = self.admin_client()
        s, d, _ = c.call("POST", "/api/admin/suppliers", {"name": "Acme", "city": "Lagos", "rating": 4.1, "on_time": 90})
        self.assertEqual(s, 201)
        sid = d["supplier"]["id"]
        self.assertEqual(c.call("POST", "/api/admin/suppliers", {"name": "X", "city": "Y", "rating": 9})[0], 400)
        self.assertEqual(c.call("PATCH", f"/api/admin/suppliers/{sid}", {"status": "Suspended"})[1]["supplier"]["status"], "Suspended")
        self.assertEqual(c.call("DELETE", f"/api/admin/suppliers/{sid}")[0], 200)
        self.assertEqual(c.call("DELETE", "/api/admin/suppliers/1")[0], 409)  # seeded supplier has quotes
        self.assertEqual(c.call("DELETE", "/api/admin/suppliers/9999")[0], 404)

    def test_request_admin(self):
        c = self.admin_client()
        s, d, _ = c.call("POST", "/api/requests", {"material": "Nails", "category": "Other", "quantity": 5, "unit": "kg",
                                                  "site": "HQ", "required_by": "2030-01-01"})
        rid = d["id"]
        self.assertEqual(c.call("PATCH", f"/api/admin/requests/{rid}", {"status": "Cancelled", "priority": "Urgent"})[0], 200)
        self.assertEqual(c.call("PATCH", f"/api/admin/requests/{rid}", {"status": "Bogus"})[0], 400)
        self.assertEqual(c.call("DELETE", f"/api/admin/requests/{rid}")[0], 200)

    # ---- policy + orders
    def test_settings_drive_order_approval(self):
        c = self.admin_client()
        self.assertEqual(c.call("PUT", "/api/admin/settings", {"approval_limit": -5})[0], 400)
        self.assertEqual(c.call("PUT", "/api/admin/settings", {"min_supplier_rating": 6})[0], 400)
        try:
            self.assertEqual(c.call("PUT", "/api/admin/settings", {"approval_limit": 1000})[0], 200)
            s, d, _ = c.call("POST", "/api/orders", {"request_id": 4, "supplier_id": 5})  # CrestCon, rating 4.0
            self.assertEqual(s, 201)
            self.assertEqual(d["status"], "Pending approval")
            self.assertTrue(any("approval limit" in r for r in d["reasons"]))
            self.assertEqual(c.call("POST", f"/api/admin/orders/{d['id']}/decision", {"action": "maybe"})[0], 400)
            self.assertEqual(c.call("POST", f"/api/admin/orders/{d['id']}/decision", {"action": "approve", "note": "ok"})[1]["status"], "Approved")
            self.assertEqual(c.call("POST", f"/api/admin/orders/{d['id']}/decision", {"action": "approve"})[0], 409)
        finally:
            c.call("PUT", "/api/admin/settings", {"approval_limit": 2500000})

    def test_suspended_supplier_cannot_receive_orders(self):
        c = self.admin_client()
        c.call("PATCH", "/api/admin/suppliers/2", {"status": "Suspended"})
        try:
            s, d, _ = c.call("POST", "/api/orders", {"request_id": 1, "supplier_id": 2})
            self.assertEqual(s, 400)
            self.assertIn("suspended", d["error"])
        finally:
            c.call("PATCH", "/api/admin/suppliers/2", {"status": "Active"})

    # ---- audit + static
    def test_audit_records_actions(self):
        c = self.admin_client()
        c.call("PUT", "/api/admin/settings", {"min_supplier_rating": 3.5})
        actions = [a["action"] for a in c.call("GET", "/api/admin/audit")[1]["audit"]]
        self.assertIn("login", actions)
        self.assertIn("settings.update", actions)

    def test_admin_page_served(self):
        s, body, res = self.c.call("GET", "/admin", raw=True)
        self.assertEqual(s, 200)
        self.assertIn(b"Administrator sign in", body)
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")

    def test_existing_endpoints_still_work(self):
        self.assertEqual(self.c.call("GET", "/api/dashboard")[0], 200)
        self.assertEqual(self.c.call("GET", "/api/nope")[0], 404)
        self.assertEqual(self.c.call("DELETE", "/api/requests")[0], 404)


if __name__ == "__main__":
    unittest.main()
