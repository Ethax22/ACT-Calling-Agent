"""Run: python load_contacts.py --fake && python test_api.py   (uses FAKE contacts only)

It writes test rows (a second exam and attendance period, audit rows), so point it at a COPY of
the database: set ERP_DB=<copy of erp.db> for both commands."""
import os
import sqlite3
from datetime import date

os.environ["ERP_API_KEY"] = "testkey"
os.environ.pop("ERP_TEST_CALLER", None)
from fastapi.testclient import TestClient

import api

H = {"Authorization": "Bearer testkey"}
c = TestClient(api.app)
db = sqlite3.connect(api.DB_PATH)
db.execute("DELETE FROM audit"); db.commit()


def post(path, **body):
    r = c.post(path, json=body, headers=H)
    return r.status_code, r.json()


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    assert cond, label


check("no auth key rejected", c.post("/identify", json={}).status_code == 401)
check("identify student", post("/identify", caller_number="+91 90000 00001")[1]["role"] == "student")
check("identify unknown number", post("/identify", caller_number="9111111111")[1]["status"] == "unknown")
check("identify missing caller id", post("/identify", caller_number="")[1]["status"] == "unknown")
check("identify inactive contact => unknown", post("/identify", caller_number="9000000005")[1]["status"] == "unknown")

s, r = post("/student", caller_number="9000000001", reg_no="312823104004", date_of_birth="12 April 2005", topic="both")
check("unparseable DOB format fails closed", r["status"] == "verification_failed")
s, r = post("/student", caller_number="9000000001", reg_no="312823104004", date_of_birth="12-04-2005", topic="attendance")
check("student own attendance ok", r["status"] == "ok"); print("   ", r["speech"])
s, r = post("/student", caller_number="9000000002", reg_no="312823104004", date_of_birth="12/4/2005", topic="attendance", subject="Management")
check("parent child subject attendance ok", r["status"] == "ok"); print("   ", r["speech"])
s, r = post("/student", caller_number="9000000003", reg_no="312825104001", date_of_birth="2007-01-01", topic="marks")
check("student marks ok (YYYY-MM-DD dob)", r["status"] == "ok"); print("   ", r["speech"])
s, r = post("/student", caller_number="9000000003", reg_no="312823104004", date_of_birth="12-04-2005")
check("student cannot read another student", r["status"] == "verification_failed")
s, r = post("/student", caller_number="9111111111", reg_no="312823104004", date_of_birth="12-04-2005")
check("unknown caller gets nothing", r["status"] == "not_authorised" and "Abinaya" not in str(r))
s, r = post("/student", caller_number="", reg_no="312823104004", date_of_birth="12-04-2005")
check("missing caller id gets nothing", r["status"] == "not_authorised")
s, r = post("/student", caller_number="9000000004", reg_no="312823104004", date_of_birth="12-04-2005")
check("faculty cannot use /student", r["status"] == "not_authorised")
s, r = post("/student", caller_number="9000000005", reg_no="312823104001", date_of_birth="02-02-2005")
check("inactive (left college) contact refused", r["status"] == "not_authorised")

for _ in range(3):
    post("/student", caller_number="9000000003", reg_no="312825104001", date_of_birth="01-01-1999")
s, r = post("/student", caller_number="9000000003", reg_no="312825104001", date_of_birth="01-01-2007")
check("lockout after 3 failures, even with right DOB", r["status"] == "locked")

check("exam name spoken in full", api.spoken_exam("IA TEST-1") == "Internal Assessment Test 1"
      and api.spoken_exam("IAT-2") == "Internal Assessment Test 2" and api.spoken_exam("MODEL EXAM") == "MODEL EXAM")

# ---- faculty: faculty ID is the second factor, classes come from faculty_classes
check("identify faculty from the faculty table", post("/identify", caller_number="9000000004")[1]["role"] == "faculty")
s, r = post("/class", caller_number="9000000004", topic="low_attendance")
check("faculty without an ID is asked for it, not failed", r["status"] == "need_details")
s, r = post("/class", caller_number="9000000004", faculty_id="FAC1002", topic="low_attendance")
check("another faculty member's ID fails", r["status"] == "verification_failed")
s, r = post("/class", caller_number="9000000004", faculty_id="fac 1001", topic="low_attendance")
check("two classes and none named: list them", r["status"] == "choose_class" and len(r["classes"]) == 2); print("   ", r["speech"])
s, r = post("/class", caller_number="9000000004", faculty_id="FAC1001", topic="marks_summary", class_name="ECE")
check("class chosen by programme", r["status"] == "ok" and r["class"].startswith("ECE")); print("   ", r["speech"])
s, r = post("/class", caller_number="9000000004", faculty_id="FAC1001", topic="low_attendance", class_name="the 2023 batch")
check("class chosen by batch", r["status"] == "ok" and "CSE" in r["class"]); print("   ", r["speech"])
s, r = post("/class", caller_number="9000000004", faculty_id="FAC1001", topic="", class_name="CSE")
check("no topic yet: ask which", r["status"] == "choose_topic")
s, r = post("/class", caller_number="9000000004", faculty_id="FAC1001", topic="low_attendance", class_name="MECH 2020")
check("a class they don't have: list theirs", r["status"] == "choose_class")
s, r = post("/class", caller_number="9000000006", faculty_id="FAC1002", topic="marks_summary")
check("one class: used without asking", r["status"] == "ok" and "CSE" in r["class"]); print("   ", r["speech"])
s, r = post("/class", caller_number="9000000001", faculty_id="FAC1001", topic="low_attendance")
check("student cannot use /class", r["status"] == "not_authorised")
for _ in range(3):
    post("/class", caller_number="9000000006", faculty_id="WRONG1", topic="low_attendance")
s, r = post("/class", caller_number="9000000006", faculty_id="FAC1002", topic="low_attendance")
check("faculty lockout after 3 wrong IDs, even with the right one", r["status"] == "locked")

# ---- matching and year of study (October 2026: batch 2023 is fourth year, 2025 second year)
oct26 = date(2026, 10, 21)
classes = [{"programme": "CSE", "batch": "2023", "section": "A"}, {"programme": "ECE", "batch": "2025", "section": "A"}]
check("year of study", api.year_of_study("2023", oct26) == 4 and api.year_of_study("2025", oct26) == 2)
check("match by year words", api.match_class("fourth year", classes, oct26)["programme"] == "CSE")
check("match by 'second year ECE'", api.match_class("second year ECE", classes, oct26)["programme"] == "ECE")
check("ambiguous section only", api.match_class("section A", classes, oct26) is None)
check("label", api.class_label(classes[0], oct26) == "CSE fourth year, 2023 batch, section A")

# ---- only the newest exam and attendance period are read
db.execute("""INSERT OR REPLACE INTO marks SELECT reg_no, programme, batch, section, semester, 'IA TEST-2', course_code,
              test, cta, 99, absent FROM marks WHERE exam='IA TEST-1'""")
db.execute("INSERT OR REPLACE INTO marks_result SELECT reg_no, 'IA TEST-2', 0 FROM marks_result WHERE exam='IA TEST-1'")
db.execute("""INSERT OR REPLACE INTO attendance_summary SELECT reg_no, '01-11-2026 to 30-11-2026', other_attended, other_total,
              attended, total, 100.0 FROM attendance_summary""")
db.commit()
db.execute("DELETE FROM audit"); db.commit()
s, r = post("/student", caller_number="9000000003", reg_no="312825104001", date_of_birth="01-01-2007", topic="marks")
check("student marks: newest exam only", "Internal Assessment Test 2 marks" in r["speech"] and "Test 1" not in r["speech"]
      and r["speech"].count("99 out of 100") == r["speech"].count("out of 100")); print("   ", r["speech"])
s, r = post("/class", caller_number="9000000006", faculty_id="FAC1002", topic="marks_summary")
n = db.execute("SELECT COUNT(*) FROM students WHERE programme='CSE' AND batch='2025' AND section='A'").fetchone()[0]
check("class marks: newest exam, nobody counted twice", "Internal Assessment Test 2" in r["speech"] and f"of {n} students" in r["speech"]); print("   ", r["speech"])
# In the loaded data the 2023 batches have attendance only and the 2025 batches marks only.
s, r = post("/class", caller_number="9000000004", faculty_id="FAC1001", topic="low_attendance", class_name="CSE")
check("class attendance: newest period only", "no students are below 75" in r["speech"]); print("   ", r["speech"])
check("period ranked by end date, not text", api.latest_period(["15-09-2026 to 30-09-2026", "01-12-2026 to 31-12-2026"]).startswith("01-12"))
leaked = db.execute("SELECT COUNT(*) FROM audit WHERE result NOT IN ('ok','not_authorised','verification_failed','locked_out')").fetchone()[0]
print("audit rows:", db.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
print("ALL PASSED")
