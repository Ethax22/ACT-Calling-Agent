# ACT ERP lookup (for the Indus calling agent)

Private service. `erp.db`, `raw/` and any real contacts CSV hold student data — never commit or share them.

## Weekly refresh
1. Drop the new marks/attendance PDFs into `raw/`.
2. `python ingest.py` (replaces rows for the same programme/batch/section/exam/period).

## Contacts (later phase)
`python load_contacts.py contacts.csv verification.csv` — formats in `load_contacts.py`.
`python load_contacts.py --fake` loads FAKE test contacts (9000000001-5) — remove before real use by loading the real files.

## Run
```
set ERP_API_KEY=<long random secret>
uvicorn api:app --host 0.0.0.0 --port 8100
python test_api.py        # needs fake contacts loaded
```
For Indus to reach it during testing: `ngrok http 8100`. For real use, host with a stable HTTPS URL.

## Indus API Tools (Tools tab -> custom API tool, all POST, header `Authorization: Bearer <ERP_API_KEY>`)
| Tool | URL | Body fields |
|---|---|---|
| `identify_caller` | `/identify` | `caller_number` (from the call variable) |
| `get_student_report` | `/student` | `caller_number`, `reg_no`, `date_of_birth` as **DD-MM-YYYY**, `topic` = marks/attendance/both, `subject` (optional) |
| `get_class_summary` | `/class` | `caller_number`, `topic` = low_attendance/marks_summary |

Agent instructions to add: call identify_caller at the start; only offer marks/attendance if role is
student/parent/faculty; ask for register number and date of birth first and convert the date to DD-MM-YYYY;
read `speech` back as-is, never add or guess numbers; on any status other than `ok` say the `speech` text and
offer a human transfer; never read register numbers or marks to anyone not verified.
