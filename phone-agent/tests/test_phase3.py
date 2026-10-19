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
import tools
from erp_client import ERP_DOWN, ErpClient
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame


class FakeErp:
    def __init__(self, route=None):
        self._route = route or {"status": "ok", "department": "admissions", "transfer_number": "+911234567890"}
        self.calls = []

    async def student(self, *a):
        self.calls.append(("student", a))
        return {"status": "ok", "speech": "For Abinaya. Maths 80 out of 100."}

    async def class_summary(self, *a):
        self.calls.append(("class", a))
        return {"status": "ok", "speech": "3 students are below 75 percent."}

    async def route(self, department):
        return self._route


class FakeWorker:
    def __init__(self):
        self.queued = []

    async def queue_frames(self, frames):
        self.queued.extend(frames)


def _control(erp=None, max_secs=1000):
    c = call_control.CallControl(erp or FakeErp(), max_secs)
    c._ns = SimpleNamespace(worker=FakeWorker(), router=SimpleNamespace(language="en"))
    return c


def _params(args):
    got = {}

    async def result_callback(result, properties=None):
        got["result"], got["properties"] = result, properties

    return SimpleNamespace(arguments=args, result_callback=result_callback), got


def test_records_tools_pass_the_erp_speech_through_with_the_callers_number():
    erp, control = FakeErp(), _control()
    handlers = tools.make_handlers(erp, control, "9000000001")
    p, got = _params({"reg_no": "312823104004", "date_of_birth": "12-04-2005", "topic": "marks"})
    asyncio.run(handlers["get_student_report"](p))
    assert got["result"]["speech"].startswith("For Abinaya")
    assert erp.calls[0][1][0] == "9000000001" and erp.calls[0][1][1] == "312823104004"
    p, got = _params({"topic": "low_attendance"})
    asyncio.run(handlers["get_class_summary"](p))
    assert "75 percent" in got["result"]["speech"]


def test_the_four_tool_names_the_model_sees():
    assert [t.name for t in tools.TOOLS.standard_tools] == [
        "get_student_report", "get_class_summary", "transfer_call", "end_call"]


def test_end_call_says_goodbye_then_ends_without_running_the_model_again():
    control = _control()
    handlers = tools.make_handlers(FakeErp(), control, "")
    p, got = _params({"goodbye": "Thanks, bye."})
    asyncio.run(handlers["end_call"](p))
    frames = control._ns.worker.queued
    assert isinstance(frames[0], TTSSpeakFrame) and frames[0].text == "Thanks, bye."
    assert isinstance(frames[-1], EndWorkerFrame)
    assert got["properties"].run_llm is False and control.ended_by == "end_call"


def test_transfer_speaks_the_line_tells_the_page_and_ends():
    control = _control()
    assert asyncio.run(control.transfer("admissions", "Connecting you now.")) is True
    kinds = [type(f).__name__ for f in control._ns.worker.queued]
    assert kinds == ["OutputTransportMessageFrame", "TTSSpeakFrame", "EndWorkerFrame"]
    assert control.transfer_to == ("admissions", "+911234567890") and control.ended_by == "transfer"


def test_transfer_with_no_number_returns_false_and_the_call_goes_on():
    control = _control(FakeErp({"status": "none", "transfer_number": "none"}))
    assert asyncio.run(control.transfer("reception", "Connecting you now.")) is False
    assert control._ns.worker.queued == [] and control.transfer_to is None


def test_quiet_caller_gets_a_nudge_then_a_goodbye_and_speaking_resets_it():
    control = _control()
    asyncio.run(control._on_idle())
    assert control._ns.worker.queued[0].text == call_control.NUDGE["en"]
    asyncio.run(control._on_user_started())
    asyncio.run(control._on_idle())  # reset: a nudge again, not a goodbye
    assert [type(f).__name__ for f in control._ns.worker.queued] == ["TTSSpeakFrame", "TTSSpeakFrame"]
    asyncio.run(control._on_idle())
    last = control._ns.worker.queued[-2:]
    assert last[0].text == call_control.GOODBYE["en"] and isinstance(last[1], EndWorkerFrame)
    assert control.ended_by == "idle"


def test_tamil_caller_gets_tamil_nudges():
    control = _control()
    control._ns.router.language = "ta"
    asyncio.run(control._on_idle())
    assert control._ns.worker.queued[0].text == call_control.NUDGE["ta"]


def test_the_call_cap_ends_the_call():
    async def go():
        control = _control(max_secs=0.05)
        await control._cap()
        return control

    control = asyncio.run(go())
    assert control.ended_by == "time_limit" and isinstance(control._ns.worker.queued[-1], EndWorkerFrame)


def test_erp_down_gives_a_safe_message_not_an_exception(monkeypatch):
    monkeypatch.setenv("ERP_URL", "http://127.0.0.1:1")
    erp = ErpClient()
    assert asyncio.run(erp.identify("9000000001"))["role"] == "unknown"
    assert asyncio.run(erp.student("1", "2", "3", "marks", "")) == ERP_DOWN


def test_post_call_record_is_clamped_to_the_allowed_values():
    rec = post_call._clean({"admission_interest": "Maybe", "call_disposition": "Answered", "caller_type": "robot",
                            "lead_phone": "98765 43210", "call_summary": "x", "language_used": "TAMIL"})
    assert rec["admission_interest"] == "unclear" and rec["call_disposition"] == "answered"
    assert rec["caller_type"] == "other" and rec["lead_phone"] == "9876543210" and rec["language_used"] == "tamil"
