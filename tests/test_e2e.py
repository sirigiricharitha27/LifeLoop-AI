"""End-to-end test: a real MCP session over HTTP (initialize -> tools/list -> tools/call)."""
import json, os, tempfile, threading, unittest, urllib.request, urllib.error
os.environ["LIFELOOP_DB"] = tempfile.mktemp(suffix=".db")
from lifeloop.mcp_server import make_server

class MCP:
    def __init__(self, base): self.base, self.sid, self.n = base, None, 0
    def post(self, body, headers=None):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid: h["Mcp-Session-Id"] = self.sid
        h.update(headers or {})
        req = urllib.request.Request(self.base + "/mcp", json.dumps(body).encode(), h)
        try: r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e: return e.code, None
        self.sid = r.headers.get("Mcp-Session-Id") or self.sid
        raw = r.read(); return r.status, (json.loads(raw) if raw else None)
    def rpc(self, method, params=None):
        self.n += 1; return self.post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
    def call(self, name, **args):
        _, j = self.rpc("tools/call", {"name": name, "arguments": args}); return j["result"]

class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = make_server(port=0); cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def test_full_flow(self):
        c = MCP(self.base)
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0], 400)       # no session yet
        s, j = c.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(j["result"]["protocolVersion"], "2025-11-25"); self.assertTrue(c.sid)
        self.assertEqual(c.post({"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)
        names = [t["name"] for t in c.rpc("tools/list")[1]["result"]["tools"]]
        self.assertEqual(len(names), 13)
        # summary + inactivity, grounded in evidence
        r = c.call("get_daily_summary")["structuredContent"]
        self.assertIn(r["summary_source"], ("bedrock", "local_fallback")); self.assertTrue(r["evidence"]); self.assertTrue(r["findings"])
        self.assertIn("lunch", r["findings"][0]["expectation"])
        # asks before acting
        self.assertEqual(c.call("suggest_meal")["structuredContent"]["status"], "needs_confirmation")
        m = c.call("suggest_meal", elder_wants_food=True)["structuredContent"]; self.assertEqual(m["status"], "pending_approval")
        self.assertTrue(c.call("suggest_meal", elder_wants_food=True)["structuredContent"]["duplicate"])   # duplicate prevention
        a = c.call("approve_alert", action_id=m["action_id"], decision="approve")["structuredContent"]
        self.assertEqual(a["delivery"], "simulated"); self.assertIn("No order was placed", a["message"])
        self.assertTrue(c.call("approve_alert", action_id=m["action_id"], decision="approve")["structuredContent"]["duplicate"])
        # SOS honesty
        s = c.call("draft_family_alert", kind="sos")["structuredContent"]; self.assertIn("No emergency services contacted", s["message"])
        d = c.call("approve_alert", action_id=s["action_id"], decision="approve")["structuredContent"]; self.assertIn("No emergency services were contacted", d["message"])
        # validation + safety rules
        self.assertTrue(c.call("schedule_reminder", kind="medication", title="x", due_at="2030-01-01T09:00")["isError"])
        self.assertFalse(c.call("schedule_reminder", kind="medication", title="x", due_at="2030-01-01T09:00", instructions_source="caregiver_verified")["isError"])
        self.assertTrue(c.call("record_checkin", slot="morning", response="ok", bogus=1)["isError"])
        self.assertTrue(c.call("record_checkin", slot="noon", response="ok")["isError"])
        # new: daily update for family review, structured summary, notification preference, care consent
        self.assertIn("source: simulated_sensor", r["summary"]); self.assertEqual(r["engine"], "local_evidence_summary")
        du = c.call("draft_family_alert", kind="daily_update")["structuredContent"]; self.assertEqual(du["recipients"], ["Charitha (caregiver)"])
        self.assertEqual(c.call("get_settings")["structuredContent"]["caregiver_name"], "Charitha")
        self.assertTrue(c.call("draft_family_alert", kind="missed_checkin")["isError"])           # important_only blocks missed_checkin
        c.call("update_settings", care_consent=False); self.assertTrue(c.call("draft_family_alert", kind="inactivity")["isError"])
        c.call("update_settings", care_consent=True)
        # consent: no consenting contacts -> no alert draft
        cs = c.call("get_settings")["structuredContent"]["contacts"]
        c.call("update_settings", contacts=[dict(x, alerts=False) for x in cs])
        self.assertTrue(c.call("draft_family_alert", kind="inactivity")["isError"])
        # check-in then lunch gap resolves when elder reports eating
        c.call("record_checkin", slot="ad_hoc", response="I had lunch", feeling="good", ate=True)
        self.assertFalse(c.call("check_inactivity")["structuredContent"]["findings"])
        self.assertGreater(len(c.call("get_audit_log")["structuredContent"]["entries"]), 8)
        self.assertEqual(c.rpc("nope")[1]["error"]["code"], -32601)

    def test_origin_blocked(self):
        c = MCP(self.base); c.rpc("initialize", {"protocolVersion": "2025-11-25"})
        self.assertEqual(c.post({"jsonrpc": "2.0", "id": 9, "method": "ping"}, {"Origin": "https://evil.example"})[0], 403)

if __name__ == "__main__": unittest.main()
