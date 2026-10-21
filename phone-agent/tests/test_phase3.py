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
    p.context = SimpleNamespace(messages=[{"role": "user", "content": "312823104004 and 12-04-2005"}])
    asyncio.run(handlers["get_student_report"](p))
    assert got["result"]["speech"].startswith("For Abinaya")
    assert erp.calls[0][1][0] == "9000000001" and erp.calls[0][1][1] == "312823104004"
    p, got = _params({"faculty_id": "FAC1001", "topic": "low_attendance"})
    p.context = SimpleNamespace(messages=[{"role": "user", "content": "my faculty ID is FAC 1001"}])
    asyncio.run(handlers["get_class_summary"](p))
    assert "75 percent" in got["result"]["speech"]
    assert erp.calls[1][1][:3] == ("9000000001", "FAC1001", "low_attendance")


def _user(*said):
    return SimpleNamespace(messages=[{"role": "user", "content": s} for s in said])


def test_the_class_tool_refuses_a_faculty_id_the_caller_never_said():
    erp = FakeErp()
    handlers = tools.make_handlers(erp, _control(), "9000000004")
    p, got = _params({"faculty_id": "FAC1001", "topic": "low_attendance"})
    p.context = _user("I want my class attendance")
    asyncio.run(handlers["get_class_summary"](p))
    assert got["result"]["status"] == "need_details" and not erp.calls


def test_a_faculty_id_spoken_as_words_counts_as_said():
    assert tools.faculty_id_said(_user("it is F A C one zero zero one"), "FAC1001")
    assert tools.faculty_id_said(_user("fac-1001"), "fac 1001")
    assert not tools.faculty_id_said(_user("it is F A C one zero zero two"), "FAC1001")


def test_the_verified_faculty_id_is_reused_when_the_caller_picks_a_class():
    class ListingErp(FakeErp):
        async def class_summary(self, *a):
            self.calls.append(("class", a))
            return {"status": "choose_class", "speech": "You are in charge of 2 classes: ..."}

    erp = ListingErp()
    handlers = tools.make_handlers(erp, _control(), "9000000004")
    p, got = _params({"faculty_id": "FAC1001", "topic": "low_attendance"})
    p.context = _user("FAC1001")
    asyncio.run(handlers["get_class_summary"](p))
    p, got = _params({"topic": "low_attendance", "class_name": "CSE 2023"})  # model dropped the ID
    p.context = _user("FAC1001", "the CSE one")
    asyncio.run(handlers["get_class_summary"](p))
    assert erp.calls[1][1] == ("9000000004", "FAC1001", "low_attendance", "CSE 2023")


def test_the_callers_own_words_name_the_class_when_the_model_leaves_it_out():
    erp = FakeErp()
    handlers = tools.make_handlers(erp, _control(), "9000000004")
    p, got = _params({"faculty_id": "FAC1001", "topic": "marks_summary"})
    p.context = _user("the latest result for my ECE class", "FAC 1001", "yes")
    asyncio.run(handlers["get_class_summary"](p))
    assert "ECE" in erp.calls[0][1][3]
    # after an answer, only the words since then count: "my other class" must not reuse "CSE"
    ctx = SimpleNamespace(messages=[{"role": "user", "content": "CSE attendance"}, {"role": "tool", "content": "{}"},
                                    {"role": "user", "content": "marks for my other class"}])
    assert tools.caller_words_since_last_lookup(ctx) == "marks for my other class"


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


def test_post_call_caller_type_comes_from_the_identified_role(monkeypatch, tmp_path):
    t = tmp_path / "transcript.json"
    t.write_text(json.dumps({"messages": [{"role": "user", "content": "class summary"}]}), encoding="utf-8")
    monkeypatch.setattr(post_call, "_transcript_path", lambda s: t)
    monkeypatch.setattr(post_call, "_append_sheet", lambda row: False)

    async def extract(messages, prompt=None):
        return {"caller_type": "current_student", "call_disposition": "answered"}

    monkeypatch.setattr(post_call, "_extract", extract)
    rec = asyncio.run(post_call.record_call("phone-test-x", "faculty", "9000000004", "transfer", None))
    assert rec["caller_type"] == "faculty"
    rec = asyncio.run(post_call.record_call("phone-test-x", "unknown", "9111111111", "caller", None))
    assert rec["caller_type"] == "current_student"   # unknown callers: the model's reading stands


def test_background_sound_loops_cleanly_and_stays_quiet(monkeypatch, tmp_path):
    import numpy as np

    import background_sound as bg

    sound = bg.make_office_murmur(seconds=4)
    assert len(sound) == 4 * bg.RATE and np.max(np.abs(sound)) <= 0.5001
    assert abs(float(sound[-1]) - float(sound[0])) < 0.05  # no click where it loops
    assert 0.02 < float(np.sqrt(np.mean(sound ** 2))) < 0.3

    monkeypatch.setattr(bg, "CACHE", tmp_path / "office.wav")
    monkeypatch.delenv("PHONE_BACKGROUND_FILE", raising=False)
    monkeypatch.setenv("PHONE_BACKGROUND", "off")
    assert bg.make_mixer() is None
    monkeypatch.setenv("PHONE_BACKGROUND", "quiet_office")
    mixer = bg.make_mixer()
    assert mixer is not None and (tmp_path / "office.wav").exists()


def test_the_records_tool_refuses_a_date_of_birth_the_caller_never_said():
    ctx = lambda *texts: SimpleNamespace(messages=[{"role": "user", "content": t} for t in texts])
    assert tools.missing_record_detail(ctx("tell me my attendance")) == "register number"
    assert tools.missing_record_detail(ctx("my register number is 312823104004")) == "date of birth"
    assert tools.missing_record_detail(ctx("312823104004", "April 12, 2005")) is None
    assert tools.missing_record_detail(ctx("312823104004", "ஏப்ரல் 12, 2005")) is None
    assert tools.missing_record_detail(ctx("312823104004", "12-04-2005")) is None

    erp, control = FakeErp(), _control()
    handlers = tools.make_handlers(erp, control, "9000000001")
    p, got = _params({"reg_no": "312823104004", "date_of_birth": "01-01-2000", "topic": "attendance"})
    p.context = ctx("my register number is 312823104004")
    asyncio.run(handlers["get_student_report"](p))
    assert got["result"]["status"] == "need_details" and erp.calls == []   # the ERP was never asked


def test_the_date_of_birth_is_read_from_the_callers_words_day_first():
    ctx = lambda *texts: SimpleNamespace(messages=[{"role": "user", "content": t} for t in texts])
    said = tools.date_the_caller_said
    assert said(ctx("என்னோட டேட் ஆஃப் பர்த் வந்து ஏப்ரல் 12, 2005.")) == "12-04-2005"   # the failed real call
    assert said(ctx("April 12, 2005")) == "12-04-2005"
    assert said(ctx("12th April 2005")) == "12-04-2005"
    assert said(ctx("12 ஏப்ரல் 2005")) == "12-04-2005"
    assert said(ctx("1st January 2007")) == "01-01-2007"
    assert said(ctx("12-04-2005")) == "12-04-2005" and said(ctx("12/4/2005")) == "12-04-2005"
    assert said(ctx("my number is 312823104004")) is None
    assert said(ctx("12 April 2005", "no wait, 3 March 2004")) == "03-03-2004"           # the latest date wins


def test_the_handler_sends_the_callers_date_even_when_the_model_swapped_day_and_month():
    erp, control = FakeErp(), _control()
    handlers = tools.make_handlers(erp, control, "9000000001")
    p, got = _params({"reg_no": "312823104004", "date_of_birth": "04-12-2005", "topic": "attendance"})
    p.context = SimpleNamespace(messages=[{"role": "user", "content": "312823104004"},
                                          {"role": "user", "content": "ஏப்ரல் 12, 2005"}])
    asyncio.run(handlers["get_student_report"](p))
    assert erp.calls[0][1][2] == "12-04-2005"


def test_numbers_spoken_as_words_count_as_said():
    """The failed call of 2026-10-20: English-mode recognition wrote the register number and the
    date of birth as words, and the guard refused the tool because it saw no digits."""
    ctx = lambda *texts: SimpleNamespace(messages=[{"role": "user", "content": t} for t in texts])
    said = ctx("My register number is three one two eight two three one zero four double zero four.",
               "My date of birth is twelve April two thousand five.")
    assert tools.missing_record_detail(said) is None
    assert tools.date_the_caller_said(said) == "12-04-2005"
    assert tools.date_the_caller_said(ctx("first January two thousand seven")) == "01-01-2007"
    assert tools.date_the_caller_said(ctx("twenty first of May two thousand and six")) == "21-05-2006"
    assert tools.missing_record_detail(ctx("one moment please")) == "register number"
