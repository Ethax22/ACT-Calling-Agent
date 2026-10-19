"""Run: python load_contacts.py --fake && python test_api.py   (uses FAKE contacts only)"""
import os
import sqlite3

os.environ["ERP_API_KEY"] = "testkey"
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

s, r = post("/class", caller_number="9000000004", topic="low_attendance")
check("faculty class low-attendance ok", r["status"] == "ok"); print("   ", r["speech"])
s, r = post("/class", caller_number="9000000001", topic="low_attendance")
check("student cannot use /class", r["status"] == "not_authorised")
leaked = db.execute("SELECT COUNT(*) FROM audit WHERE result NOT IN ('ok','not_authorised','verification_failed','locked_out')").fetchone()[0]
print("audit rows:", db.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
print("ALL PASSED")
