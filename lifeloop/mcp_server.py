"""Self-hosted MCP server: JSON-RPC 2.0 over Streamable HTTP (spec 2025-11-25), single /mcp endpoint.
Implements initialize, notifications/initialized, ping, tools/list, tools/call, session ids, Origin validation.
Standard library only, so it runs anywhere Python 3.9+ runs."""
import json, mimetypes, os, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from . import db, tools

VERSION = "1.1.0"
SUPPORTED = ["2025-11-25", "2025-06-18", "2025-03-26"]
SESSIONS = set()
STATIC = os.path.join(db.ROOT, "static")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

def rpc_error(id_, code, msg): return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}

def handle(msg, session_id):
    """Returns (response_dict_or_None). None means notification/response -> HTTP 202."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0": return rpc_error(None, -32600, "Invalid Request")
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None: return None                      # a client response; nothing to do
    if id_ is None: return None                         # notification (e.g. notifications/initialized)
    if method == "ping": return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": [t["def"] for t in tools.TOOLS.values()]}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in tools.TOOLS: return rpc_error(id_, -32602, f"Unknown tool: {name}")
        try:
            out = tools.call_tool(name, args, actor=f"mcp_session:{(session_id or '')[:8]}")
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": json.dumps(out)}], "structuredContent": out, "isError": False}}
        except tools.ToolError as e:
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        except Exception as e:  # never leak internals
            return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": f"Internal error: {type(e).__name__}"}], "isError": True}}
    return rpc_error(id_, -32601, f"Method not found: {method}")

class Handler(BaseHTTPRequestHandler):
    server_version = "LifeLoopMCP/1.0"
    def log_message(self, *a): pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def _origin_ok(self):
        o = self.headers.get("Origin")
        if not o: return True
        host = urlparse(o).hostname or ""
        allowed = {h.strip() for h in os.environ.get("ALLOWED_ORIGINS", "").split(",") if h.strip()}
        return host in LOCAL_HOSTS or o in allowed

    def do_OPTIONS(self): self._send(204)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/mcp": return self._send(405, b'{"error":"This server does not offer a GET SSE stream"}', headers={"Allow": "POST, DELETE"})
        if path == "/api/health":
            return self._send(200, json.dumps({"status": "ok", "version": VERSION, "bedrock_configured": bool(os.environ.get("BEDROCK_MODEL_ID")), "tools": len(tools.TOOLS)}).encode())
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full): return self._send(404, b"Not found", "text/plain")
        with open(full, "rb") as f: self._send(200, f.read(), mimetypes.guess_type(full)[0] or "application/octet-stream", {"Cache-Control": "no-store"})

    def do_DELETE(self):
        if urlparse(self.path).path != "/mcp": return self._send(404)
        sid = self.headers.get("Mcp-Session-Id")
        if sid in SESSIONS: SESSIONS.discard(sid); return self._send(200)
        self._send(404)

    def do_POST(self):
        if urlparse(self.path).path != "/mcp": return self._send(404)
        if not self._origin_ok(): return self._send(403, json.dumps(rpc_error(None, -32600, "Origin not allowed")).encode())
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 100_000: return self._send(413)
            msg = json.loads(self.rfile.read(n) or b"null")
        except Exception:
            return self._send(400, json.dumps(rpc_error(None, -32700, "Parse error")).encode())
        if isinstance(msg, list): return self._send(400, json.dumps(rpc_error(None, -32600, "Batching is not supported")).encode())
        sid = self.headers.get("Mcp-Session-Id")
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            p = msg.get("params") or {}
            ver = p.get("protocolVersion") if p.get("protocolVersion") in SUPPORTED else SUPPORTED[0]
            sid = uuid.uuid4().hex; SESSIONS.add(sid)
            res = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "lifeloop-ai", "title": "LifeLoop AI", "version": "1.0.0"},
                   "instructions": "Proactive, permission-based caregiving. Household events are simulated. Ask the elder before alarming family; nothing is sent without approval."}}
            return self._send(200, json.dumps(res).encode(), headers={"Mcp-Session-Id": sid})
        if not sid: return self._send(400, json.dumps(rpc_error(None, -32600, "Missing Mcp-Session-Id header")).encode())
        if sid not in SESSIONS: return self._send(404, json.dumps(rpc_error(None, -32600, "Unknown session; re-initialize")).encode())
        res = handle(msg, sid)
        if res is None: return self._send(202)
        self._send(200, json.dumps(res).encode())

def scheduler_loop(stop):
    """Simulated delivery: marks due reminders and logs check-in prompts. No real notifications are sent."""
    while not stop.wait(15):
        try:
            now = db.now_iso()
            for r in db.q("SELECT id,title FROM reminders WHERE status='pending' AND due_at<=?", (now,)):
                db.run("UPDATE reminders SET status='due_simulated' WHERE id=?", (r["id"],))
                db.run("INSERT INTO audit(ts,actor,tool,args,ok,summary) VALUES(?,?,?,?,?,?)", (now, "scheduler", "reminder_due", json.dumps({"id": r["id"]}), 1, f"Reminder due (simulated delivery): {r['title']}"[:200]))
            for slot, t in (db.get_setting("checkin_times") or {}).items():
                tag = f"checkin_prompt_due:{slot}:{db.today()}"
                if now[11:16] >= t and not db.q1("SELECT 1 FROM audit WHERE tool=?", (tag,)):
                    db.run("INSERT INTO audit(ts,actor,tool,args,ok,summary) VALUES(?,?,?,?,?,?)", (now, "scheduler", tag, "{}", 1, f"{slot} check-in prompt due (simulated)"))
        except Exception:
            pass

def make_server(host="127.0.0.1", port=8000):
    db.init()
    return ThreadingHTTPServer((host, port), Handler)

def main():
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    srv = make_server(host, port); stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(stop,), daemon=True).start()
    print(f"LifeLoop AI v{VERSION} (sidebar UI) from {db.ROOT}")
    print(f"-> http://{host}:{port}   MCP endpoint: http://{host}:{port}/mcp   (Ctrl+C to stop)")
    print("Bedrock:", "configured (" + os.environ["BEDROCK_MODEL_ID"] + ")" if os.environ.get("BEDROCK_MODEL_ID") else "not configured, using labelled local fallback")
    try: srv.serve_forever()
    except KeyboardInterrupt: stop.set()

if __name__ == "__main__": main()
