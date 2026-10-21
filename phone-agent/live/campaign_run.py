"""Live check of a whole outbound campaign on the browser line, without a browser.

Creates a campaign of three leads through the API, then plays the test page: the first ring
is answered and the "lead" says (by synthetic voice) "Please stop calling me", the second is
declined, the third is left to ring out. Prints what the campaign, the DND list and the call
record say afterwards. Needs the phone agent with a short ring time:

    DIALLER_RING_SECS=8  (in the phone agent's environment)
    python live/campaign_run.py

Writes to data/campaigns.db (a campaign named "live check ..." and one DND entry for the test
number 9876500001, which it removes again at the end). It cancels earlier live-check campaigns first.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: F401,E402

import aiohttp  # noqa: E402
import scenario_test as h  # noqa: E402
import websockets  # noqa: E402

BASE = "http://127.0.0.1:8002"
CSV = ("phone,lead_name,lead_course,enquiry_source,enquiry_date\n"
       "9876500001,Ravi,B.E. Computer Science,our website,recently\n"
       "9876500002,Priya,B.E. ECE,a phone call,2026-10-04\n"
       "9876500003,Arun,B.E. Mechanical,our website,recently\n")


async def poll(http, seconds=40):
    """Be the open test page until something rings."""
    end = time.time() + seconds
    while time.time() < end:
        async with http.get(f"{BASE}/test/api/ringing") as r:
            d = await r.json()
        if d.get("ringing"):
            return d["ringing"]
        await asyncio.sleep(1)
    return None


async def main():
    async with aiohttp.ClientSession() as http:
        # A clean start: earlier live checks must not ring in between, or block the test numbers.
        async with http.get(f"{BASE}/test/api/campaigns") as r:
            for c in (await r.json())["campaigns"]:
                if c["name"].startswith("live check") and c["status"] in ("scheduled", "running"):
                    await http.post(f"{BASE}/test/api/campaigns/{c['id']}/cancel")
        for n in ("9876500001", "9876500002", "9876500003"):
            await http.delete(f"{BASE}/test/api/dnd/{n}")
        await asyncio.sleep(2)
        async with http.post(f"{BASE}/test/api/recipients/parse", json={"text": CSV}) as r:
            parsed = await r.json()
        spec = {"name": f"live check {time.strftime('%H:%M:%S')}", "start_at": "now", "days": "1111111",
                "window_start": "00:00", "window_end": "23:59", "max_retries": 1, "retry_interval_min": 60,
                "retry_on": ["busy", "no_answer"],
                "cohort": {"name": "Outbound_Demo", "source": "csv", "file_name": "demo.csv",
                           "rows": parsed["rows"], "mapping": parsed["suggested"]}}
        async with http.post(f"{BASE}/test/api/campaigns", json=spec) as r:
            camp = await r.json()
        print(f"campaign {camp['name']}: {camp['stats']['audience']} recipients, status {camp['status']}")

        ring = await poll(http)
        print(f"1st ring: {ring['lead_name']} ({ring['phone_masked']}) -> answer")
        ws = await websockets.connect(f"ws://127.0.0.1:8002/test/ws?attempt={ring['attempt_id']}", max_size=None)
        call = h.Call(ws)
        await call.wait_until_quiet()
        start, _ = await call.speak(await h.synthesize(http, "Please stop calling me.", "en"))
        for _ in range(60):  # the bot apologises and ends the call
            if ws.close_code is not None:
                break
            await asyncio.sleep(0.5)
        print(f"   call closed by the agent: {ws.close_code is not None}")
        await call.close()

        ring = await poll(http)
        print(f"2nd ring: {ring['lead_name']} -> decline")
        async with http.post(f"{BASE}/test/api/ringing/{ring['attempt_id']}/decline") as r:
            print("   declined:", (await r.json())["declined"])

        ring = await poll(http)
        print(f"3rd ring: {ring['lead_name']} -> let it ring out")
        while (await poll(http, 2)) and True:
            await asyncio.sleep(1)
        await asyncio.sleep(25)  # the post-call record (one Sarvam call)

        async with http.get(f"{BASE}/test/api/campaigns/{camp['id']}") as r:
            detail = await r.json()
        print(f"\ncampaign status {detail['status']}; stats {json.dumps({k: detail['stats'][k] for k in ('audience', 'finished', 'completion_rate', 'attempts', 'connected', 'pickup_rate')})}")
        for c in detail["calls"]:
            print(f"   {c['lead_name']:6} {c['phone_masked']} status={c['status']:10} attempts={c['attempts']} "
                  f"reason={c['failure_reason']} next={c['next_attempt_at']} outcome={c['outcome']} dnc={c['do_not_call']}")
        print("   timeline:", detail["timeline"])
        async with http.get(f"{BASE}/test/api/dnd") as r:
            dnd = (await r.json())["dnd"]
        print("DND list:", [(d["phone_masked"], d["reason"], d["source"]) for d in dnd])
        ravi = next(c for c in detail["calls"] if c["lead_name"] == "Ravi")
        if ravi["last_call_id"]:
            async with http.get(f"{BASE}/test/api/calls/{ravi['last_call_id']}") as r:
                call_detail = await r.json()
            print("call record:", json.dumps(call_detail.get("record"), ensure_ascii=False)[:400])
            print("call meta campaign:", call_detail["meta"].get("campaign"))
        async with http.post(f"{BASE}/test/api/campaigns/{camp['id']}/cancel") as r:
            pass
        async with http.delete(f"{BASE}/test/api/dnd/9876500001") as r:
            print("test number removed from DND:", (await r.json())["removed"])


asyncio.run(main())
