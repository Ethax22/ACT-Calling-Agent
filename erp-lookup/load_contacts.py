"""Load contacts, DOB verification and faculty from CSV into erp.db (replaces previous contents of those tables).

contacts.csv columns:         phone,role,name,reg_no,programme,batch,section,active
                              role = student | parent. For parent, reg_no is the CHILD's.
                              (Faculty rows here are ignored: faculty now come from faculty.csv.)
verification.csv columns:     reg_no,dob          (dob as DD-MM-YYYY)
faculty.csv columns:          faculty_id,name,phone,active
faculty_classes.csv columns:  faculty_id,programme,batch,section     one row per class they are in charge of;
                              programme/batch/section as in the marks data (e.g. CSE,2023,A)

    python load_contacts.py contacts.csv verification.csv [faculty.csv faculty_classes.csv]

`python load_contacts.py --fake` writes FAKE test contacts (never real numbers) for exercising the API.
"""
import csv
import re
import sqlite3
import sys
from pathlib import Path

from ingest import DB_PATH, SCHEMA


def _rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f)


def load(contacts_csv: Path, verification_csv: Path, faculty_csv: Path | None = None,
         faculty_classes_csv: Path | None = None):
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.execute("DELETE FROM contacts")
    db.execute("DELETE FROM verification")
    for r in _rows(contacts_csv):
        db.execute("INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?)", (
            re.sub(r"\D", "", r["phone"])[-10:], r["role"].strip().lower(), r["name"], r["reg_no"].strip(),
            r["programme"], r["batch"], r["section"], int(r.get("active", "1") or 1)))
    for r in _rows(verification_csv):
        db.execute("INSERT OR REPLACE INTO verification VALUES(?,?)", (r["reg_no"].strip(), re.sub(r"\D", "", r["dob"])))
    if faculty_csv:
        db.execute("DELETE FROM faculty")
        for r in _rows(faculty_csv):
            db.execute("INSERT OR REPLACE INTO faculty VALUES(?,?,?,?)", (
                re.sub(r"[^A-Z0-9]", "", r["faculty_id"].upper()), r["name"].strip(),
                re.sub(r"\D", "", r["phone"])[-10:], int(r.get("active", "1") or 1)))
    if faculty_classes_csv:
        db.execute("DELETE FROM faculty_classes")
        for r in _rows(faculty_classes_csv):
            db.execute("INSERT OR REPLACE INTO faculty_classes VALUES(?,?,?,?)", (
                re.sub(r"[^A-Z0-9]", "", r["faculty_id"].upper()), r["programme"].strip().upper(),
                r["batch"].strip(), r["section"].strip().upper()))
    db.commit()
    count = lambda t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print("contacts:", count("contacts"), "verification:", count("verification"),
          "faculty:", count("faculty"), "faculty_classes:", count("faculty_classes"))


def write_fake(folder: Path):
    folder.mkdir(exist_ok=True)
    (folder / "contacts.csv").write_text(
        "phone,role,name,reg_no,programme,batch,section,active\n"
        "9000000001,student,Abinaya A,312823104004,CSE,2023,A,1\n"
        "9000000002,parent,Parent of Abinaya A,312823104004,CSE,2023,A,1\n"
        "9000000003,student,Aaron Prince R,312825104001,CSE,2025,A,1\n"
        "9000000005,student,Alumni Student,312823104001,CSE,2023,A,0\n", encoding="utf-8")
    (folder / "verification.csv").write_text(
        "reg_no,dob\n312823104004,12-04-2005\n312825104001,01-01-2007\n312823104001,02-02-2005\n", encoding="utf-8")
    # FAC1001 (9000000004) is in charge of two classes, FAC1002 (9000000006) of one.
    (folder / "faculty.csv").write_text(
        "faculty_id,name,phone,active\n"
        "FAC1001,Test Faculty,9000000004,1\n"
        "FAC1002,Second Faculty,9000000006,1\n", encoding="utf-8")
    (folder / "faculty_classes.csv").write_text(
        "faculty_id,programme,batch,section\n"
        "FAC1001,CSE,2023,A\n"
        "FAC1001,ECE,2025,A\n"
        "FAC1002,CSE,2025,A\n", encoding="utf-8")


if __name__ == "__main__":
    base = Path(__file__).parent / "fake_contacts"
    if "--fake" in sys.argv:
        write_fake(base)
        load(base / "contacts.csv", base / "verification.csv", base / "faculty.csv", base / "faculty_classes.csv")
    else:
        paths = [Path(p) for p in sys.argv[1:5]]
        load(*paths)
