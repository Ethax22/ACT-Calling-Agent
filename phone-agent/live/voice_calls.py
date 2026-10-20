"""Live voice checks of the call housekeeping, through the browser test line.

    python live/voice_calls.py <check> [--url ws://127.0.0.1:8002]

checks:
  end-call   caller wraps up; the call must be closed from our side after the goodbye
  transfer   caller asks for admissions; the page must get a transfer message, then the call ends
  quiet      caller says nothing; nudge, second nudge/goodbye, then the call ends
             (start the server with PHONE_NUDGE_SECS=3)
  cap        caller says nothing; the call ends at the time limit
             (start the server with CALL_MAX_MINUTES=0.4 PHONE_NUDGE_SECS=100)
  records    registered student asks for marks by voice (caller 9000000003)

Uses the website scenario harness's synthetic caller voice and costs a few vendor calls.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: F401,E402

import aiohttp  # noqa: E402
import scenario_test as h  # noqa: E402  (the synthetic caller from voice-pipeline)
import websockets  # noqa: E402


class PhoneCall(h.Call):
    """Also records the JSON messages the page would get."""

    def __init__(self, ws):
        self.messages = []
        self.closed_at = None
        super().__init__(ws)

    async def _receive(self):
        try:
            async for message in self.ws:
                if isinstance(message, (bytes, bytearray)):
                    self.audio.append((self.now(), len(message)))
                else:
                    data = json.loads(message)
                    self.messages.append((self.now(), data))
                    if data.get("type") == "UserStartedSpeaking":
                        self.stops.append(self.now())
        except Exception:
            pass
        self.closed_at = self.now()


async def open_call(url, caller):
    ws = await websockets.connect(f"{url}/test/ws?caller={caller}", max_size=None)
    return PhoneCall(ws)


async def wait_closed(call, seconds):
    deadline = call.now() + seconds
    while call.closed_at is None and call.now() < deadline:
        await asyncio.sleep(0.1)
    return call.closed_at


def audio_seconds(call):
    return sum(n for _, n in call.audio) / 16000  # 8 kHz PCM16


async def say(http, call, text, lang="en", wait_reply=False):
    """Speak one line. With wait_reply: then wait for the bot to start answering and finish."""
    start, _ = await call.speak(await h.synthesize(http, text, lang))
    if wait_reply:
        await call.wait_for_reply_start(start, timeout=15)
        await call.wait_until_quiet(timeout=60)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("check")
    ap.add_argument("--url", default="ws://127.0.0.1:8002")
    a = ap.parse_args()
    async with aiohttp.ClientSession() as http:
        caller = "9000000003" if a.check == "records" else "9111111111"
        call = await open_call(a.url, caller)
        await call.wait_until_quiet()
        print(f"greeting played: {audio_seconds(call):.1f}s of audio")
        if a.check == "end-call":
            await say(http, call, "What courses do you offer?", wait_reply=True)
            before, t = audio_seconds(call), call.now()
            await say(http, call, "Okay, thank you, that's all.")
            closed = await wait_closed(call, 25)
            print(f"goodbye audio: {audio_seconds(call) - before:.1f}s; call closed by server: {closed is not None}"
                  + (f" ({closed - t:.1f}s after the caller wrapped up)" if closed else ""))
        elif a.check == "transfer":
            await say(http, call, "I want to speak to someone in admissions about the fees.")
            closed = await wait_closed(call, 25)
            print("transfer message:", [m for _, m in call.messages if m.get("type") == "transfer"])
            print(f"line audio after request: {audio_seconds(call):.1f}s total; call closed by server: {closed is not None}")
        elif a.check in ("quiet", "cap"):
            closed = await wait_closed(call, 70)
            print(f"call closed by server: {closed is not None} at {closed and round(closed, 1)}s; "
                  f"total bot audio {audio_seconds(call):.1f}s")
        elif a.check == "records":
            await say(http, call, "I would like to know my marks please.", wait_reply=True)
            await say(http, call, "My register number is three one two eight two five one zero four double zero one", wait_reply=True)
            await say(http, call, "My date of birth is first January two thousand seven", wait_reply=True)
            print(f"total bot audio {audio_seconds(call):.1f}s")
        await call.close()
        await asyncio.sleep(2)
    newest = max(h.LOGS_DIR.glob("*/phone-test-*/transcript.json"), key=lambda p: p.stat().st_mtime)
    print("transcript:", newest.parent.name)
    for m in json.loads(newest.read_text(encoding="utf-8"))["messages"]:
        if m["role"] in ("user", "assistant", "tool") and m.get("content"):
            print(f"  {m['role']:9} {str(m['content'])[:170]}")


asyncio.run(main())
