"""Live check of the phone prompt + tools, with typed turns instead of audio.

Needs rag-backend (RAG_BACKEND_PUBLIC_URL) and erp-lookup (ERP_URL) running, with the fake
contacts loaded. It plays scripted callers against the real model, runs the tools the way the
phone agent does, and prints what the bot said and which tools it called. Costs a few Sarvam calls.

    python live/records_dialogs.py            (all dialogs)
    python live/records_dialogs.py faculty    (faculty dialogs only)
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: F401,E402
import aiohttp  # noqa: E402
from erp_client import ErpClient  # noqa: E402
from tools import TOOLS, _compact, caller_words_since_last_lookup, faculty_id_said  # noqa: E402

OPENAI_TOOLS = [{"type": "function", "function": f.to_default_dict()} for f in TOOLS.standard_tools]
BACKEND = os.environ["RAG_BACKEND_PUBLIC_URL"].rstrip("/")
KEY = os.environ.get("RAG_BACKEND_AUTH_KEY", "")


async def run_tool(erp, number, name, args, messages, state):
    if name == "get_student_report":
        return await erp.student(number, str(args.get("reg_no", "")), str(args.get("date_of_birth", "")),
                                 str(args.get("topic", "both")), str(args.get("subject", "")))
    if name == "get_class_summary":  # same guard and memory as tools.get_class_summary
        fid = _compact(str(args.get("faculty_id", "")))
        if not fid and state.get("fid"):
            fid = state["fid"]
        elif fid != state.get("fid") and not faculty_id_said(type("C", (), {"messages": messages})(), fid):
            return {"status": "need_details", "speech": "Ask the caller for their faculty ID. Do not guess it."}
        ctx = type("C", (), {"messages": messages})()
        class_name = str(args.get("class_name", "")).strip() or caller_words_since_last_lookup(ctx)
        result = await erp.class_summary(number, fid, str(args.get("topic", "")), class_name)
        if result.get("status") in ("ok", "choose_class", "choose_topic"):
            state["fid"] = fid
        return result
    if name == "transfer_call":
        return {"status": "ok", "transfer": await erp.route(str(args.get("department", "reception")))}
    return {"status": "ok"}


async def dialog(http, title, number, turns):
    erp = ErpClient()
    who = await erp.identify(number)
    headers = {"Authorization": f"Bearer {KEY}", "X-Source": "phone", "X-Session-Id": f"live-{title}-{os.getpid()}",
               "X-Caller-Role": who.get("role", "unknown"), "X-Caller-Name": who.get("first_name", "")}
    print(f"\n=== {title}  (caller {number[-4:]}, role {who.get('role')})")
    messages, state = [], {}
    for said in turns:
        print(f"CALLER: {said}")
        messages.append({"role": "user", "content": said})
        for _ in range(3):
            async with http.post(f"{BACKEND}/v1/chat/completions", headers=headers,
                                 json={"model": "agni-ai", "messages": messages, "tools": OPENAI_TOOLS}) as r:
                msg = (await r.json())["choices"][0]["message"]
            if msg.get("content"):
                print(f"BOT:    {msg['content']}")
            calls = msg.get("tool_calls") or []
            messages.append({k: v for k, v in msg.items() if v is not None})
            if not calls:
                break
            for c in calls:
                args = json.loads(c["function"]["arguments"] or "{}")
                shown = {k: ("<hidden>" if k in ("reg_no", "date_of_birth") else v) for k, v in args.items()}
                result = await run_tool(erp, number, c["function"]["name"], args, messages, state)
                print(f"TOOL:   {c['function']['name']}({shown}) -> {json.dumps(result)[:150]}")
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
                if c["function"]["name"] in ("end_call", "transfer_call"):
                    return


async def faculty_dialogs(http):
    # FAC1001 (9000000004) has two classes: CSE 2023 A (attendance only in the data) and ECE 2025 A (marks only).
    await dialog(http, "faculty-two-classes", "9000000004", [
        "Which students in my class are below seventy five percent attendance?", "My faculty ID is FAC 1001",
        "yes", "the CSE class"])
    await dialog(http, "faculty-marks-class-named", "9000000004", [
        "I need the latest internal assessment result for my ECE class", "F A C one zero zero one", "yes, correct"])
    # FAC1002 (9000000006) has one class, so no list.
    await dialog(http, "faculty-one-class", "9000000006", [
        "How did my class do in the latest test?", "FAC1002", "yes"])
    # Replay of a real Tamil call (2026-10-21): attendance for one class, then marks for the other.
    # Before the fix the second request was sent to the reception.
    await dialog(http, "faculty-tamil-two-requests", "9000000004", [
        "என்னோட கிளாஸோட இன்ஃபர்மேஷன் வேணும்.",
        "எனக்கு வந்து அட்டனன்ஸ் சம்மரி தான் வேணும். என்னோட ஃபேக்கல்டி ஐடி வந்து FAC1001.",
        "கரெக்ட் தாங்க. என்னோட கிளாஸ்ஸோட சம்மரி சொல்லுங்க.",
        "என்னோட கிளாஸ் வந்து சிஎஸ்சி 2023 ஏ செக்ஷன்.",
        "சரி ஓகேங்க.",
        "அப்படியே என்னோட வேற ஒரு கிளாஸ் இருக்கு. ECE 2025 A செக்ஷன். அதுல இருந்து எனக்கு மார்க்ஸ் வேணும்."])
    # A wrong ID (counts one failed try on 9000000006; unlock afterwards).
    await dialog(http, "faculty-wrong-id", "9000000006", [
        "Show me students with low attendance", "my ID is FAC 9999", "yes"])


async def main():
    async with aiohttp.ClientSession() as http:
        if sys.argv[1:] == ["faculty"]:
            await faculty_dialogs(http)
            return
        await dialog(http, "student-marks", "9000000003", [
            "Hi, I want to know my marks", "312825104001", "first January two thousand seven", "marks"])
        await dialog(http, "tamil-attendance", "9000000001", [
            "Tell me my attendance percentage.", "என்னோட ரெஜிஸ்டர் நம்பர் வந்து 312823104004.", "ஆமா",
            "என்னோட டேட் ஆஃப் பர்த் வந்து ஏப்ரல் 12, 2005."])
        await dialog(http, "parent-attendance", "9000000002", [
            "I want to know my daughter's attendance", "312823104004", "yes", "twelfth April two thousand five"])
        # Last of the records dialogs: three wrong tries lock caller 9000000001 for an hour.
        await dialog(http, "wrong-dob", "9000000001", [
            "Tell me my attendance", "312823104004", "first January two thousand", "attendance",
            "12-05-2005", "312823104004", "03-03-2003"])
        await dialog(http, "unknown-caller", "9111111111", ["Can you tell me my son's marks?"])
        await faculty_dialogs(http)
        await dialog(http, "transfer", "9111111111", ["I want to talk to someone in admissions about fees"])
        await dialog(http, "office-open-now", "9111111111", ["Is the college office open right now?"])
        # Writes a real row to the leads Sheet (source phone); delete it afterwards.
        if os.environ.get("LIVE_LEAD_TEST") == "1":
            await dialog(http, "lead-phone", "9111111111", [
                "I'm interested in joining computer science", "My name is Local Test Delete and my number is 9876543210",
                "yes that's right"])
        await dialog(http, "wrapping-up", "9111111111", ["What courses do you offer?", "Okay, thank you, that's all."])


asyncio.run(main())
