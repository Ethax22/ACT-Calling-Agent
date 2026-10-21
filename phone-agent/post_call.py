"""After a call: turn the saved transcript into the structured record Indus produced
(admission_interest, call_disposition, ...), keep it beside the transcript and add a
row to the "Calls" tab of the Google Sheet.

Same pattern as rag-backend/sentiment_report.py: Sarvam 105B reads the transcript. The
labels are model judgements and have not been checked against hand-labelled calls.
"""
import asyncio
import json
import os
import re
from datetime import datetime, timedelta, timezone

from loguru import logger

FIELDS = {
    "admission_interest": ("yes", "no", "unclear"),
    "call_disposition": ("answered", "lead_captured", "transferred", "unanswered_question", "abandoned"),
    "call_summary": None,
    "caller_intent": ("admissions", "courses", "fees", "placements", "campus_info", "complaint", "human_request", "other"),
    "caller_type": ("prospective_student", "parent", "current_student", "faculty", "alumni", "other", "unknown"),
    "escalation_reason": None,
    "language_used": ("english", "tamil", "mixed"),
    "lead_course": None,
    "lead_name": None,
    "lead_phone": None,
}

PROMPT = f"""You read the transcript of a phone call to Agni College of Technology and fill in a record.
Answer with one JSON object only, with exactly these keys:
- admission_interest: one of yes, no, unclear. Did the caller show clear interest in joining the college?
- call_disposition: one of answered, lead_captured, transferred, unanswered_question, abandoned. The single main outcome.
- call_summary: one or two sentences.
- caller_intent: one of admissions, courses, fees, placements, campus_info, complaint, human_request, other.
- caller_type: one of prospective_student, parent, current_student, faculty, alumni, other, unknown, from what they said.
- escalation_reason: why a person was offered or the call was transferred, else empty.
- language_used: one of english, tamil, mixed. The caller's main language.
- lead_course: the course or department the caller wants, else empty.
- lead_name: the caller's name if given, else empty.
- lead_phone: the phone number the caller gave, digits only, else empty.
Use empty strings for anything not in the call. Do not invent details."""

# Outbound (a lead called back): the Indus outbound agent's output variables
# (docs/Calling_Agent_Outbound_Config.md section 5); language_used gains "tanglish" per the
# 2026-10-09 changes. Success = call_outcome visit_booked.
OUTBOUND_FIELDS = {
    "call_outcome": ("interested", "visit_booked", "callback_requested", "not_interested", "wrong_number",
                     "voicemail", "no_answer", "transferred"),
    "interest_level": ("high", "medium", "low", "none", "unclear"),
    "visit_date": None,
    "callback_time": None,
    "do_not_call": ("no", "yes"),
    "language_used": ("english", "tamil", "tanglish"),
    "call_summary": None,
}

OUTBOUND_PROMPT = """You read the transcript of an OUTBOUND phone call: Agni College of Technology called back a person who had enquired about admissions. Fill in a record.
Answer with one JSON object only, with exactly these keys:
- call_outcome: one of interested, visit_booked, callback_requested, not_interested, wrong_number, voicemail, no_answer, transferred. The single primary outcome. visit_booked only if they agreed a day for a campus visit or an admissions call.
- interest_level: one of high, medium, low, none, unclear. How interested is the person in joining the college?
- visit_date: the campus visit or admissions-call day and time they agreed to, as they said it, else empty.
- callback_time: when they asked to be called back, else empty.
- do_not_call: yes only if they asked not to be called again, to be removed, or said they are not interested; else no.
- language_used: one of english, tamil, tanglish (Tamil and English mixed). The person's main language.
- call_summary: one or two sentences, including their questions.
Use empty strings for anything not in the call. Do not invent details."""

_IST = timezone(timedelta(hours=5, minutes=30))
SHEET_COLUMNS = ["timestamp_ist", "session_id", "caller_role", "caller_number", "ended_by", "transferred_to", *FIELDS]


def _clean(raw: dict, fields: dict | None = None) -> dict:
    out = {}
    for key, allowed in (fields or FIELDS).items():
        value = str(raw.get(key, "") or "").strip()
        if allowed:
            value = value.lower() if value.lower() in allowed else ("unclear" if "unclear" in allowed else "other" if "other" in allowed else allowed[0])
        if key == "lead_phone":
            value = re.sub(r"\D", "", value)
        out[key] = value
    return out


def _transcript_path(session_id: str):
    from conversation_logger import LOGS_DIR, _safe_session_id

    found = sorted(LOGS_DIR.glob(f"*/{_safe_session_id(session_id)}/transcript.json"))
    return found[-1] if found else None


async def _extract(messages: list[dict], prompt: str = PROMPT) -> dict | None:
    from openai import AsyncOpenAI

    text = "\n".join(f"{m['role']}: {m['content']}" for m in messages if m.get("role") in ("user", "assistant") and m.get("content"))
    if not text.strip():
        return None
    client = AsyncOpenAI(api_key=os.environ["SARVAM_API_KEY"], base_url="https://api.sarvam.ai/v1")
    r = await client.chat.completions.create(
        model="sarvam-105b", temperature=0, extra_body={"reasoning_effort": None},
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": text}],
    )
    return json.loads(re.search(r"\{.*\}", r.choices[0].message.content or "", re.S).group())


def _append_sheet(row: list) -> bool:
    sheet_id, creds_path = os.environ.get("CALLS_SHEET_ID"), os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sheet_id or not creds_path:
        logger.warning("Calls sheet not configured (CALLS_SHEET_ID / GOOGLE_SERVICE_ACCOUNT_JSON); record kept on disk only")
        return False
    import gspread
    from google.oauth2.service_account import Credentials

    book = gspread.authorize(Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"])).open_by_key(sheet_id)
    tab = os.environ.get("CALLS_SHEET_TAB", "Calls")
    try:
        sheet = book.worksheet(tab)
    except gspread.WorksheetNotFound:
        sheet = book.add_worksheet(title=tab, rows=1000, cols=len(SHEET_COLUMNS))
        sheet.append_row(SHEET_COLUMNS)
    sheet.append_row(row)
    return True


async def record_call(session_id: str, role: str, caller_number: str, ended_by: str, transfer_to,
                      direction: str = "inbound", lead=None) -> dict | None:
    """Never raises: a failure here must not touch the call or its transcript."""
    outbound = direction == "outbound"
    try:
        path = _transcript_path(session_id)
        if not path:
            logger.warning(f"[post-call] no transcript for {session_id}")
            return None
        messages = json.loads(path.read_text(encoding="utf-8")).get("messages", [])
        raw = await _extract(messages, OUTBOUND_PROMPT if outbound else PROMPT)
        if raw is None:
            return None
        record = _clean(raw, OUTBOUND_FIELDS if outbound else FIELDS)
        if outbound:
            # What the call itself knows beats the model's reading of the transcript.
            said = [m for m in messages if m.get("role") == "user" and (m.get("content") or "").strip()]
            if not said:
                # Answered and gone without a word: the model once marked such a call
                # "do not call" (2026-10-21), which would have put the number on the DND list.
                record.update({"call_outcome": "no_answer", "do_not_call": "no", "interest_level": "unclear"})
            if ended_by == "voicemail":
                record["call_outcome"] = "voicemail"
            elif transfer_to:
                record["call_outcome"] = "transferred"
            record["direction"] = "outbound"
            if lead is not None:
                record.update({"lead_name": lead.name, "lead_course": lead.course,
                               "enquiry_source": lead.source, "enquiry_date": lead.date})
        else:
            # The number already told us who called; the model labelled a faculty caller a
            # "current_student" (2026-10-21) from the transcript alone.
            known = {"student": "current_student", "parent": "parent", "faculty": "faculty"}.get(role)
            if known:
                record["caller_type"] = known
        if transfer_to and not outbound:
            record["call_disposition"] = "transferred"
        record.update({
            "session_id": session_id, "caller_role": role, "ended_by": ended_by,
            "caller_number": ("XXXXXX" + caller_number[-4:]) if len(caller_number) >= 4 else "",
            "transferred_to": transfer_to[0] if transfer_to else "",
            "timestamp_ist": datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S"),
        })
        path.with_name("call_record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        # Outbound results stay with the call and its campaign; the Sheet's Calls tab is inbound only.
        wrote = False if outbound else await asyncio.to_thread(_append_sheet, [record.get(c, "") for c in SHEET_COLUMNS])
        outcome = record.get("call_outcome") if outbound else record.get("call_disposition")
        logger.info(f"[post-call] {session_id[:16]} {direction} outcome={outcome} sheet={'yes' if wrote else 'no'}")
        return record
    except Exception:
        logger.exception("[post-call] failed")
        return None
