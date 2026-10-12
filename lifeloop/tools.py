"""The 13 MCP tools. Every tool validates input, persists state, returns honest results, and is audit-logged."""
import datetime as dt, json, re
from . import db
from .summarizer import summarize

class ToolError(Exception): pass
TOOLS = {}

def tool(name, description, props=None, required=(), readonly=False):
    def deco(fn):
        TOOLS[name] = {"fn": fn, "def": {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": readonly}}}
        return fn
    return deco

_T = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
def validate(schema, args):
    if not isinstance(args, dict): raise ToolError("arguments must be an object")
    for k in schema["required"]:
        if k not in args: raise ToolError(f"missing required argument: {k}")
    for k, v in args.items():
        p = schema["properties"].get(k)
        if p is None: raise ToolError(f"unknown argument: {k}")
        t = _T[p["type"]]
        if not isinstance(v, t) or (p["type"] in ("integer", "number") and isinstance(v, bool)):
            raise ToolError(f"{k} must be {p['type']}")
        if "enum" in p and v not in p["enum"]: raise ToolError(f"{k} must be one of {p['enum']}")
        if p["type"] == "string" and len(v) > p.get("maxLength", 500): raise ToolError(f"{k} is too long")
        if "pattern" in p and not re.fullmatch(p["pattern"], v): raise ToolError(f"{k} has invalid format")
        if p["type"] == "integer" and not (p.get("minimum", -10**9) <= v <= p.get("maximum", 10**9)): raise ToolError(f"{k} out of range")

def call_tool(name, args, actor="mcp_client"):
    if name not in TOOLS: raise KeyError(name)
    ok, summary = True, ""
    try:
        validate(TOOLS[name]["def"]["inputSchema"], args or {})
        out = TOOLS[name]["fn"](**(args or {}))
        summary = (out.get("message") or out.get("status") or "ok")[:200] if isinstance(out, dict) else "ok"
        return out
    except ToolError as e:
        ok, summary = False, str(e); raise
    finally:
        if name != "get_audit_log" or not ok:
            db.run("INSERT INTO audit(ts,actor,tool,args,ok,summary) VALUES(?,?,?,?,?,?)",
                   (db.now_iso(), actor, name, json.dumps(args or {})[:500], int(ok), summary))

DATE = {"type": "string", "pattern": r"\d{4}-\d{2}-\d{2}", "description": "YYYY-MM-DD, defaults to today"}
def _date(d): return d or db.today()
def _hm(ts): return ts[11:16]

# ---------- timeline ----------
def build_timeline(date):
    items = []
    for e in db.q("SELECT * FROM events WHERE ts LIKE ?", (date + "%",)):
        items.append({"id": f"event:{e['id']}", "ts": e["ts"], "time": _hm(e["ts"]), "type": "event", "title": "Household activity", "label": e["detail"], "room": e["room"], "simulated": True, "source": "simulated sensor", "source_key": "simulated_sensor", "status": "simulated"})
    for c in db.q("SELECT * FROM checkins WHERE ts LIKE ?", (date + "%",)):
        extra = [x for x in [("ate" if c["ate"] else "has not eaten") if c["ate"] is not None else None,
                             ("took medication (reported)" if c["took_meds"] else "has not taken medication (reported)") if c["took_meds"] is not None else None] if x]
        label = c["response"] + (f" ({', '.join(extra)})" if extra else "")
        items.append({"id": f"checkin:{c['id']}", "ts": c["ts"], "time": _hm(c["ts"]), "type": "checkin", "title": ("Morning" if c["slot"] == "morning" else "Evening" if c["slot"] == "evening" else "Ad-hoc") + " check-in", "label": label, "slot": c["slot"], "feeling": c["feeling"], "simulated": False, "source": "resident response", "source_key": "resident_response", "status": "recorded"})
    for m in db.q("SELECT * FROM med_logs WHERE ts LIKE ?", (date + "%",)):
        ack = m["status"] == "reminder_acknowledged"
        items.append({"id": f"med:{m['id']}", "ts": m["ts"], "time": _hm(m["ts"]), "type": "medication", "title": "Medication report", "label": f"{m['name']}: {m['status'].replace('_', ' ')} (reported by {m['reported_by']}; not verified)", "simulated": False, "source": "acknowledgement" if ack else f"{m['reported_by']} report", "source_key": "resident_acknowledgement" if ack else f"{m['reported_by']}_report", "status": "acknowledged" if ack else "reported"})
    for r in db.q("SELECT * FROM reminders WHERE ack_at LIKE ?", (date + "%",)):
        items.append({"id": f"reminder:{r['id']}", "ts": r["ack_at"], "time": _hm(r["ack_at"]), "type": "reminder", "title": r["title"], "label": "Reminder acknowledged; completion is not verified.", "simulated": False, "source": "acknowledgement", "source_key": "resident_acknowledgement", "status": "acknowledged"})
    return sorted(items, key=lambda i: i["ts"])

@tool("get_timeline", "Household events (simulated), check-ins and medication reports for a day, plus reminders and approval items.", {"date": DATE}, readonly=True)
def get_timeline(date=None):
    d = _date(date)
    acts = db.q("SELECT * FROM actions ORDER BY id DESC LIMIT 20")
    for a in acts: a["payload"] = json.loads(a["payload"]); a["evidence"] = json.loads(a["evidence"])
    return {"date": d, "items": build_timeline(d), "reminders": db.q("SELECT * FROM reminders ORDER BY due_at"), "actions": acts,
            "data_notice": "Household events are SIMULATED demo data. No real sensors or Ring hardware are connected."}

# ---------- inactivity ----------
EXPECTED = [("breakfast", "kitchen", "07:00", "10:00"), ("lunch", "kitchen", "11:30", "14:00")]

@tool("check_inactivity", "List expected activity (e.g. kitchen use around mealtimes) with no record. A gap is a question to ask, not a conclusion.",
      {"date": DATE, "as_of": {"type": "string", "pattern": r"([01]\d|2[0-3]):[0-5]\d", "description": "HH:MM; defaults to the demo clock"}}, readonly=True)
def check_inactivity(date=None, as_of=None):
    d = _date(date); clock = "real" if as_of else "demo"
    as_of = as_of or db.get_setting("demo_as_of")
    findings, ok = [], []
    for name, room, a, b in EXPECTED:
        if as_of < b: continue
        hits = db.q("SELECT id FROM events WHERE room=? AND ts>=? AND ts<=?", (room, f"{d}T{a}:00", f"{d}T{b}:00"))
        eaten = db.q("SELECT id FROM checkins WHERE ate=1 AND ts LIKE ?", (d + "%",))  # elder-reported meal resolves the gap
        if hits or eaten: ok.append({"expectation": name, "evidence": [f"event:{h['id']}" for h in hits] + [f"checkin:{h['id']}" for h in eaten]})
        else:
            last = db.q("SELECT id FROM events WHERE room=? AND ts<? ORDER BY ts DESC LIMIT 1", (room, f"{d}T{a}:00"))
            findings.append({"expectation": name, "status": "no_record",
                "message": f"No {room.replace('_', ' ')} motion recorded between {a} and {b} (as of {as_of}).",
                "unknown": f"Whether {db.get_setting('elder_name')} has had {name} is unknown. Sensors can miss things, or food may have been eaten elsewhere.",
                "suggested_next_step": "Ask the elder first via a check-in. Do not alarm family yet.",
                "evidence": [f"event:{l['id']}" for l in last]})
    return {"date": d, "as_of": as_of, "clock": clock, "findings": findings, "ok": ok}

# ---------- summary ----------
@tool("get_daily_summary", "Evidence-grounded daily summary (Amazon Bedrock when configured, otherwise a labelled local fallback).", {"date": DATE}, readonly=True)
def get_daily_summary(date=None):
    d = _date(date); tl = build_timeline(d); inact = check_inactivity(d)
    unknowns = []
    if not any(i["type"] == "checkin" for i in tl): unknowns.append("No check-in response recorded yet.")
    if not any(i["type"] == "medication" for i in tl): unknowns.append("No medication report recorded; whether tablets were taken is unknown.")
    unknowns += [f["unknown"] for f in inact["findings"]]
    ctx = {"elder_name": db.get_setting("elder_name"), "date": d, "timeline": [{k: i[k] for k in ("id", "time", "type", "title", "label", "source", "source_key", "status")} for i in tl], "findings": inact["findings"], "unknowns": unknowns,
           "upcoming": [f"{r['title']} ({r['due_at'][:16].replace('T', ' ')})" for r in db.q("SELECT * FROM reminders WHERE status='pending' AND due_at>=? ORDER BY due_at", (f"{d}T{inact['as_of']}:00",))]}
    s = summarize(ctx)
    return {"date": d, "summary": s["text"], "summary_source": s["source"], "model": s.get("model"), "fallback_reason": s.get("fallback_reason"),
            "evidence": [i["id"] for i in tl], "findings": inact["findings"], "unknowns": unknowns,
            "sources": sorted({i["source"] for i in tl}), "engine": "amazon_bedrock" if s["source"] == "bedrock" else "local_evidence_summary", "disclaimer": "Simulated household data. Not medical advice."}

# ---------- reports ----------
@tool("record_checkin", "Save what the elder reported at a check-in. A response is a report, not verification.",
      {"slot": {"type": "string", "enum": ["morning", "evening", "ad_hoc"]}, "response": {"type": "string", "maxLength": 500},
       "feeling": {"type": "string", "enum": ["good", "okay", "unwell", "unsure"]}, "ate": {"type": "boolean"}, "took_meds": {"type": "boolean"}}, ["slot", "response"])
def record_checkin(slot, response, feeling=None, ate=None, took_meds=None):
    if not response.strip(): raise ToolError("response must not be empty")
    i = db.run("INSERT INTO checkins(ts,slot,response,feeling,ate,took_meds,reported_by) VALUES(?,?,?,?,?,?,?)",
               (db.now_iso(), slot, response.strip(), feeling, None if ate is None else int(ate), None if took_meds is None else int(took_meds), "elder"))
    msg = "Check-in recorded as reported by the elder (not independently verified)."
    if feeling == "unwell": msg += " The elder said they feel unwell: consider asking a caregiver to review; use draft_family_alert if the family should know."
    return {"id": f"checkin:{i}", "status": "recorded", "message": msg}

@tool("log_medication", "Record caregiver- or elder-REPORTED medication information. Never proof a dose was taken.",
      {"name": {"type": "string", "maxLength": 80}, "status": {"type": "string", "enum": ["reported_taken", "reported_skipped", "reminder_acknowledged"]},
       "reported_by": {"type": "string", "enum": ["caregiver", "elder"]}, "scheduled_time": {"type": "string", "pattern": r"([01]\d|2[0-3]):[0-5]\d"}, "note": {"type": "string", "maxLength": 200}},
      ["name", "status", "reported_by"])
def log_medication(name, status, reported_by, scheduled_time=None, note=None):
    i = db.run("INSERT INTO med_logs(ts,name,scheduled_time,status,reported_by,note) VALUES(?,?,?,?,?,?)", (db.now_iso(), name, scheduled_time, status, reported_by, note))
    return {"id": f"med:{i}", "status": "recorded", "message": f"Recorded '{status}' as reported by {reported_by}. This is a record of a report, not verification. No dosing advice is given."}

@tool("schedule_reminder", "Create a reminder. Medication reminders require caregiver-verified instructions; this tool never produces dosing advice.",
      {"kind": {"type": "string", "enum": ["medication", "appointment", "routine", "meal", "water"]}, "title": {"type": "string", "maxLength": 120},
       "due_at": {"type": "string", "pattern": r"\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?"},
       "instructions_source": {"type": "string", "enum": ["caregiver_verified", "elder_reported", "n/a"]}}, ["kind", "title", "due_at"])
def schedule_reminder(kind, title, due_at, instructions_source="n/a"):
    if kind == "medication" and instructions_source != "caregiver_verified":
        raise ToolError("Medication reminders must come from the caregiver's verified instructions (instructions_source='caregiver_verified'). LifeLoop does not generate dosing advice.")
    if len(due_at) == 16: due_at += ":00"
    i = db.run("INSERT INTO reminders(kind,title,due_at,instructions_source,status,created_at) VALUES(?,?,?,?,?,?)", (kind, title, due_at, instructions_source, "pending", db.now_iso()))
    return {"id": i, "status": "scheduled", "message": f"Reminder scheduled for {due_at}. Delivery is simulated in this demo."}

@tool("acknowledge_reminder", "Record that the elder acknowledged a reminder. Acknowledgement is NOT verified completion.", {"reminder_id": {"type": "integer", "minimum": 1}}, ["reminder_id"])
def acknowledge_reminder(reminder_id):
    r = db.q1("SELECT * FROM reminders WHERE id=?", (reminder_id,))
    if not r: raise ToolError("reminder not found")
    if r["ack_at"]: return {"status": "already_acknowledged", "message": "Already acknowledged; nothing changed."}
    db.run("UPDATE reminders SET ack_at=?, status='acknowledged' WHERE id=?", (db.now_iso(), reminder_id))
    return {"status": "acknowledged", "message": "Acknowledged. This does not verify the task was completed."}

# ---------- approvals ----------
MEALS = [("Khichdi with curd", 120, "vegetarian"), ("Vegetable upma and fruit", 90, "vegetarian"), ("Dal, rice and cooked vegetables", 160, "vegetarian"),
         ("Paneer wrap and buttermilk", 210, "vegetarian"), ("Chicken soup and bread", 230, "non-vegetarian"), ("Egg curry with chapati", 200, "non-vegetarian")]

def _pending_duplicate(type_, kind):
    return db.q1("SELECT id FROM actions WHERE type=? AND kind=? AND status='pending_approval'", (type_, kind))

def _new_action(type_, kind, payload, evidence):
    return db.run("INSERT INTO actions(type,kind,status,payload,evidence,created_at) VALUES(?,?,?,?,?,?)", (type_, kind, "pending_approval", json.dumps(payload), json.dumps(evidence), db.now_iso()))

@tool("suggest_meal", "Suggest a meal within the elder's preferences and budget, then wait for approval. Only after the elder said they want food.",
      {"elder_wants_food": {"type": "boolean", "description": "Must be true: ask the elder first."}, "budget_inr": {"type": "integer", "minimum": 20, "maximum": 5000}, "diet": {"type": "string", "enum": ["vegetarian", "non-vegetarian"]}})
def suggest_meal(elder_wants_food=False, budget_inr=None, diet=None):
    name = db.get_setting("elder_name")
    if not elder_wants_food:
        return {"status": "needs_confirmation", "message": f"Not suggesting food yet. Ask {name} first whether they have eaten and whether they want something."}
    prefs = db.get_setting("meal_prefs"); budget = budget_inr or prefs["budget_inr"]; diet = diet or prefs["diet"]
    opts = [{"meal": m, "estimated_cost_inr": c} for m, c, dd in MEALS if c <= budget and (diet == "non-vegetarian" or dd == "vegetarian")][:3]
    if not opts: raise ToolError("No catalogue meal fits that budget and diet.")
    if _pending_duplicate("meal_suggestion", "lunch"): return {"status": "duplicate_pending", "duplicate": True, "message": "A meal suggestion is already waiting for approval."}
    aid = _new_action("meal_suggestion", "lunch", {"options": opts, "recommended": opts[0], "diet": diet, "budget_inr": budget}, [f["evidence"] for f in check_inactivity()["findings"]])
    return {"action_id": aid, "status": "pending_approval", "options": opts, "message": "Suggestion saved. Estimated costs only. Nothing is ordered; approve or reject with approve_alert."}

@tool("draft_family_alert", "Draft an alert for authorised family contacts with its evidence. Nothing is sent without approval; SOS here is a DEMO draft.",
      {"kind": {"type": "string", "enum": ["inactivity", "missed_checkin", "daily_update", "sos"]}, "note": {"type": "string", "maxLength": 200}}, ["kind"])
def draft_family_alert(kind, note=None):
    st = {k: db.get_setting(k) for k in ("contacts", "care_consent", "notification_pref", "elder_name")}
    name = st["elder_name"]
    if not st["care_consent"] and kind != "sos": raise ToolError("Care coordination consent is switched off in settings. Turn it on before drafting family messages.")
    if kind in ("inactivity", "missed_checkin") and st["notification_pref"] == "none": raise ToolError("Notification preference is 'none': only SOS and daily updates can be drafted.")
    if kind == "missed_checkin" and st["notification_pref"] == "important_only": raise ToolError("Notification preference is 'important items only': a missed check-in alone is not drafted. Change it in settings if wanted.")
    to = [c["name"] for c in st["contacts"] if c["view_summary" if kind == "daily_update" else "alerts"]]
    if not to: raise ToolError("No contact has consented to receive this. Update consent in settings first.")
    if _pending_duplicate("family_alert", kind): return {"status": "duplicate_pending", "duplicate": True, "message": "A message of this kind is already waiting for approval."}
    f = check_inactivity()["findings"]
    if kind == "sos":
        msg, ev = f"SOS requested for {name}. Please call {name} now.", [i["id"] for i in build_timeline(db.today())[-3:]]
    elif kind == "daily_update":
        sm = get_daily_summary(); msg, ev = "Daily update for family review: " + sm["summary"].replace("\n", " ")[:600], sm["evidence"]
    elif kind == "inactivity":
        msg, ev = ("; ".join(x["message"] for x in f) or "No expected-activity gap found.") + f" This is a missing record, not a confirmed problem. Consider calling {name}.", [e for x in f for e in x["evidence"]]
    else:
        msg, ev = f"No check-in response from {name} yet today. This may just mean they have not seen the prompt.", []
    if note: msg += f" Note: {note}"
    qh = db.get_setting("quiet_hours"); hm = db.now_iso()[11:16]
    quiet = (hm >= qh["start"] or hm < qh["end"]) if qh["start"] > qh["end"] else (qh["start"] <= hm < qh["end"])
    held = quiet and kind not in ("sos", "daily_update")
    aid = _new_action("family_alert", kind, {"recipients": to, "message": msg, "quiet_hours_hold": held, "demo": True}, ev)
    status_note = "DRAFT ONLY. No emergency services contacted. No message sent." if kind == "sos" else "Draft only. Nothing sent."
    return {"action_id": aid, "status": "pending_approval", "recipients": to, "message_draft": msg, "evidence": ev,
            "quiet_hours_note": "Would be held until quiet hours end (non-urgent)." if held else None, "message": status_note}

@tool("approve_alert", "Record the caregiver's explicit approve/reject decision on a pending alert or meal suggestion. Idempotent.",
      {"action_id": {"type": "integer", "minimum": 1}, "decision": {"type": "string", "enum": ["approve", "reject"]}, "note": {"type": "string", "maxLength": 200}}, ["action_id", "decision"])
def approve_alert(action_id, decision, note=None):
    a = db.q1("SELECT * FROM actions WHERE id=?", (action_id,))
    if not a: raise ToolError("action not found")
    if a["status"] != "pending_approval": return {"action_id": action_id, "status": a["status"], "duplicate": True, "message": f"Already decided ({a['status']}); nothing changed."}
    status = "approved_demo_only" if decision == "approve" else "rejected"
    db.run("UPDATE actions SET status=?, decided_at=?, decision_note=? WHERE id=?", (status, db.now_iso(), note, action_id))
    if decision == "reject": return {"action_id": action_id, "status": status, "message": "Rejected. No action taken."}
    what = "No alert was actually sent to anyone (demo adapter)." if a["type"] == "family_alert" else "No order was placed (demo adapter)."
    if a["kind"] == "sos": what += " No emergency services were contacted."
    return {"action_id": action_id, "status": status, "delivery": "simulated", "message": "Approval recorded. " + what}

# ---------- settings / audit ----------
@tool("get_settings", "Consent, quiet hours, check-in times and meal preferences.", readonly=True)
def get_settings():
    return {k: db.get_setting(k) for k in db.DEFAULT_SETTINGS}

@tool("update_settings", "Update consent (which contacts get alerts/summaries), quiet hours and check-in times.",
      {"contacts": {"type": "array"}, "quiet_hours": {"type": "object"}, "checkin_times": {"type": "object"}, "elder_name": {"type": "string", "maxLength": 40},
       "caregiver_name": {"type": "string", "maxLength": 40}, "notification_pref": {"type": "string", "enum": ["all", "important_only", "none"]}, "care_consent": {"type": "boolean"}})
def update_settings(contacts=None, quiet_hours=None, checkin_times=None, elder_name=None, caregiver_name=None, notification_pref=None, care_consent=None):
    hm = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
    for key, val in (("elder_name", elder_name), ("caregiver_name", caregiver_name)):
        if val is not None:
            if not val.strip(): raise ToolError(f"{key} must not be empty")
            db.set_setting(key, val.strip())
    if notification_pref is not None: db.set_setting("notification_pref", notification_pref)
    if care_consent is not None: db.set_setting("care_consent", care_consent)
    if contacts is not None:
        for c in contacts:
            if not (isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("alerts"), bool) and isinstance(c.get("view_summary"), bool)): raise ToolError("each contact needs name, alerts, view_summary")
        db.set_setting("contacts", contacts)
    for key, val, ks in (("quiet_hours", quiet_hours, ("start", "end")), ("checkin_times", checkin_times, ("morning", "evening"))):
        if val is not None:
            if set(val) != set(ks) or not all(isinstance(val[k], str) and hm.fullmatch(val[k]) for k in ks): raise ToolError(f"{key} needs {ks} as HH:MM")
            db.set_setting(key, val)
    return {"status": "updated", "message": "Settings saved.", "settings": get_settings()}

@tool("get_audit_log", "Recent tool calls, approvals and outcomes.", {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}, readonly=True)
def get_audit_log(limit=30):
    return {"entries": db.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))}
