"""The tools the model can call on a phone call, and what each one does.

Same job as the Indus tools: the records tools return erp-lookup's ready-to-speak
`speech` for the model to say as it is. Caller identification is not a tool here: it
runs once before the greeting (call_pipeline.py) and the role reaches the backend as a
header. Transfer is one tool, not Indus's two: the model gives the department and the
one line to say, and we say it, then hand the call over.
"""
import re

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from spoken_numbers import normalize_spoken_numbers

# Same patterns as rag-backend/main.py (_DATE_LIKE, _LONG_NUMBER): what counts as the caller having
# said a date of birth and a register number.
_DATE_LIKE = re.compile(
    r"january|february|march|april|may|june|july|august|september|october|november|december"
    r"|ஜனவரி|பிப்ரவரி|மார்ச்|ஏப்ரல்|மே|ஜூன்|ஜூலை|ஆகஸ்ட்|செப்டம்பர்|அக்டோபர்|நவம்பர்|டிசம்பர்"
    r"|\b\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{2,4}\b", re.I)
_LONG_NUMBER = re.compile(r"(?:\d\D*){8,}")


def missing_record_detail(context) -> str | None:
    """What the caller has not actually said yet, or None. The model once called the records
    tool with a date of birth it had made up, which counted as a failed try."""
    said = " ".join(m.get("content") or "" for m in context.messages
                    if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str))
    said = normalize_spoken_numbers(said)  # English-mode recognition writes numbers as words
    if not _LONG_NUMBER.search(said):
        return "register number"
    if not _DATE_LIKE.search(said):
        return "date of birth"
    return None


_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
           "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
           "ஜனவரி": 1, "பிப்ரவரி": 2, "மார்ச்": 3, "ஏப்ரல்": 4, "மே": 5, "ஜூன்": 6, "ஜூலை": 7,
           "ஆகஸ்ட்": 8, "செப்டம்பர்": 9, "அக்டோபர்": 10, "நவம்பர்": 11, "டிசம்பர்": 12}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_TAIL = r"[^\s\d,.]*"  # rest of the month word (Tamil case endings and "of")
_DMY_NUMERIC = re.compile(r"\b(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{4})\b")
_D_MONTH_Y = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTH_ALT}){_TAIL}\.?,?\s*(\d{{4}})", re.I)
_MONTH_D_Y = re.compile(rf"({_MONTH_ALT}){_TAIL}\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})", re.I)


def date_the_caller_said(context) -> str | None:
    """The date of birth in the caller's own words as DD-MM-YYYY (day first, the Indian way), or
    None when it cannot be read. The model turned "April 12, 2005" into 04-12-2005 on a real call
    (2026-10-20), which failed verification; the caller's words are the source of truth."""
    for m in reversed(context.messages):
        text = m.get("content") if isinstance(m, dict) and m.get("role") == "user" else None
        if not isinstance(text, str):
            continue
        text = normalize_spoken_numbers(text)
        for rx, order in ((_D_MONTH_Y, "dmy"), (_MONTH_D_Y, "mdy"), (_DMY_NUMERIC, "num")):
            hit = rx.search(text)
            if not hit:
                continue
            a, b, year = hit.groups()
            if order == "num":
                day, month = int(a), int(b)
            elif order == "dmy":
                day, month = int(a), _MONTHS[b.lower()]
            else:
                day, month = int(b), _MONTHS[a.lower()]
            if 1 <= day <= 31 and 1 <= month <= 12:
                return f"{day:02d}-{month:02d}-{year}"
    return None


def _compact(text: str) -> str:
    """'F A C one zero zero one', 'fac-1001' -> 'FAC1001': letters and digits only, numbers as digits."""
    return re.sub(r"[^A-Z0-9]", "", normalize_spoken_numbers(text or "").upper())


def caller_words_since_last_lookup(context) -> str:
    """What the caller said after the last tool result (or in the whole call before the first). Used
    as class_name when the model leaves it out: a caller who said "my ECE class" up front was asked
    to pick a class again (2026-10-21). Only words since the last answer, so "my other class" after
    a CSE answer does not pick CSE again."""
    said = []
    for m in reversed(context.messages):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            break
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            said.append(m["content"])
    return " ".join(reversed(said))


def faculty_id_said(context, faculty_id: str) -> bool:
    """True when the faculty ID the model passes appears in the caller's own words. Same reason
    as missing_record_detail: a made-up ID would count as a failed try against the caller."""
    wanted = _compact(faculty_id)
    if len(wanted) < 3:
        return False
    said = " ".join(m.get("content") or "" for m in context.messages
                    if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str))
    return wanted in _compact(said)


TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema(
        name="get_student_report",
        description="Marks or attendance for a student, for a verified registered student or parent caller. "
                    "Only call it after the caller has given the register number and date of birth.",
        properties={
            "reg_no": {"type": "string", "description": "The student's register number, digits only."},
            "date_of_birth": {"type": "string", "description": "The student's date of birth as DD-MM-YYYY, day first: April 12, 2005 is 12-04-2005."},
            "topic": {"type": "string", "enum": ["marks", "attendance", "both"]},
            "subject": {"type": "string", "description": "One subject if the caller asked about one, else empty."},
        },
        required=["reg_no", "date_of_birth", "topic"],
    ),
    FunctionSchema(
        name="get_class_summary",
        description="For a faculty caller: one of the classes they are in charge of, either the students below "
                    "75 percent attendance or the latest internal assessment result. Only call it after the caller "
                    "has said their faculty ID.",
        properties={
            "faculty_id": {"type": "string", "description": "The caller's faculty ID as they said it, letters and digits."},
            "topic": {"type": "string", "enum": ["low_attendance", "marks_summary"]},
            "class_name": {"type": "string", "description": "Which of their classes, in the caller's words, e.g. "
                                                            "'CSE 2023' or 'fourth year ECE'. Empty if not named yet."},
        },
        required=["faculty_id", "topic"],
    ),
    FunctionSchema(
        name="transfer_call",
        description="Hand the call to a person. Give the department and the one short line to say first.",
        properties={
            "department": {"type": "string", "enum": ["reception", "admissions", "placement"]},
            "line": {"type": "string", "description": "One short sentence in the caller's language, e.g. 'Connecting you to the admissions team now.'"},
        },
        required=["department", "line"],
    ),
    FunctionSchema(
        name="end_call",
        description="End the call after the caller is done. Give the short goodbye to say.",
        properties={"goodbye": {"type": "string", "description": "A goodbye of under ten words in the caller's language."}},
        required=["goodbye"],
    ),
])


# Outbound calls (a lead called back): no records tools; transfer goes to admissions, and
# leave_voicemail stands in for Indus's "Handle Voicemail".
_by_name = {t.name: t for t in TOOLS.standard_tools}
OUTBOUND_TOOLS = ToolsSchema(standard_tools=[
    FunctionSchema(
        name="transfer_call",
        description="Hand the call to the admissions team. Give the one short line to say first.",
        properties={
            "department": {"type": "string", "enum": ["admissions"]},
            "line": _by_name["transfer_call"].properties["line"],
        },
        required=["department", "line"],
    ),
    _by_name["end_call"],
    FunctionSchema(
        name="leave_voicemail",
        description="Call this at once when the call reached voicemail, an answering machine or a network "
                    "announcement (\"leave a message after the tone\", \"the number you have called is not available\"). "
                    "A short message is left and the call ends.",
        properties={},
        required=[],
    ),
])


def make_handlers(erp, control, caller_number: str) -> dict:
    async def get_student_report(params: FunctionCallParams):
        a = params.arguments
        missing = missing_record_detail(params.context)
        if missing:
            logger.warning(f"[tool] get_student_report refused: the caller has not said the {missing}")
            await params.result_callback({"status": "need_details",
                                          "speech": f"Ask the caller for the student's {missing}. Do not guess it."})
            return
        logger.info(f"[tool] get_student_report topic={a.get('topic')} subject={a.get('subject')!r}")  # no register number or DOB in logs
        dob = date_the_caller_said(params.context) or str(a.get("date_of_birth", ""))
        if dob != str(a.get("date_of_birth", "")):
            logger.info("[tool] date of birth taken from the caller's words, not the model's argument")
        await params.result_callback(await erp.student(
            caller_number, str(a.get("reg_no", "")), dob,
            str(a.get("topic", "both")), str(a.get("subject", ""))))

    verified = {"faculty_id": ""}  # the faculty ID erp-lookup accepted on this call

    async def get_class_summary(params: FunctionCallParams):
        a = params.arguments
        faculty_id = _compact(str(a.get("faculty_id", "")))
        if not faculty_id and verified["faculty_id"]:
            faculty_id = verified["faculty_id"]  # the model left it out when the caller picked a class
        elif faculty_id != verified["faculty_id"] and not faculty_id_said(params.context, faculty_id):
            logger.warning("[tool] get_class_summary refused: the caller has not said that faculty ID")
            await params.result_callback({"status": "need_details",
                                          "speech": "Ask the caller for their faculty ID. Do not guess it."})
            return
        class_name = str(a.get("class_name", "")).strip() or caller_words_since_last_lookup(params.context)
        logger.info(f"[tool] get_class_summary topic={a.get('topic')} class_given={bool(a.get('class_name'))}")  # no faculty ID or words in logs
        result = await erp.class_summary(caller_number, faculty_id, str(a.get("topic", "")), class_name)
        if result.get("status") in ("ok", "choose_class", "choose_topic"):
            verified["faculty_id"] = faculty_id
        await params.result_callback(result)

    async def transfer_call(params: FunctionCallParams):
        ok = await control.transfer(str(params.arguments.get("department", "reception")),
                                    str(params.arguments.get("line", "")))
        if ok:
            from pipecat.frames.frames import FunctionCallResultProperties
            await params.result_callback({"status": "ok"}, properties=FunctionCallResultProperties(run_llm=False))
        else:  # no number to dial: the model apologises and offers a callback
            await params.result_callback({"status": "error", "speech": "No transfer number is available right now."})

    async def end_call(params: FunctionCallParams):
        from pipecat.frames.frames import FunctionCallResultProperties
        await params.result_callback({"status": "ok"}, properties=FunctionCallResultProperties(run_llm=False))
        await control.say_and_end(str(params.arguments.get("goodbye", "")), "end_call")

    async def leave_voicemail(params: FunctionCallParams):
        from pipecat.frames.frames import FunctionCallResultProperties

        from prompts import voicemail_message

        await params.result_callback({"status": "ok"}, properties=FunctionCallResultProperties(run_llm=False))
        await control.say_and_end(voicemail_message(control.language()), "voicemail")

    return {"get_student_report": get_student_report, "get_class_summary": get_class_summary,
            "transfer_call": transfer_call, "end_call": end_call, "leave_voicemail": leave_voicemail}
