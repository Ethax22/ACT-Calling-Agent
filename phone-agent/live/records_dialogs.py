"""Live check of the phone prompt + tools, with typed turns instead of audio.

Needs rag-backend (RAG_BACKEND_PUBLIC_URL) and erp-lookup (ERP_URL) running, with the fake
contacts loaded. It plays scripted callers against the real model, runs the tools the way the
phone agent does, and prints what the bot said and which tools it called. Costs a few Sarvam calls.

    python live/records_dialogs.py
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
from tools import TOOLS  # noqa: E402

OPENAI_TOOLS = [{"type": "function", "function": f.to_default_dict()} for f in TOOLS.standard_tools]
BACKEND = os.environ["RAG_BACKEND_PUBLIC_URL"].rstrip("/")
KEY = os.environ.get("RAG_BACKEND_AUTH_KEY", "")


async def run_tool(erp, number, name, args):
    if name == "get_student_report":
        return await erp.student(number, str(args.get("reg_no", "")), str(args.get("date_of_birth", "")),
                                 str(args.get("topic", "both")), str(args.get("subject", "")))
    if name == "get_class_summary":
        return await erp.class_summary(number, str(args.get("topic", "")))
    if name == "transfer_call":
        return {"status": "ok", "transfer": await erp.route(str(args.get("department", "reception")))}
    return {"status": "ok"}


async def dialog(http, title, number, turns):
    erp = ErpClient()
    who = await erp.identify(number)
    headers = {"Authorization": f"Bearer {KEY}", "X-Source": "phone", "X-Session-Id": f"live-{title}-{os.getpid()}",
               "X-Caller-Role": who.get("role", "unknown"), "X-Caller-Name": who.get("first_name", "")}
    print(f"\n=== {title}  (caller {number[-4:]}, role {who.get('role')})")
    messages = []
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
                result = await run_tool(erp, number, c["function"]["name"], args)
                print(f"TOOL:   {c['function']['name']}({shown}) -> {json.dumps(result)[:150]}")
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
                if c["function"]["name"] in ("end_call", "transfer_call"):
                    return


async def main():
    async with aiohttp.ClientSession() as http:
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
        await dialog(http, "faculty", "9000000004", ["Which students in my class are below seventy five percent attendance?"])
        await dialog(http, "transfer", "9111111111", ["I want to talk to someone in admissions about fees"])
        await dialog(http, "office-open-now", "9111111111", ["Is the college office open right now?"])
        # Writes a real row to the leads Sheet (source phone); delete it afterwards.
        if os.environ.get("LIVE_LEAD_TEST") == "1":
            await dialog(http, "lead-phone", "9111111111", [
                "I'm interested in joining computer science", "My name is Local Test Delete and my number is 9876543210",
                "yes that's right"])
        await dialog(http, "wrapping-up", "9111111111", ["What courses do you offer?", "Okay, thank you, that's all."])


asyncio.run(main())
