"""Load contacts + DOB verification from CSV into erp.db (replaces previous contents of those two tables).

contacts.csv columns:      phone,role,name,reg_no,programme,batch,section,active
                           role = student | parent | faculty. For parent, reg_no is the CHILD's.
                           Faculty rows leave reg_no blank and set programme/batch/section they teach.
verification.csv columns:  reg_no,dob          (dob as DD-MM-YYYY)

`python load_contacts.py --fake` writes FAKE test contacts (never real numbers) for exercising the API.
"""
import csv
import re
import sqlite3
import sys
from pathlib import Path

from ingest import DB_PATH, SCHEMA


def load(contacts_csv: Path, verification_csv: Path):
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.execute("DELETE FROM contacts")
    db.execute("DELETE FROM verification")
    with contacts_csv.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            db.execute("INSERT INTO contacts VALUES(?,?,?,?,?,?,?,?)", (
                re.sub(r"\D", "", r["phone"])[-10:], r["role"].strip().lower(), r["name"], r["reg_no"].strip(),
                r["programme"], r["batch"], r["section"], int(r.get("active", "1") or 1)))
    with verification_csv.open(newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            db.execute("INSERT OR REPLACE INTO verification VALUES(?,?)", (r["reg_no"].strip(), re.sub(r"\D", "", r["dob"])))
    db.commit()
    print("contacts:", db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0],
          "verification:", db.execute("SELECT COUNT(*) FROM verification").fetchone()[0])


def write_fake(folder: Path):
    folder.mkdir(exist_ok=True)
    (folder / "contacts.csv").write_text(
        "phone,role,name,reg_no,programme,batch,section,active\n"
        "9000000001,student,Abinaya A,312823104004,CSE,2023,A,1\n"
        "9000000002,parent,Parent of Abinaya A,312823104004,CSE,2023,A,1\n"
        "9000000003,student,Aaron Prince R,312825104001,CSE,2025,A,1\n"
        "9000000004,faculty,Test Faculty,,CSE,2023,A,1\n"
        "9000000005,student,Alumni Student,312823104001,CSE,2023,A,0\n", encoding="utf-8")
    (folder / "verification.csv").write_text(
        "reg_no,dob\n312823104004,12-04-2005\n312825104001,01-01-2007\n312823104001,02-02-2005\n", encoding="utf-8")


if __name__ == "__main__":
    base = Path(__file__).parent / "fake_contacts"
    if "--fake" in sys.argv:
        write_fake(base)
        load(base / "contacts.csv", base / "verification.csv")
    else:
        load(Path(sys.argv[1]), Path(sys.argv[2]))
