"""ERP lookup API for the Indus calling agent (registered there as API Tools).

Rules enforced HERE, not in the prompt — the model only ever sees what this returns:
  * caller's phone must match an ACTIVE contact (missing/garbled caller ID => treated as unknown, no data)
  * student  -> only own record;  parent -> only linked child;  faculty -> only the classes they are in charge of
  * student/parent must also pass a second factor (date of birth), faculty their faculty ID — caller ID alone
    is just a hint
  * only the newest exam and attendance period are read (older ones stay in the database)
  * 3 failed verifications per number per hour => locked out for that hour
  * every lookup is written to the audit table (no marks/attendance values logged)

Responses carry a short `speech` string written for reading aloud, plus `status` so the agent can branch.
"""
import os
import re
import sqlite3
from datetime import date
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

# Faculty and the classes each one is in charge of (loaded by load_contacts.py). Created here too,
# so an older erp.db without them still starts.
FACULTY_SCHEMA = """
CREATE TABLE IF NOT EXISTS faculty(faculty_id TEXT PRIMARY KEY, name TEXT, phone TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS faculty_classes(faculty_id TEXT, programme TEXT, batch TEXT, section TEXT,
  PRIMARY KEY(faculty_id, programme, batch, section));
"""


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


@app.on_event("startup")
def _ensure_faculty_tables():
    con = db()
    con.executescript(FACULTY_SCHEMA)
    con.close()


# ---------------------------------------------------------------- newest exam / period, spoken names
def spoken_exam(exam: str) -> str:
    """'IA TEST-1' (as the marks PDFs write it) -> 'Internal Assessment Test 1'. The voice
    blurred 'IA' (Prakash, 2026-10-21); other exam names are left as written."""
    m = re.fullmatch(r"\s*I\.?\s*A\.?\s*T(?:EST)?\s*[-–]?\s*(\d+)\s*", exam or "", re.I)
    return f"Internal Assessment Test {m.group(1)}" if m else (exam or "").strip()


def _exam_rank(row) -> tuple:
    """Newest exam = highest semester, then the highest test number in its name (IA TEST-2 after
    IA TEST-1). The marks PDFs carry no date, so an exam without a number (a model exam) ranks
    below the numbered tests of its semester."""
    n = re.search(r"(\d+)\s*$", row["exam"] or "")
    try:
        sem = int(row["semester"] or 0)
    except ValueError:
        sem = 0
    return (sem, int(n.group(1)) if n else 0, row["exam"] or "")


def latest_exam(con, where: str, args: tuple) -> str | None:
    rows = con.execute(f"SELECT DISTINCT exam, semester FROM marks WHERE {where}", args).fetchall()
    return max(rows, key=_exam_rank)["exam"] if rows else None


def _period_rank(period: str) -> tuple:
    """'01-09-2026 to 30-10-2026' ranks by its end date. Sorting the text would put
    '01-12-...' before '15-09-...' (day first)."""
    m = re.search(r"(\d{1,2})-(\d{1,2})-(\d{4})\s*$", period or "")
    return (int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else (0, 0, 0, period or "")


def latest_period(periods) -> str | None:
    periods = [p for p in periods if p]
    return max(periods, key=_period_rank) if periods else None


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


def find_contact(con, caller) -> dict | None:
    """The caller's contact record: a student or parent from `contacts`, or a faculty member from
    `faculty` (faculty rows left in `contacts` from the old one-class layout are ignored)."""
    phone = norm_phone(caller)
    if not phone and os.environ.get("ERP_TEST_CALLER"):
        # TEST ONLY: browser "Test agent" calls carry no caller number. Remove ERP_TEST_CALLER from .env before go-live,
        # otherwise every call without caller ID would be treated as this contact.
        phone = norm_phone(os.environ["ERP_TEST_CALLER"])
    if not phone:
        return None
    row = con.execute("""SELECT * FROM contacts WHERE active=1 AND role IN ('student','parent')
                         AND substr(replace(phone,' ',''), -10)=? LIMIT 1""", (phone,)).fetchone()
    if row:
        return dict(row)
    f = con.execute("SELECT * FROM faculty WHERE active=1 AND substr(replace(phone,' ',''), -10)=? LIMIT 1",
                    (phone,)).fetchone()
    if f:
        return {"role": "faculty", "name": f["name"], "phone": phone, "reg_no": None}
    return None


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
    exam = latest_exam(con, "reg_no=?", (reg,))
    if not exam:
        return "I could not find marks for that."
    rows = con.execute("""SELECT m.exam, m.course_code, c.name, m.test, m.cta, m.total, m.absent
                          FROM marks m JOIN courses c ON c.code=m.course_code WHERE m.reg_no=? AND m.exam=?
                          ORDER BY m.course_code""", (reg, exam)).fetchall()
    if subject:
        rows = [r for r in rows if subject.lower() in r["name"].lower() or subject.upper() == r["course_code"]]
    if not rows:
        return "I could not find marks for that."
    parts = [f"{r['name']}: {r['total']} out of 100" + (", absent for the test" if r["absent"] else "") for r in rows]
    res = con.execute("SELECT arrears FROM marks_result WHERE reg_no=? AND exam=?", (reg, exam)).fetchone()
    tail = ""
    if res and not subject:
        tail = " They have passed all courses." if res[0] == 0 else f" They are currently below pass mark in {res[0]} course{'s' if res[0] != 1 else ''}."
    return f"{spoken_exam(exam)} marks. " + ". ".join(parts) + "." + tail


def _att_speech(con, reg, subject):
    period = latest_period(r[0] for r in con.execute("SELECT DISTINCT period FROM attendance_summary WHERE reg_no=?", (reg,)))
    s = con.execute("SELECT * FROM attendance_summary WHERE reg_no=? AND period=?", (reg, period)).fetchone() if period else None
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
_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
_YEAR_WORDS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
               "fourth": 4, "4th": 4, "final": 4, "fifth": 5, "5th": 5}
# Programme names a caller may say instead of the short code stored in `students`.
_PROGRAMME_WORDS = {"COMPUTER": "CSE", "CS": "CSE", "ELECTRONICS": "ECE", "ELECTRICAL": "EEE",
                    "MECHANICAL": "MECH", "INFORMATION": "IT", "CIVIL": "CIVIL"}


def norm_faculty_id(s: str) -> str:
    """'fac-1001', 'F A C 1001', 'FAC 1 0 0 1' -> 'FAC1001'."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def year_of_study(batch: str, today: date | None = None) -> int | None:
    """Batch 2023 is in its fourth year from June 2026 (the academic year starts in June)."""
    today = today or date.today()
    try:
        y = today.year - int(batch) + (1 if today.month >= 6 else 0)
    except ValueError:
        return None
    return y if 1 <= y <= 5 else None


def class_label(cls: dict, today: date | None = None) -> str:
    year = year_of_study(cls["batch"], today)
    year_part = f" {_ORDINALS[year]} year," if year else ""
    return f"{cls['programme']}{year_part} {cls['batch']} batch, section {cls['section']}"


def match_class(text: str, classes: list[dict], today: date | None = None) -> dict | None:
    """The one class among `classes` that the caller's words point to ('CSE 2023', 'fourth year
    ECE', 'section A'), or None when nothing or more than one matches."""
    words = re.findall(r"[A-Za-z0-9]+", text or "")
    upper = {w.upper() for w in words}
    cands, constrained = list(classes), False
    programmes = {_PROGRAMME_WORDS.get(w, w) for w in upper} & {c["programme"].upper() for c in classes}
    if programmes:
        cands, constrained = [c for c in cands if c["programme"].upper() in programmes], True
    batches = {w for w in upper if re.fullmatch(r"(19|20)\d\d", w)}
    if batches:
        cands, constrained = [c for c in cands if c["batch"] in batches], True
    years = {_YEAR_WORDS[w.lower()] for w in words if w.lower() in _YEAR_WORDS}
    if years:
        cands, constrained = [c for c in cands if year_of_study(c["batch"], today) in years], True
    sec = re.search(r"\bsection\s*([A-Za-z])\b|\b([A-Za-z])\s*section\b", text or "", re.I)
    letters = {(sec.group(1) or sec.group(2)).upper()} if sec else {w for w in words if len(w) == 1 and w.isupper()}
    letters &= {c["section"].upper() for c in classes}
    if letters:
        cands, constrained = [c for c in cands if c["section"].upper() in letters], True
    return cands[0] if constrained and len(cands) == 1 else None


class ClassIn(BaseModel):
    caller_number: str | None = ""
    faculty_id: str = ""
    topic: str = ""            # low_attendance | marks_summary
    class_name: str = ""       # which of their classes, in the caller's words ("CSE 2023", "fourth year ECE")


@app.post("/class", dependencies=[Depends(require_key)])
def class_summary(body: ClassIn):
    """Faculty only. The faculty ID must belong to the calling number (second factor, like a
    student's date of birth), and only the classes that faculty member is in charge of are read."""
    con = db()
    c = find_contact(con, body.caller_number)
    topic = (body.topic or "").strip().lower()
    if not c or c["role"] != "faculty":
        audit(con, body.caller_number, "unknown" if not c else c["role"], "", topic, "not_authorised")
        return {"status": "not_authorised", "speech": "That information is only available to faculty."}
    if locked_out(con, body.caller_number):
        audit(con, body.caller_number, "faculty", "", topic, "locked_out")
        return {"status": "locked", "speech": "I can't verify this right now. Please contact the department office."}
    fid = norm_faculty_id(body.faculty_id)
    if not fid:  # nothing to check yet: not a failed try
        return {"status": "need_details", "speech": "Ask the caller for their faculty ID. Do not guess it."}
    f = con.execute("""SELECT faculty_id FROM faculty WHERE active=1 AND faculty_id=?
                       AND substr(replace(phone,' ',''), -10)=?""", (fid, c["phone"])).fetchone()
    if not f:
        audit(con, body.caller_number, "faculty", "", topic, "verification_failed")
        return {"status": "verification_failed",
                "speech": "Sorry, I couldn't verify that faculty ID. Please check it and try again."}
    classes = [dict(r) for r in con.execute(
        "SELECT programme, batch, section FROM faculty_classes WHERE faculty_id=? ORDER BY programme, batch, section", (fid,))]
    if not classes:
        audit(con, body.caller_number, "faculty", fid, topic, "no_classes")
        return {"status": "no_classes", "speech": "I don't have any classes listed for you. I can connect you to the reception."}
    chosen = classes[0] if len(classes) == 1 else match_class(body.class_name, classes)
    if not chosen:
        labels = [class_label(k) for k in classes]
        audit(con, body.caller_number, "faculty", fid, topic, "choose_class")
        return {"status": "choose_class", "classes": labels,
                "speech": f"You are in charge of {len(labels)} classes: " + "; ".join(labels) + ". Which class would you like?"}
    label = class_label(chosen)
    if topic not in ("low_attendance", "marks_summary"):
        return {"status": "choose_topic", "class": label,
                "speech": f"For {label}: would you like the students below 75 percent attendance, or the latest internal assessment result?"}
    args = (chosen["programme"], chosen["batch"], chosen["section"])
    if topic == "marks_summary":
        exam = latest_exam(con, "programme=? AND batch=? AND section=?", args)
        r = con.execute("""SELECT COUNT(*) n, SUM(mr.arrears=0) passed FROM students s JOIN marks_result mr ON mr.reg_no=s.reg_no
                           WHERE s.programme=? AND s.batch=? AND s.section=? AND mr.exam=?""", args + (exam,)).fetchone() if exam else None
        # "No marks found" was retold by the model as good news; say plainly that the data is missing.
        speech = (f"{label}, {spoken_exam(exam)}: {r['passed']} of {r['n']} students passed all courses."
                  if r and r["n"] else f"Marks for {label} have not been uploaded yet, so I can't tell you the result.")
    else:
        period = latest_period(p[0] for p in con.execute(
            """SELECT DISTINCT a.period FROM attendance_summary a JOIN students s ON s.reg_no=a.reg_no
               WHERE s.programme=? AND s.batch=? AND s.section=?""", args))
        rows = con.execute("""SELECT s.name, a.pct FROM students s JOIN attendance_summary a ON a.reg_no=s.reg_no
                              WHERE s.programme=? AND s.batch=? AND s.section=? AND a.period=? AND a.pct<? ORDER BY a.pct""",
                           args + (period, LOW_ATTENDANCE)).fetchall() if period else []
        if not period:  # "No attendance found" became "no attendance issues" in a live test (2026-10-21)
            speech = f"Attendance for {label} has not been uploaded yet, so I can't tell you who is below 75 percent."
        elif rows:
            # Every name up to ten. With "5 names and 3 more" the model retold it in Tamil as
            # "6 named, 2 more" (2026-10-21); nothing left to count is nothing to miscount.
            shown = rows[:10]
            speech = (f"{label}: {len(rows)} student{'s are' if len(rows) != 1 else ' is'} below 75 percent attendance: "
                      + "; ".join(f"{r['name']}, {r['pct']:.0f} percent" for r in shown)
                      + (f". The other {len(rows) - len(shown)} are not read out on the phone." if len(rows) > len(shown) else "."))
        else:
            speech = f"{label}: no students are below 75 percent attendance."
    audit(con, body.caller_number, "faculty", f"{fid}:{'-'.join(args)}", topic, "ok")
    return {"status": "ok", "class": label, "speech": speech}


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
