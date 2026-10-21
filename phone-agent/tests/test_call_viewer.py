"""The test page's live view (what is sent to the page during a call) and call history."""
import asyncio
import json
import os
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SARVAM_API_KEY", "t")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: F401
import call_history
import live_view
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
)


class Worker:
    def __init__(self):
        self.sent = []

    async def queue_frames(self, frames):
        self.sent += [f.message for f in frames if isinstance(f, OutputTransportMessageUrgentFrame)]


def _feed(view, *frames):
    async def run():
        for f in frames:
            await view.on_push_frame(SimpleNamespace(frame=f))
    asyncio.run(run())


def test_live_view_sends_words_tools_and_hides_identity_details():
    view, worker = live_view.LiveView("phone-test-x"), Worker()
    view.worker = worker
    _feed(view,
          TTSSpeakFrame("Welcome to Agni College."),
          TranscriptionFrame("my marks please", "u", "t", finalized=True),
          UserStoppedSpeakingFrame(),
          LLMFullResponseStartFrame(), LLMTextFrame("One "), LLMTextFrame("moment."),
          FunctionCallInProgressFrame(function_name="get_student_report", tool_call_id="c1",
                                      arguments={"reg_no": "312823104004", "date_of_birth": "12-04-2005", "topic": "marks"}),
          FunctionCallResultFrame(function_name="get_student_report", tool_call_id="c1", arguments={},
                                  result={"status": "ok", "speech": "For Abinaya."}),
          BotStartedSpeakingFrame(), InterruptionFrame())
    kinds = [m["type"] for m in worker.sent]
    assert kinds == ["bot", "user", "bot", "bot", "tool_call", "tool_result", "interrupted"]
    assert worker.sent[0]["text"] == "Welcome to Agni College." and not worker.sent[0]["append"]
    assert worker.sent[2]["rid"] == worker.sent[3]["rid"] != worker.sent[0]["rid"]   # one bubble for the reply
    assert worker.sent[4]["args"] == {"reg_no": "<hidden>", "date_of_birth": "<hidden>", "topic": "marks"}
    assert "312823104004" not in json.dumps(worker.sent)
    assert view.user_turns == 1 and len(view.response_times) == 1


def test_live_view_ignores_its_own_messages_and_repeats():
    view, worker = live_view.LiveView("phone-test-x"), Worker()
    view.worker = worker
    f = TranscriptionFrame("hello", "u", "t")
    _feed(view, f, f, OutputTransportMessageUrgentFrame(message={"type": "user"}))
    assert len(worker.sent) == 1
    # a tool call travels up and down as two frames: one card on the page (seen on a real call)
    call = dict(function_name="end_call", tool_call_id="c9", arguments={})
    _feed(view, FunctionCallInProgressFrame(**call), FunctionCallInProgressFrame(**call))
    assert [m["type"] for m in worker.sent].count("tool_call") == 1


def _call(logs, day, call_id, **files):
    d = logs / day / call_id
    d.mkdir(parents=True)
    for name, data in files.items():
        (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def _wav(path, seconds, channels):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\0\0" * channels * 8000 * seconds)


def test_call_history_lists_old_and_new_calls_newest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(call_history, "LOGS_DIR", tmp_path)
    old = _call(tmp_path, "2026-10-19", "phone-test-old1",
                transcript={"logged_at": "2026-10-19T10:00:30+00:00", "language": "en",
                            "messages": [{"role": "assistant", "content": "Hi"}, {"role": "user", "content": "x"}]})
    _wav(old / "audio.wav", 30, 1)
    new = _call(tmp_path, "2026-10-21", "phone-test-new1",
                transcript={"logged_at": "2026-10-21T10:00:00+00:00", "messages": []},
                call_record={"caller_role": "faculty", "call_disposition": "answered"})
    _wav(new / "audio.wav", 5, 2)
    _call(tmp_path, "2026-10-21", "web-session-1", transcript={"messages": []})   # not a phone call
    monkeypatch.setattr(call_history, "_session_dir", lambda cid: tmp_path / "2026-10-21" / cid)
    call_history.write_meta("phone-test-new1", started_at="2026-10-21T09:59:00+00:00", caller_number="9000000004",
                            role="faculty", ended_by="", transfer_to=("admissions", "+911234"), response_times=[1.0, 2.0],
                            user_turns=3)
    listed = call_history.list_calls(1, 20)
    assert listed["total"] == 2 and [c["id"] for c in listed["calls"]] == ["phone-test-new1", "phone-test-old1"]
    newest, oldest = listed["calls"]
    assert newest["caller"] == "XXXXXX0004" and newest["ended_by"] == "caller" and newest["transferred_to"] == "admissions"
    assert oldest["started_at"].startswith("2026-10-19T10:00:00") and oldest["duration_s"] == 30 and oldest["role"] == ""
    detail = call_history.call_detail("phone-test-new1")
    assert detail["audio"]["channels"] == 2 and detail["meta"]["avg_response_s"] == 1.5 and detail["record"]
    assert call_history.call_detail("../../etc") is None and call_history.audio_path("phone-test-nope") is None
