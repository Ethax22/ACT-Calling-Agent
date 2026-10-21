# Testing the inbound phone agent on this machine

## Start

One command, from `C:\ACT-Calling-Agent` in PowerShell (three windows open; wait about 30 seconds):

```powershell
powershell -ExecutionPolicy Bypass -File .\start-local.ps1
```

Then open `http://127.0.0.1:8002/test` in Chrome or Edge. Use headphones. Pick the caller number, press **Call**, allow the microphone.

Check that all three answer: `http://127.0.0.1:8100/health`, `http://127.0.0.1:8000/health`, `http://127.0.0.1:8002/health` (each shows `ok`).

**Stop:** close the three windows. **Restart one service** after a code change: close its window, run the matching line below in a new PowerShell window.

```powershell
# erp-lookup (records, caller identity, transfer numbers)  :8100
cd C:\ACT-Calling-Agent\erp-lookup
C:\ACT-Calling-Agent\phone-agent\venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8100

# rag-backend (the shared brain)  :8000
cd C:\ACT-Voice-Assistant\rag-backend
.\venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

# phone-agent (the call + the test page)  :8002
cd C:\ACT-Calling-Agent\phone-agent
.\venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8002
```

The keys come from `C:\ACT-Calling-Agent\.env` (and `voice-pipeline\.env` for anything missing). If a port says "address already in use", an older copy is still running: close its window.

## Caller numbers

| Caller number | Who the agent thinks you are | Use it for |
|---|---|---|
| 9000000001 | student (Abinaya A) | attendance (she has no marks in the test data). Register number 312823104004, date of birth 12-04-2005 |
| 9000000002 | parent of Abinaya | same records as the student |
| 9000000003 | student (Aaron Prince R) | marks and attendance. Register number 312825104001, date of birth 01-01-2007 |
| 9000000004 | faculty, faculty ID **FAC1001** | in charge of two classes: CSE 2023 A (attendance only in the test data) and ECE 2025 A (marks only). The bot lists both and asks which |
| 9000000006 | faculty, faculty ID **FAC1002** | in charge of one class, CSE 2025 A (marks only): answered without asking which class |
| anything else, e.g. 9111111111 | unknown caller | general questions, refusal of records, transfer, leads |

The faculty rows come from `python load_contacts.py --fake` (in `erp-lookup`); run it again if 9000000004 is treated as an unknown caller. Three wrong dates of birth (or faculty IDs) in an hour lock that number (it then says it cannot verify right now). To unlock the fake test numbers: `python -I -X utf8 -c "import sqlite3; c=sqlite3.connect(r'C:\ACT-Calling-Agent\erp-lookup\erp.db'); c.execute(\"delete from audit where caller like '90000000%' and result in ('verification_failed','locked_out')\"); c.commit()"`

## What to try, and what you should see

**Opening.** The greeting plays at once: "Welcome to Agni College of Technology, this is Agni AI. This call may be recorded..." You should hear a very quiet office murmur under the whole call (synthetic; switch it off with `PHONE_BACKGROUND=off` in `.env`).

**Questions** (any caller): courses, fees, hostel, placements, admissions, location. Two or three short sentences, no "bro" or "anna", same facts as the website bot.

**Time.** "Is the college office open now?" It answers from the current India time: office hours are 8 AM to 5 PM, Monday to Saturday.

**Languages.** Ask in Tamil: spoken Tamil back. Say "hostel iruka?": Tanglish back. Switch to English: it follows.

**Interrupting.** Talk over a long answer; it should stop within about a second. Say only "okay" while it talks; it keeps going. (Final tuning waits for the real phone.)

**Records** (9000000003): "I want to know my marks" -> register number -> date of birth -> it reads the marks. Wrong date of birth: "could not verify"; three wrong tries: "cannot verify right now". An unknown number asking for marks is refused and offered the reception. Marks are announced as "Internal Assessment Test 1".

**Faculty** (9000000004): "Which students in my class are below 75 percent attendance?" -> it asks for your faculty ID -> say "FAC 1001" -> it reads it back ("F A C 1 0 0 1, is that right?") -> "yes" -> it lists your two classes -> "the CSE one" -> the names below 75%. Ask "latest internal assessment result for my ECE class" for the marks summary. 9000000006 with FAC1002 gets its one class straight away. A wrong ID: "couldn't verify"; three wrong tries lock the number like a wrong date of birth.

**Leads.** As an unknown caller say you want to join computer science, give a name and a ten-digit number. It reads the number back digit by digit; say "yes". It thanks you and a row appears in the leads Sheet with source `phone`. (On the website chat the same flow saves with source `chat`.) Delete test rows afterwards.

**Transfer.** "Let me talk to someone in admissions." One spoken line, the page log shows a `transfer` message with the number, the call ends. Nothing is dialled on the browser line (real transfer is tested on the real phone).

**Ending.** "Okay thank you, that's all." A short goodbye, then the call closes by itself.

**Quiet caller.** Say nothing after the greeting. About 10 seconds later: "are you still there?"; 10 seconds after that: a goodbye and the call closes.

## After every call

- `C:\ACT-Voice-Assistant\voice-pipeline\logs\<date>\phone-test-<id>\` holds `transcript.json`, `audio.wav` and, a few seconds after the call ends, `call_record.json`. (The folder date is UTC.)
- A new row appears in the `Calls` tab of the calls Sheet. Delete test rows afterwards.
- The phone-agent window prints `[latency]` and `[turn]` lines for each answer.

Report anything that sounds wrong with what you said and what it replied; the transcript file has both.

## Automated checks (optional)

```powershell
cd C:\ACT-Calling-Agent\phone-agent;           .\venv\Scripts\python.exe -m pytest tests -p no:cacheprovider
cd C:\ACT-Voice-Assistant\rag-backend;         .\venv\Scripts\python.exe -m pytest tests -p no:cacheprovider
cd C:\ACT-Voice-Assistant\voice-pipeline;      .\venv\Scripts\python.exe -m pytest tests -p no:cacheprovider
```

Live scripts (need the three services running; they call the real vendors and some write real rows):
`phone-agent\live\records_dialogs.py` (typed callers against the real model; `LIVE_LEAD_TEST=1` also saves a lead),
`phone-agent\live\voice_calls.py <end-call|transfer|quiet|cap|records>` (spoken callers; start the phone agent with `PHONE_BACKGROUND=off` for these).
