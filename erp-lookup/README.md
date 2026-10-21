# ACT ERP lookup (for the Indus calling agent)

Private service. `erp.db`, `raw/` and any real contacts CSV hold student data — never commit or share them.

## Weekly refresh
1. Drop the new marks/attendance PDFs into `raw/`.
2. `python ingest.py` (replaces rows for the same programme/batch/section/exam/period).

Only the newest exam (highest semester, then highest test number: IA TEST-2 after IA TEST-1) and the newest
attendance period (by its end date) are read; older ones stay in the database. Exams are spoken as
"Internal Assessment Test 1".

## Contacts and faculty (later phase)
`python load_contacts.py contacts.csv verification.csv faculty.csv faculty_classes.csv` — formats in `load_contacts.py`.
Students and parents come from `contacts.csv` (+ date of birth in `verification.csv`). Faculty come from `faculty.csv`
(faculty ID, name, phone) and `faculty_classes.csv` (one row per class they are in charge of).
`python load_contacts.py --fake` loads FAKE test contacts (9000000001-6, faculty FAC1001 and FAC1002) — remove before
real use by loading the real files.

**Faculty lookup (`/class`):** the caller's number must belong to an active faculty member AND the faculty ID they give
must be that member's (second factor, like a student's date of birth; three wrong tries lock the number for an hour).
With one class it answers for that class; with several and no `class_name`, it returns `choose_class` with the list,
and `class_name` in the caller's words ("CSE 2023", "fourth year ECE", "section A") picks one.

## Run
```
set ERP_API_KEY=<long random secret>
uvicorn api:app --host 0.0.0.0 --port 8100
set ERP_DB=<a copy of erp.db>     # the tests write rows: never run them on the real database
python load_contacts.py --fake && python test_api.py
```
For Indus to reach it during testing: `ngrok http 8100`. For real use, host with a stable HTTPS URL.

## Indus API Tools (Tools tab -> custom API tool, all POST, header `Authorization: Bearer <ERP_API_KEY>`)
| Tool | URL | Body fields |
|---|---|---|
| `identify_caller` | `/identify` | `caller_number` (from the call variable) |
| `get_student_report` | `/student` | `caller_number`, `reg_no`, `date_of_birth` as **DD-MM-YYYY**, `topic` = marks/attendance/both, `subject` (optional) |
| `get_class_summary` | `/class` | `caller_number`, `faculty_id`, `topic` = low_attendance/marks_summary, `class_name` (optional) |

**Changed 2026-10-21:** `/class` now needs `faculty_id`. The Indus agent's `get_class_summary` tool does not send it,
so faculty lookups through Indus get `need_details` until that tool gets the field (students and parents unaffected).

Agent instructions to add: call identify_caller at the start; only offer marks/attendance if role is
student/parent/faculty; ask for register number and date of birth first and convert the date to DD-MM-YYYY;
read `speech` back as-is, never add or guess numbers; on any status other than `ok` say the `speech` text and
offer a human transfer; never read register numbers or marks to anyone not verified.
