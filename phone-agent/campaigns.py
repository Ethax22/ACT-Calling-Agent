"""Outbound campaigns: who to call, when, how often to retry, and what happened.

Modelled on the Indus outbound campaigns (Prakash's recording, 2026-10-21): a campaign
has a name, recipients (a "cohort" from a CSV upload or the leads Google Sheet, with its
columns mapped to the lead's details), a schedule (start and end, allowed days, a daily
calling window in IST, attempts per second), and retry rules (how many, how far apart,
on which results). Numbers on the DND list, or on a campaign's stop list, are never called.

Calls are placed by the Dialler (dialler.py). Until the Vobiz line is wired the "line" is
the browser test page: a due call rings there, answering plays the lead, declining counts
as busy and not answering as no answer. Everything lives in one SQLite file
(data/campaigns.db, git-ignored: it holds lead phone numbers).
"""
import csv
import io
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
DB_PATH = Path(os.environ.get("CAMPAIGNS_DB", Path(__file__).parent / "data" / "campaigns.db"))

LEAD_FIELDS = ("phone", "lead_name", "lead_course", "enquiry_source", "enquiry_date")
RETRY_RESULTS = ("busy", "no_answer", "failed", "short_duration", "provider_error", "internal_error")
SHORT_CALL_SECS = 10        # a connected call shorter than this counts as "short_duration"

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns(
  id TEXT PRIMARY KEY, name TEXT, agent TEXT, connection TEXT, status TEXT, created_at TEXT,
  start_at TEXT, end_at TEXT, days TEXT, window_start TEXT, window_end TEXT, cps REAL,
  max_retries INTEGER, retry_interval_min INTEGER, retry_on TEXT, error_retries TEXT,
  ended_at TEXT);
CREATE TABLE IF NOT EXISTS cohorts(
  id TEXT PRIMARY KEY, campaign_id TEXT, name TEXT, source TEXT, file_name TEXT, uploaded_at TEXT,
  valid INTEGER, invalid INTEGER, mapping TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS recipients(
  id TEXT PRIMARY KEY, campaign_id TEXT, cohort_id TEXT, seq INTEGER, phone TEXT, lead_name TEXT,
  lead_course TEXT, enquiry_source TEXT, enquiry_date TEXT, status TEXT, attempts INTEGER DEFAULT 0,
  error_attempts INTEGER DEFAULT 0, next_attempt_at TEXT, last_attempt_at TEXT, failure_reason TEXT,
  last_call_id TEXT, outcome TEXT, do_not_call TEXT);
CREATE TABLE IF NOT EXISTS attempts(
  id TEXT PRIMARY KEY, campaign_id TEXT, recipient_id TEXT, number INTEGER, started_at TEXT,
  answered_at TEXT, ended_at TEXT, result TEXT, failure_reason TEXT, call_id TEXT, duration_s REAL,
  outcome TEXT);
CREATE TABLE IF NOT EXISTS dnd(phone TEXT PRIMARY KEY, reason TEXT, source TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS stops(campaign_id TEXT, phone TEXT, added_at TEXT, PRIMARY KEY(campaign_id, phone));
CREATE INDEX IF NOT EXISTS rec_campaign ON recipients(campaign_id, status);
CREATE INDEX IF NOT EXISTS att_campaign ON attempts(campaign_id);
"""

_lock = threading.RLock()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=IST)  # a time without a zone is India time


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- phone numbers
def normalize_phone(raw) -> str | None:
    """A 10-digit Indian mobile number, or None. Same rule as rag-backend's leads.py."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else None


def mask(phone: str) -> str:
    return ("XXXXXX" + phone[-4:]) if phone and len(phone) >= 4 else ""


# ---------------------------------------------------------------- recipients: CSV / leads Sheet
_GUESS = {
    "phone": r"phone|mobile|number|contact|whatsapp",
    "lead_name": r"^(?:lead_?)?name$|student|full ?name|first ?name",
    "lead_course": r"course|branch|program|department|interest",
    "enquiry_source": r"source|channel|origin|enquir(?:y|ed)_?(?:via|through)",
    "enquiry_date": r"date|timestamp|time|enquir(?:y|ed)_?(?:on|at)",
}


def suggest_mapping(columns: list[str]) -> dict:
    """Lead field -> the column that most likely holds it (or "" for none)."""
    out, taken = {}, set()
    for field in LEAD_FIELDS:
        exact = next((c for c in columns if c.strip().lower() == field and c not in taken), None)
        guess = exact or next((c for c in columns if c not in taken and re.search(_GUESS[field], c.strip().lower())), None)
        out[field] = guess or ""
        if guess:
            taken.add(guess)
    return out


def parse_csv(text: str) -> dict:
    """Columns and rows of an uploaded CSV (first row = column names)."""
    text = text.lstrip("﻿")
    rows = list(csv.DictReader(io.StringIO(text)))
    columns = [c for c in (rows[0].keys() if rows else next(csv.reader(io.StringIO(text)), [])) if c is not None]
    rows = [{k: (v or "").strip() for k, v in r.items() if k is not None} for r in rows]
    return {"columns": columns, "rows": rows, "suggested": suggest_mapping(columns)}


def sample_csv() -> str:
    return ("phone,lead_name,lead_course,enquiry_source,enquiry_date\n"
            "9876543210,Ravi,B.E. Computer Science,our website,2026-10-04\n"
            "9876501234,Priya,B.E. ECE,a phone call,recently\n")


def leads_sheet_rows() -> dict:
    """The leads Google Sheet (rag-backend's lead capture) as columns and rows. Only read."""
    sheet_id = os.environ.get("LEADS_SHEET_ID", "").strip()
    if not sheet_id:  # the phone agent's .env may not have it; rag-backend's does
        from dotenv import dotenv_values

        import config
        rb = Path(config.VOICE_PIPELINE_DIR).parent / "rag-backend" / ".env"
        sheet_id = (dotenv_values(rb).get("LEADS_SHEET_ID") or "").strip() if rb.exists() else ""
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sheet_id or not creds:
        raise RuntimeError("The leads Sheet is not configured (LEADS_SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON).")
    import gspread
    from google.oauth2.service_account import Credentials

    book = gspread.authorize(Credentials.from_service_account_file(
        creds, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])).open_by_key(sheet_id)
    values = book.sheet1.get_all_values()
    columns = values[0] if values else []
    rows = [{c: (r[i].strip() if i < len(r) else "") for i, c in enumerate(columns)} for r in values[1:]]
    rows = [r for r in rows if any(r.values())]
    mapping = suggest_mapping(columns)
    if "name" in columns:
        mapping["lead_name"] = "name"
    if "preferred_course" in columns:
        mapping["lead_course"] = "preferred_course"
    if "timestamp_ist" in columns:
        mapping["enquiry_date"] = "timestamp_ist"
    return {"columns": columns, "rows": rows, "suggested": mapping}


_SOURCE_WORDS = {"chat": "our website chat", "voice": "our website", "phone": "a phone call"}


def build_recipients(rows: list[dict], mapping: dict) -> tuple[list[dict], list[dict]]:
    """Map rows to leads. Returns (valid, invalid); invalid rows carry a reason. Duplicate
    numbers keep the first row. mapping["_required"] lists lead fields a row must have
    (the wizard's Required column); the phone number is always required."""
    valid, invalid, seen = [], [], set()
    required = [f for f in (mapping.get("_required") or []) if f in LEAD_FIELDS and f != "phone"]
    for i, row in enumerate(rows, start=1):
        get = lambda f: (row.get(mapping.get(f) or "", "") or "").strip()
        missing = next((f for f in required if not get(f)), None)
        if missing and get("phone"):
            invalid.append({"row": i, "reason": f"no {missing.replace('_', ' ')}"})
            continue
        phone = normalize_phone(get("phone"))
        if not get("phone"):
            invalid.append({"row": i, "reason": "no phone number"})
            continue
        if not phone:
            invalid.append({"row": i, "reason": f"not a valid mobile number: {get('phone')[:20]}"})
            continue
        if phone in seen:
            invalid.append({"row": i, "reason": "duplicate number"})
            continue
        seen.add(phone)
        source = get("enquiry_source")
        date = get("enquiry_date")
        m = re.match(r"(\d{4}-\d{2}-\d{2})", date)  # "2026-10-04 12:30:00" from the leads Sheet
        valid.append({"phone": phone, "lead_name": get("lead_name")[:60], "lead_course": get("lead_course")[:80],
                      "enquiry_source": _SOURCE_WORDS.get(source.lower(), source)[:60],
                      "enquiry_date": (m.group(1) if m else date)[:30]})
    return valid, invalid


# ---------------------------------------------------------------- campaigns
DEFAULTS = {
    "agent": "Agni Outbound agent", "connection": "Browser test line",
    "days": "1111110",                 # Monday..Sunday; Sunday off (outbound config)
    "window_start": "09:00", "window_end": "19:00",
    "cps": 0.5, "max_retries": 2, "retry_interval_min": 180,   # outbound config: max 2, 3 hours apart
    "retry_on": ["busy", "no_answer", "failed"],
    "error_retries": {"provider_error": {"max": 3, "interval_min": 5}, "internal_error": {"max": 3, "interval_min": 5}},
}


def _hhmm(value, default):
    return value if isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) else default


def create_campaign(spec: dict) -> dict:
    """spec: name, start_at ("now" or ISO), end_at (ISO, optional: end of today IST), days ("1111110"),
    window_start/end ("HH:MM", IST), cps, max_retries, retry_interval_min, retry_on, error_retries,
    cohort: {name, source ("csv" | "sheet"), file_name, rows, mapping}."""
    name = (spec.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("A campaign name is needed.")
    cohort = spec.get("cohort") or {}
    valid, invalid = build_recipients(cohort.get("rows") or [], cohort.get("mapping") or {})
    if not valid:
        raise ValueError("The recipient list has no valid phone numbers.")
    now = now_utc()
    start = now if spec.get("start_at") in (None, "", "now") else (parse_time(spec.get("start_at")) or now)
    end = parse_time(spec.get("end_at")) or now.astimezone(IST).replace(hour=23, minute=59, second=0, microsecond=0)
    if end <= start:
        raise ValueError("The campaign must end after it starts.")
    days = spec.get("days") if re.fullmatch(r"[01]{7}", str(spec.get("days") or "")) else DEFAULTS["days"]
    if "1" not in days:
        raise ValueError("Pick at least one calling day.")
    retry_on = [r for r in (spec.get("retry_on") or DEFAULTS["retry_on"]) if r in RETRY_RESULTS]
    camp = {
        "id": _new_id("camp"), "name": name, "agent": DEFAULTS["agent"], "connection": DEFAULTS["connection"],
        "status": "scheduled", "created_at": iso(now), "start_at": iso(start), "end_at": iso(end), "days": days,
        "window_start": _hhmm(spec.get("window_start"), DEFAULTS["window_start"]),
        "window_end": _hhmm(spec.get("window_end"), DEFAULTS["window_end"]),
        "cps": max(0.1, min(float(spec.get("cps") or DEFAULTS["cps"]), 10.0)),
        "max_retries": max(0, min(int(spec.get("max_retries") if spec.get("max_retries") is not None else DEFAULTS["max_retries"]), 20)),
        "retry_interval_min": max(1, min(int(spec.get("retry_interval_min") or DEFAULTS["retry_interval_min"]), 1440)),
        "retry_on": json.dumps(retry_on),
        "error_retries": json.dumps(spec.get("error_retries") or DEFAULTS["error_retries"]),
        "ended_at": None,
    }
    if camp["window_end"] <= camp["window_start"]:
        raise ValueError("The calling window must end after it starts.")
    with _lock, db() as con:
        con.execute(f"INSERT INTO campaigns({','.join(camp)}) VALUES({','.join('?' * len(camp))})", list(camp.values()))
        _insert_cohort(con, camp["id"], cohort, valid, invalid)
    return get_campaign(camp["id"])


def _insert_cohort(con, campaign_id: str, cohort: dict, valid: list, invalid: list) -> str:
    cid = _new_id("cohort")
    con.execute("INSERT INTO cohorts VALUES(?,?,?,?,?,?,?,?,?,?)", (
        cid, campaign_id, (cohort.get("name") or cohort.get("file_name") or "Recipients")[:80],
        cohort.get("source") or "csv", (cohort.get("file_name") or "")[:120], iso(now_utc()),
        len(valid), len(invalid), json.dumps(cohort.get("mapping") or {}), "completed"))
    start = con.execute("SELECT COALESCE(MAX(seq), 0) FROM recipients WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
    existing = {r[0] for r in con.execute("SELECT phone FROM recipients WHERE campaign_id=?", (campaign_id,))}
    for i, r in enumerate((r for r in valid if r["phone"] not in existing), start=start + 1):
        con.execute("""INSERT INTO recipients(id, campaign_id, cohort_id, seq, phone, lead_name, lead_course,
                       enquiry_source, enquiry_date, status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (_new_id("rcp"), campaign_id, cid, i, r["phone"], r["lead_name"], r["lead_course"],
                     r["enquiry_source"], r["enquiry_date"], "pending"))
    return cid


def add_cohort(campaign_id: str, cohort: dict) -> dict:
    valid, invalid = build_recipients(cohort.get("rows") or [], cohort.get("mapping") or {})
    if not valid:
        raise ValueError("The recipient list has no valid phone numbers.")
    with _lock, db() as con:
        c = con.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not c or c["status"] in ("completed", "cancelled"):
            raise ValueError("Recipients can only be added to a scheduled or running campaign.")
        _insert_cohort(con, campaign_id, cohort, valid, invalid)
    return get_campaign(campaign_id)


def set_status(campaign_id: str, status: str) -> None:
    with _lock, db() as con:
        con.execute("UPDATE campaigns SET status=?, ended_at=CASE WHEN ? IN ('completed','cancelled') THEN ? ELSE ended_at END "
                    "WHERE id=?", (status, status, iso(now_utc()), campaign_id))


def cancel_campaign(campaign_id: str) -> dict:
    with _lock, db() as con:
        c = con.execute("SELECT status FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not c:
            raise ValueError("No such campaign.")
        if c["status"] in ("completed", "cancelled"):
            raise ValueError(f"The campaign is already {c['status']}.")
    set_status(campaign_id, "cancelled")
    return get_campaign(campaign_id)


def _campaign_row(row) -> dict:
    d = dict(row)
    d["retry_on"] = json.loads(d.get("retry_on") or "[]")
    d["error_retries"] = json.loads(d.get("error_retries") or "{}")
    return d


def get_campaign(campaign_id: str) -> dict | None:
    with db() as con:
        row = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            return None
        camp = _campaign_row(row)
        camp["stats"] = _stats(con, campaign_id)
        camp["cohorts"] = [dict(r) for r in con.execute(
            "SELECT id, name, source, file_name, uploaded_at, valid, invalid, status FROM cohorts WHERE campaign_id=? "
            "ORDER BY uploaded_at", (campaign_id,))]
    return camp


def _stats(con, campaign_id: str) -> dict:
    """Audience size, completion rate (recipients finished) and pick-up rate (attempts answered),
    the three numbers at the top of an Indus campaign page."""
    total = con.execute("SELECT COUNT(*) FROM recipients WHERE campaign_id=?", (campaign_id,)).fetchone()[0]
    done = con.execute("""SELECT COUNT(*) FROM recipients WHERE campaign_id=? AND status NOT IN
                          ('pending','retry','ringing','in_call')""", (campaign_id,)).fetchone()[0]
    att = con.execute("SELECT COUNT(*), SUM(result='connected') FROM attempts WHERE campaign_id=? AND result IS NOT NULL",
                      (campaign_id,)).fetchone()
    by_status = {r[0]: r[1] for r in con.execute(
        "SELECT status, COUNT(*) FROM recipients WHERE campaign_id=? GROUP BY status", (campaign_id,))}
    return {"audience": total, "finished": done, "completion_rate": round(done / total, 4) if total else 0,
            "attempts": att[0] or 0, "connected": att[1] or 0,
            "pickup_rate": round((att[1] or 0) / att[0], 4) if att[0] else 0, "by_status": by_status}


def list_campaigns() -> list[dict]:
    with db() as con:
        rows = [_campaign_row(r) for r in con.execute("SELECT * FROM campaigns ORDER BY created_at DESC")]
        for r in rows:
            r["stats"] = _stats(con, r["id"])
    return rows


def campaign_calls(campaign_id: str, status: str = "") -> list[dict]:
    """The "Attempted calls" table: one row per recipient that has been tried (or skipped)."""
    with db() as con:
        q = """SELECT id, phone, lead_name, lead_course, status, attempts, failure_reason, last_attempt_at,
                      next_attempt_at, last_call_id, outcome, do_not_call FROM recipients WHERE campaign_id=?"""
        args = [campaign_id]
        if status:
            q += " AND status=?"
            args.append(status)
        rows = [dict(r) for r in con.execute(q + " ORDER BY COALESCE(last_attempt_at, '') DESC, seq", args)]
    for r in rows:
        r["phone_masked"] = mask(r.pop("phone"))
    return rows


def campaign_timeline(campaign_id: str) -> list[dict]:
    """Attempts per minute by result, for the campaign's calls chart."""
    with db() as con:
        return [dict(r) for r in con.execute(
            """SELECT substr(started_at, 1, 16) AS minute, result, COUNT(*) AS n FROM attempts
               WHERE campaign_id=? AND result IS NOT NULL GROUP BY minute, result ORDER BY minute""", (campaign_id,))]


def all_attempts(since: str | None = None) -> list[dict]:
    with db() as con:
        q = "SELECT a.*, c.name AS campaign_name FROM attempts a LEFT JOIN campaigns c ON c.id=a.campaign_id"
        args = []
        if since:
            q += " WHERE a.started_at >= ?"
            args.append(since)
        return [dict(r) for r in con.execute(q + " ORDER BY a.started_at", args)]


# ---------------------------------------------------------------- DND and stop lists
def dnd_list() -> list[dict]:
    with db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM dnd ORDER BY added_at DESC")]
    for r in rows:
        r["phone_masked"] = mask(r["phone"])
    return rows


def dnd_add(phone_raw: str, reason: str = "", source: str = "manual") -> bool:
    phone = normalize_phone(phone_raw)
    if not phone:
        return False
    with _lock, db() as con:
        con.execute("INSERT OR IGNORE INTO dnd VALUES(?,?,?,?)", (phone, reason[:200], source[:80], iso(now_utc())))
    return True


def dnd_remove(phone_raw: str) -> bool:
    phone = normalize_phone(phone_raw) or re.sub(r"\D", "", phone_raw or "")
    with _lock, db() as con:
        return con.execute("DELETE FROM dnd WHERE phone=?", (phone,)).rowcount > 0


def stop_add(campaign_id: str, phones: list[str]) -> int:
    added = 0
    with _lock, db() as con:
        for raw in phones:
            phone = normalize_phone(raw)
            if phone:
                added += con.execute("INSERT OR IGNORE INTO stops VALUES(?,?,?)",
                                     (campaign_id, phone, iso(now_utc()))).rowcount
    return added


def stop_count(campaign_id: str) -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM stops WHERE campaign_id=?", (campaign_id,)).fetchone()[0]


def blocked(con, campaign_id: str, phone: str) -> str | None:
    """Why this number must not be called, or None."""
    if con.execute("SELECT 1 FROM dnd WHERE phone=?", (phone,)).fetchone():
        return "dnd"
    if con.execute("SELECT 1 FROM stops WHERE campaign_id=? AND phone=?", (campaign_id, phone)).fetchone():
        return "stopped"
    return None


# ---------------------------------------------------------------- calling window
def in_window(camp: dict, when: datetime) -> bool:
    """Is `when` on an allowed day, inside the daily calling window (IST)?"""
    local = when.astimezone(IST)
    if camp["days"][local.weekday()] != "1":
        return False
    return camp["window_start"] <= local.strftime("%H:%M") < camp["window_end"]
