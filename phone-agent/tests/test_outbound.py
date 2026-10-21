"""Outbound calls (the college calls a lead back): greeting, tools, voicemail, nudge, call record."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SARVAM_API_KEY", "t")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: F401
import call_control
import post_call
import prompts
import tools
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
from test_phase3 import FakeErp, FakeWorker, _params


def test_outbound_greeting_names_the_lead_and_falls_back_like_indus():
    g = prompts.outbound_greeting(prompts.Lead(name="Ravi", course="B.E. CSE"))
    assert g.startswith("Hello Ravi, this is Agni AI calling from Agni College of Technology.")
    assert "may be recorded" in g and "B.E. CSE" in g and g.endswith("talk for a minute?")
    assert "Hello there" in prompts.outbound_greeting(prompts.Lead()) and "engineering" in prompts.outbound_greeting(prompts.Lead())


def test_outbound_tools_have_no_records_tools_and_a_voicemail_tool():
    names = [t.name for t in tools.OUTBOUND_TOOLS.standard_tools]
    assert names == ["transfer_call", "end_call", "leave_voicemail"]
    assert tools.OUTBOUND_TOOLS.standard_tools[0].properties["department"]["enum"] == ["admissions"]


def test_voicemail_leaves_the_message_with_the_number_digit_by_digit_then_ends(monkeypatch):
    monkeypatch.setenv("ADMISSIONS_PHONE", "+91 73580 11190")
    control = call_control.CallControl(FakeErp(), 1000, outbound=True)
    control._ns = SimpleNamespace(worker=FakeWorker(), router=SimpleNamespace(language="en"))
    handlers = tools.make_handlers(FakeErp(), control, "9876543210")
    p, got = _params({})
    asyncio.run(handlers["leave_voicemail"](p))
    spoken = [f.text for f in control._ns.worker.queued if isinstance(f, TTSSpeakFrame)]
    assert "7 3 5 8 0 1 1 1 9 0" in spoken[0] and "Agni AI" in spoken[0]
    assert isinstance(control._ns.worker.queued[-1], EndWorkerFrame) and control.ended_by == "voicemail"
    assert got["properties"].run_llm is False


def test_outbound_nudges_once_then_ends():
    control = call_control.CallControl(FakeErp(), 1000, outbound=True)
    control._ns = SimpleNamespace(worker=FakeWorker(), router=SimpleNamespace(language="en"))
    asyncio.run(control._on_idle())
    assert control._ns.worker.queued[0].text == "Hello, are you still there?"
    asyncio.run(control._on_idle())
    assert isinstance(control._ns.worker.queued[-1], EndWorkerFrame) and control.ended_by == "idle"


def test_outbound_call_record_uses_the_outbound_fields_and_what_the_call_knows(monkeypatch, tmp_path):
    t = tmp_path / "transcript.json"
    t.write_text(json.dumps({"messages": [{"role": "user", "content": "leave a message"}]}), encoding="utf-8")
    monkeypatch.setattr(post_call, "_transcript_path", lambda s: t)
    sheet = []
    monkeypatch.setattr(post_call, "_append_sheet", lambda row: sheet.append(row) or True)

    async def extract(messages, prompt=post_call.PROMPT):
        assert prompt is post_call.OUTBOUND_PROMPT
        return {"call_outcome": "interested", "interest_level": "HIGH", "do_not_call": "maybe", "language_used": "Tanglish"}

    monkeypatch.setattr(post_call, "_extract", extract)
    lead = prompts.Lead(name="Ravi", course="B.E. CSE", source="our website", date="recently")
    rec = asyncio.run(post_call.record_call("phone-test-o", "lead", "9876543210", "voicemail", None,
                                            direction="outbound", lead=lead))
    assert rec["call_outcome"] == "voicemail" and rec["interest_level"] == "high" and rec["do_not_call"] == "no"
    assert rec["language_used"] == "tanglish" and rec["lead_name"] == "Ravi" and rec["direction"] == "outbound"
    assert "call_disposition" not in rec and not sheet   # outbound stays out of the inbound Calls tab


def test_a_silent_outbound_call_is_never_do_not_call(monkeypatch, tmp_path):
    t = tmp_path / "transcript.json"
    t.write_text(json.dumps({"messages": [{"role": "assistant", "content": "Hello Ravi..."}]}), encoding="utf-8")
    monkeypatch.setattr(post_call, "_transcript_path", lambda s: t)

    async def extract(messages, prompt=None):
        return {"call_outcome": "not_interested", "do_not_call": "yes"}   # what the model said on a real silent call

    monkeypatch.setattr(post_call, "_extract", extract)
    rec = asyncio.run(post_call.record_call("phone-test-s", "lead", "9876543210", "", None, direction="outbound",
                                            lead=prompts.Lead(name="Ravi")))
    assert rec["do_not_call"] == "no" and rec["call_outcome"] == "no_answer"
