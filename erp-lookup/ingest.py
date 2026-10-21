"""Parse the ERP marks / attendance PDFs in raw/ into erp.db (SQLite).

Idempotent: re-running replaces rows for the same (programme, batch, section, exam/period),
so the weekly refresh is just "drop new PDFs in raw/ and run `python ingest.py`".
Never touches the contacts / verification tables (loaded separately by load_contacts.py).
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DB_PATH = Path(os.environ.get("ERP_DB", HERE / "erp.db"))  # same override as api.py
RAW = HERE / "raw"

SCHEMA = """
CREATE TABLE IF NOT EXISTS students(reg_no TEXT PRIMARY KEY, name TEXT, programme TEXT, batch TEXT, section TEXT);
CREATE TABLE IF NOT EXISTS courses(code TEXT PRIMARY KEY, name TEXT, faculty TEXT);
CREATE TABLE IF NOT EXISTS marks(reg_no TEXT, programme TEXT, batch TEXT, section TEXT, semester TEXT, exam TEXT,
  course_code TEXT, test INTEGER, cta INTEGER, total INTEGER, absent INTEGER, PRIMARY KEY(reg_no, exam, course_code));
CREATE TABLE IF NOT EXISTS marks_result(reg_no TEXT, exam TEXT, arrears INTEGER, PRIMARY KEY(reg_no, exam));
CREATE TABLE IF NOT EXISTS attendance(reg_no TEXT, period TEXT, course_code TEXT, attended INTEGER, total INTEGER, pct REAL,
  PRIMARY KEY(reg_no, period, course_code));
CREATE TABLE IF NOT EXISTS attendance_summary(reg_no TEXT, period TEXT, other_attended INTEGER, other_total INTEGER,
  attended INTEGER, total INTEGER, pct REAL, PRIMARY KEY(reg_no, period));
CREATE TABLE IF NOT EXISTS contacts(phone TEXT, role TEXT, name TEXT, reg_no TEXT, programme TEXT, batch TEXT, section TEXT,
  active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS verification(reg_no TEXT PRIMARY KEY, dob TEXT);
CREATE TABLE IF NOT EXISTS faculty(faculty_id TEXT PRIMARY KEY, name TEXT, phone TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS faculty_classes(faculty_id TEXT, programme TEXT, batch TEXT, section TEXT,
  PRIMARY KEY(faculty_id, programme, batch, section));
CREATE TABLE IF NOT EXISTS audit(ts TEXT DEFAULT CURRENT_TIMESTAMP, caller TEXT, role TEXT, reg_no TEXT, topic TEXT, result TEXT);
"""

NUM = r"(?:\d+|AB)"
MARKS_ROW = re.compile(rf"^(\d+) (\d{{12}}) (.+?) ((?:{NUM} ){{14}}{NUM}) (P|\d+)$")
ATT_CELL = r"\d+ / \d+ [\d.]+ "
ATT_ROW = re.compile(rf"^(\d+) (\d{{12}}) (.+?) ((?:{ATT_CELL}){{5}})(\d+) / (\d+) (\d+) / (\d+) ([\d.]+)$")
FACULTY_ROW = re.compile(r"^\d+ ([A-Z0-9]{6,7}) (.+?) ((?:Dr|Mr|Mrs|Ms|Prof)\..+?) \d+$")
CODE = re.compile(r"\b(?:\d\d[A-Z]{2}\d{3}|[A-Z]{2,3}\d{3,4})\b")


def _text(path: Path) -> list[str]:
    import pdfplumber  # here, not at the top: load_contacts.py imports this file and needs no PDF reader

    with pdfplumber.open(path) as pdf:
        return [ln for pg in pdf.pages for ln in (pg.extract_text() or "").split("\n")]


def _prog_short(name: str) -> str:
    name = name.upper()
    for key, short in (("COMPUTER SCIENCE", "CSE"), ("ELECTRONICS AND COMMUNICATION", "ECE")):
        if key in name:
            return short
    return re.sub(r"[^A-Z]", "", name)[:6]


def ingest_marks(db, path: Path, names: dict) -> int:
    lines = _text(path)
    head = next(l for l in lines if l.startswith("BATCH :"))
    m = re.search(r"BATCH : (\d+) PROGRAMME : (.+?)(?: \[SECTION : (\w+)\])? SEMESTER : (\d+)", head)
    batch, programme, section, semester = m.group(1), _prog_short(m.group(2)), m.group(3) or "A", m.group(4)
    exam = next(l for l in lines if "IA TEST" in l or "ASSESSMENT" in l).split(":")[-1].strip()
    codes = next(l for l in lines if re.fullmatch(r"(?:\d\d[A-Z]{2}\d{3} ?)+", l.strip())).split()
    db.execute("DELETE FROM marks WHERE programme=? AND batch=? AND section=? AND exam=?", (programme, batch, section, exam))
    n = 0
    for l in lines:
        r = MARKS_ROW.match(l.strip())
        if not r:
            continue
        reg, name, vals, res = r.group(2), r.group(3), r.group(4).split(), r.group(5)
        db.execute("INSERT OR REPLACE INTO students VALUES(?,?,?,?,?)", (reg, name.title(), programme, batch, section))
        for i, code in enumerate(codes):
            t, c, tot = vals[3 * i : 3 * i + 3]
            db.execute("INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                reg, programme, batch, section, semester, exam, code,
                None if t == "AB" else int(t), int(c), int(tot), int(t == "AB")))
        db.execute("INSERT OR REPLACE INTO marks_result VALUES(?,?,?)", (reg, exam, 0 if res == "P" else int(res)))
        n += 1
    for code in codes:
        db.execute("INSERT OR IGNORE INTO courses(code,name) VALUES(?,?)", (code, names.get(code, code)))
    return n


def ingest_attendance(db, path: Path) -> int:
    lines = _text(path)
    blob = "\n".join(lines)
    pm = re.search(r"PROGRAMME : B\.E-(\w+) - SECTION : (\w+)", blob)
    programme, section = pm.group(1), pm.group(2)
    batch = re.search(r"BATCH : (\d+)", blob).group(1)
    per = re.search(r"FROM (\S+) TO (\S+)", blob)
    period = f"{per.group(1)} to {per.group(2)}"
    codes = CODE.findall(next(l for l in lines if l.startswith("S.No Reg. No.")))
    for l in lines:
        f = FACULTY_ROW.match(l.strip())
        if f:
            db.execute("INSERT INTO courses(code,name,faculty) VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name, faculty=excluded.faculty",
                       (f.group(1), f.group(2).title(), f.group(3)))
    db.execute("""DELETE FROM attendance WHERE period=? AND reg_no IN
                  (SELECT reg_no FROM students WHERE programme=? AND batch=? AND section=?)""", (period, programme, batch, section))
    n = 0
    for l in lines:
        r = ATT_ROW.match(l.strip())
        if not r:
            continue
        reg, name = r.group(2), r.group(3)
        db.execute("INSERT OR REPLACE INTO students VALUES(?,?,?,?,?)", (reg, name.title(), programme, batch, section))
        cells = re.findall(r"(\d+) / (\d+) ([\d.]+)", r.group(4))
        for code, (a, t, p) in zip(codes, cells):
            if int(t) == 0:  # "0 / 0" = subject not taken by this student (elective)
                continue
            db.execute("INSERT OR REPLACE INTO attendance VALUES(?,?,?,?,?,?)", (reg, period, code, int(a), int(t), float(p)))
        db.execute("INSERT OR REPLACE INTO attendance_summary VALUES(?,?,?,?,?,?,?)",
                   (reg, period, int(r.group(5)), int(r.group(6)), int(r.group(7)), int(r.group(8)), float(r.group(9))))
        n += 1
    return n


def main():
    names = json.loads((HERE / "course_names.json").read_text(encoding="utf-8"))
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    for pdf in sorted(RAW.glob("*.pdf")):
        head = "\n".join(_text(pdf)[:12])
        if "STATEMENT OF MARKS" in head:
            print(f"{pdf.name}: marks for {ingest_marks(db, pdf, names)} students")
        elif "ATTENDANCE" in head:
            print(f"{pdf.name}: attendance for {ingest_attendance(db, pdf)} students")
        else:
            print(f"{pdf.name}: unrecognised layout, skipped", file=sys.stderr)
    db.commit()


if __name__ == "__main__":
    main()
