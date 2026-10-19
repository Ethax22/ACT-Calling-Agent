"""ERP lookup API for the Indus calling agent (registered there as API Tools).

Rules enforced HERE, not in the prompt — the model only ever sees what this returns:
  * caller's phone must match an ACTIVE contact (missing/garbled caller ID => treated as unknown, no data)
  * student  -> only own record;  parent -> only linked child;  faculty -> only own programme/batch/section
  * student/parent must also pass a second factor (date of birth) — caller ID alone is just a hint
  * 3 failed verifications per number per hour => locked out for that hour
  * every lookup is written to the audit table (no marks/attendance values logged)

Responses carry a short `speech` string written for reading aloud, plus `status` so the agent can branch.
"""
import os
import re
import sqlite3
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

def _load_dotenv():
    """Read erp-lookup/.env (KEY=value lines) so the key survives new terminals/restarts."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_dotenv()
DB_PATH = Path(os.environ.get("ERP_DB", Path(__file__).parent / "erp.db"))
API_KEY = os.environ.get("ERP_API_KEY", "")
MAX_FAILS_PER_HOUR = 3
LOW_ATTENDANCE = 75.0

app = FastAPI(title="ACT ERP lookup")


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def require_key(authorization: str = Header(default="")):
    if not API_KEY or authorization != f"Bearer {API_KEY}":
        raise HTTPException(401, "unauthorized")


def norm_phone(p: str | None) -> str:
    digits = re.sub(r"\D", "", p or "")
    return digits[-10:] if len(digits) >= 10 else ""


def norm_dob(s: str) -> str:
    """Accepts 5-3-2006, 05/03/2006, 05032006, 2006-03-05 -> DDMMYYYY ('' if unparseable)."""
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s)          # YYYY-MM-DD
    if m:
        return f"{int(m.group(3)):02d}{int(m.group(2)):02d}{m.group(1)}"
    m = re.fullmatch(r"(\d{1,2})\D+(\d{1,2})\D+(\d{4})", s)          # D-M-YYYY
    if m:
        return f"{int(m.group(1)):02d}{int(m.group(2)):02d}{m.group(3)}"
    d = re.sub(r"\D", "", s)
    return d if len(d) == 8 else ""


def audit(con, caller, role, reg_no, topic, result):
    con.execute("INSERT INTO audit(caller, role, reg_no, topic, result) VALUES(?,?,?,?,?)",
                (norm_phone(caller) or "unknown", role, reg_no, topic, result))
    con.commit()


def locked_out(con, caller) -> bool:
    n = con.execute("""SELECT COUNT(*) FROM audit WHERE caller=? AND result='verification_failed'
                       AND ts > datetime('now','-1 hour')""", (norm_phone(caller),)).fetchone()[0]
    return n >= MAX_FAILS_PER_HOUR


def find_contact(con, caller):
    phone = norm_phone(caller)
    if not phone and os.environ.get("ERP_TEST_CALLER"):
        # TEST ONLY: browser "Test agent" calls carry no caller number. Remove ERP_TEST_CALLER from .env before go-live,
        # otherwise every call without caller ID would be treated as this contact.
        phone = norm_phone(os.environ["ERP_TEST_CALLER"])
    if not phone:
        return None
    return con.execute("SELECT * FROM contacts WHERE active=1 AND substr(replace(phone,' ',''), -10)=? LIMIT 1", (phone,)).fetchone()


# ---------------------------------------------------------------- identify
class IdentifyIn(BaseModel):
    caller_number: str | None = ""


@app.post("/identify", dependencies=[Depends(require_key)])
def identify(body: IdentifyIn):
    """Call at the start of a call. Returns role only — never data — so the agent can pick a flow."""
    con = db()
    c = find_contact(con, body.caller_number)
    if not c:
        return {"status": "unknown", "role": "unknown"}
    return {"status": "known", "role": c["role"], "first_name": c["name"].split()[0]}


# ---------------------------------------------------------------- student / parent
class StudentIn(BaseModel):
    caller_number: str | None = ""
    reg_no: str
    date_of_birth: str = ""      # student's DOB, any common format
    topic: str = "both"          # marks | attendance | both
    subject: str = ""            # optional course name fragment or code


def _marks_speech(con, reg, subject):
    rows = con.execute("""SELECT m.exam, m.course_code, c.name, m.test, m.cta, m.total, m.absent
                          FROM marks m JOIN courses c ON c.code=m.course_code WHERE m.reg_no=? ORDER BY m.course_code""", (reg,)).fetchall()
    if subject:
        rows = [r for r in rows if subject.lower() in r["name"].lower() or subject.upper() == r["course_code"]]
    if not rows:
        return "I could not find marks for that."
    exam = rows[0]["exam"]
    parts = [f"{r['name']}: {r['total']} out of 100" + (", absent for the test" if r["absent"] else "") for r in rows]
    res = con.execute("SELECT arrears FROM marks_result WHERE reg_no=? AND exam=?", (reg, exam)).fetchone()
    tail = ""
    if res and not subject:
        tail = " They have passed all courses." if res[0] == 0 else f" They are currently below pass mark in {res[0]} course{'s' if res[0] != 1 else ''}."
    return f"{exam} marks. " + ". ".join(parts) + "." + tail


def _att_speech(con, reg, subject):
    s = con.execute("SELECT * FROM attendance_summary WHERE reg_no=? ORDER BY period DESC LIMIT 1", (reg,)).fetchone()
    if not s:
        return "I could not find attendance for that."
    rows = con.execute("""SELECT a.course_code, c.name, a.attended, a.total, a.pct FROM attendance a JOIN courses c ON c.code=a.course_code
                          WHERE a.reg_no=? AND a.period=? ORDER BY a.pct""", (reg, s["period"])).fetchall()
    if subject:
        rows = [r for r in rows if subject.lower() in r["name"].lower() or subject.upper() == r["course_code"]]
        if not rows:
            return "I could not find attendance for that subject."
        return "; ".join(f"{r['name']}: {r['attended']} of {r['total']} hours, {r['pct']:.0f} percent" for r in rows) + "."
    out = f"Overall attendance for {s['period']} is {s['pct']:.0f} percent."
    low = [r for r in rows if r["pct"] < LOW_ATTENDANCE]
    if low:
        out += " Below 75 percent in " + ", ".join(f"{r['name']} at {r['pct']:.0f}" for r in low) + "."
    else:
        out += " All subjects are above 75 percent."
    return out


@app.post("/student", dependencies=[Depends(require_key)])
def student(body: StudentIn):
    con = db()
    c = find_contact(con, body.caller_number)
    reg = re.sub(r"\D", "", body.reg_no)
    # The voice agent must fill every field, so treat filler words as "not given" and unknown topics as "both".
    if body.subject.strip().lower() in ("", "none", "all", "any", "na", "n/a", "null", "nil", "no", "-", "everything"):
        body.subject = ""
    body.topic = body.topic.strip().lower()
    if body.topic not in ("marks", "attendance", "both"):
        body.topic = "both"
    if not c or c["role"] not in ("student", "parent"):
        audit(con, body.caller_number, "unknown" if not c else c["role"], reg, body.topic, "not_authorised")
        return {"status": "not_authorised", "speech": "I can only share academic details with registered students and parents."}
    if locked_out(con, body.caller_number):
        audit(con, body.caller_number, c["role"], reg, body.topic, "locked_out")
        return {"status": "locked", "speech": "I can't verify this right now. Please contact the department office."}
    # a student/parent may only ask about the one reg number linked to their contact record
    ver = con.execute("SELECT dob FROM verification WHERE reg_no=?", (reg,)).fetchone()
    ok = reg == c["reg_no"] and ver is not None and norm_dob(body.date_of_birth) == ver["dob"]
    if not ok:
        audit(con, body.caller_number, c["role"], reg, body.topic, "verification_failed")
        return {"status": "verification_failed",
                "speech": "Sorry, I couldn't verify those details. Please check the register number and date of birth and try again."}
    who = con.execute("SELECT name FROM students WHERE reg_no=?", (reg,)).fetchone()
    name = who["name"] if who else "the student"
    parts = []
    if body.topic in ("marks", "both"):
        parts.append(_marks_speech(con, reg, body.subject))
    if body.topic in ("attendance", "both"):
        parts.append(_att_speech(con, reg, body.subject))
    audit(con, body.caller_number, c["role"], reg, body.topic, "ok")
    return {"status": "ok", "student_name": name, "speech": f"For {name}. " + " ".join(parts)}


# ---------------------------------------------------------------- faculty
class ClassIn(BaseModel):
    caller_number: str | None = ""
    topic: str = "low_attendance"   # low_attendance | marks_summary


@app.post("/class", dependencies=[Depends(require_key)])
def class_summary(body: ClassIn):
    """Faculty only, scoped to the faculty member's own programme/batch/section."""
    con = db()
    c = find_contact(con, body.caller_number)
    if not c or c["role"] != "faculty":
        audit(con, body.caller_number, "unknown" if not c else c["role"], "", body.topic, "not_authorised")
        return {"status": "not_authorised", "speech": "That information is only available to faculty."}
    q = "FROM students s WHERE s.programme=? AND s.batch=? AND s.section=?"
    args = (c["programme"], c["batch"], c["section"])
    if body.topic == "marks_summary":
        r = con.execute(f"""SELECT COUNT(*) n, SUM(mr.arrears=0) passed {q.replace('FROM students s', 'FROM students s JOIN marks_result mr ON mr.reg_no=s.reg_no')}""", args).fetchone()
        speech = f"{r['passed']} of {r['n']} students passed all courses in the latest internal test." if r["n"] else "No marks found for your class."
    else:
        rows = con.execute(f"""SELECT s.name, a.pct FROM students s JOIN attendance_summary a ON a.reg_no=s.reg_no
                               WHERE s.programme=? AND s.batch=? AND s.section=? AND a.pct<? ORDER BY a.pct""",
                           args + (LOW_ATTENDANCE,)).fetchall()
        speech = (f"{len(rows)} students are below 75 percent. " + "; ".join(f"{r['name']} {r['pct']:.0f}" for r in rows[:5])
                  + (f", and {len(rows) - 5} more." if len(rows) > 5 else ".")) if rows else "No students are below 75 percent."
    audit(con, body.caller_number, "faculty", f"{c['programme']}-{c['batch']}-{c['section']}", body.topic, "ok")
    return {"status": "ok", "speech": speech}


# ---------------------------------------------------------------- transfer routing
class RouteIn(BaseModel):
    department: str = "reception"   # reception | admissions | placement


@app.post("/route", dependencies=[Depends(require_key)])
def route(body: RouteIn):
    """Department -> number to forward the call to. Numbers come from .env (RECEPTION_PHONE etc.).
    Unknown/blank department falls back to reception; no number configured at all => 'none'."""
    d = (body.department or "").strip().lower()
    key = "ADMISSIONS_PHONE" if "admi" in d else "PLACEMENT_PHONE" if "place" in d else "RECEPTION_PHONE"
    number = os.environ.get(key, "").strip() or os.environ.get("RECEPTION_PHONE", "").strip()
    dept = {"ADMISSIONS_PHONE": "admissions", "PLACEMENT_PHONE": "placement"}.get(key, "reception")
    return {"status": "ok" if number else "none", "department": dept, "transfer_number": number or "none"}


@app.get("/health")
def health():
    return {"ok": True}
