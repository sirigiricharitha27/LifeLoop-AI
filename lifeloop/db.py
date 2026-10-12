"""SQLite persistence + simulated household event feed (clearly labelled simulated)."""
import datetime as dt, json, os, sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("LIFELOOP_DB", os.path.join(ROOT, "lifeloop.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, source TEXT, kind TEXT, room TEXT, detail TEXT, simulated INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS checkins(id INTEGER PRIMARY KEY, ts TEXT, slot TEXT, response TEXT, feeling TEXT, ate INTEGER, took_meds INTEGER, reported_by TEXT);
CREATE TABLE IF NOT EXISTS med_logs(id INTEGER PRIMARY KEY, ts TEXT, name TEXT, scheduled_time TEXT, status TEXT, reported_by TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, kind TEXT, title TEXT, due_at TEXT, instructions_source TEXT, status TEXT, ack_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY, type TEXT, kind TEXT, status TEXT, payload TEXT, evidence TEXT, created_at TEXT, decided_at TEXT, decision_note TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, tool TEXT, args TEXT, ok INTEGER, summary TEXT);
"""

DEFAULT_SETTINGS = {
    "elder_name": "Maya",
    "caregiver_name": "Charitha",
    "notification_pref": "important_only",   # all | important_only | none
    "care_consent": True,
    "contacts": [{"name": "Charitha (caregiver)", "alerts": True, "view_summary": True},
                 {"name": "Ravi (neighbour)", "alerts": False, "view_summary": False}],
    "quiet_hours": {"start": "22:00", "end": "07:00"},
    "checkin_times": {"morning": "08:00", "evening": "19:00"},
    "meal_prefs": {"diet": "vegetarian", "budget_inr": 250},
    "demo_as_of": "15:00",   # demo clock so the lunch-gap scenario is reproducible at any real time
}

def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c

def now_iso(): return dt.datetime.now().isoformat(timespec="seconds")
def today(): return dt.date.today().isoformat()

def q(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]

def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None

def run(sql, args=()):
    c = conn()
    try:
        with c:
            return c.execute(sql, args).lastrowid
    finally:
        c.close()

def get_setting(key):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else DEFAULT_SETTINGS.get(key)

def set_setting(key, value):
    run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

SCHEMA_V = 2   # bump = demo database from an older build is rebuilt (demo data only)

def init(reset=False):
    if os.path.exists(DB_PATH):
        old = False
        try:
            c = conn(); r = c.execute("SELECT value FROM settings WHERE key='schema_v'").fetchone(); old = r is None or int(json.loads(r[0])) < SCHEMA_V; c.close()
        except sqlite3.Error:
            old = True
        if reset or old: os.remove(DB_PATH)
    c = conn(); c.executescript(SCHEMA); c.commit(); c.close()
    set_setting("schema_v", SCHEMA_V)
    if not q1("SELECT 1 FROM settings LIMIT 1"):
        for k, v in DEFAULT_SETTINGS.items(): set_setting(k, v)
    if not q1("SELECT 1 FROM events LIMIT 1"): seed_events()

def seed_events():
    """SIMULATED household feed. No real sensors or Ring hardware are connected."""
    d = today()
    feed = [("06:55", "bedroom", "motion", "Bedroom motion"), ("07:10", "bathroom", "motion", "Bathroom motion"),
            ("07:40", "kitchen", "motion", "Kitchen motion"), ("08:05", "kitchen", "motion", "Kitchen motion"),
            ("09:30", "living_room", "motion", "Living room motion"), ("10:20", "front_door", "door", "Front door opened"),
            ("10:50", "front_door", "door", "Front door opened"), ("11:00", "living_room", "motion", "Living room motion"),
            ("12:30", "living_room", "motion", "Living room motion")]
    for t, room, kind, detail in feed:
        run("INSERT INTO events(ts,source,kind,room,detail,simulated) VALUES(?,?,?,?,?,1)",
            (f"{d}T{t}:00", "simulated_feed", kind, room, detail))
    run("INSERT INTO reminders(kind,title,due_at,instructions_source,status,created_at) VALUES(?,?,?,?,?,?)",
        ("medication", "Morning medication reminder", f"{d}T09:00:00", "caregiver_verified", "pending", now_iso()))
    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    run("INSERT INTO reminders(kind,title,due_at,instructions_source,status,created_at) VALUES(?,?,?,?,?,?)",
        ("appointment", "Doctor follow-up visit", f"{tomorrow}T11:00:00", "n/a", "pending", now_iso()))
