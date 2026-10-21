"""Agent Analytics: each definition checked on a small known set of calls and attempts."""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("SARVAM_API_KEY", "t")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: F401
import analytics as A
import call_history as H
import campaigns as C


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "campaigns.db")
    return tmp_path / "logs"


def _call(logs, cid, *, start, secs, direction="inbound", turns=2, ended_by="caller", record=None, latency=(),
          tools=(), campaign=None, language="en"):
    d = logs / start.strftime("%Y-%m-%d") / cid
    d.mkdir(parents=True)
    meta = {"started_at": start.isoformat(), "ended_at": (start + timedelta(seconds=secs)).isoformat(), "duration_s": secs,
            "caller": "XXXXXX0001", "role": "lead" if direction == "outbound" else "unknown", "ended_by": ended_by,
            "direction": direction, "user_turns": turns, "response_times_s": list(latency), "tool_runs": list(tools)}
    if campaign:
        meta["campaign"] = campaign
    (d / "call_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "transcript.json").write_text(json.dumps({"language": language, "messages": []}), encoding="utf-8")
    if record:
        (d / "call_record.json").write_text(json.dumps(record), encoding="utf-8")


def test_inbound_numbers(world):
    t = datetime(2026, 10, 21, 10, 0, tzinfo=C.IST)
    _call(world, "phone-test-a", start=t, secs=60, turns=3, record={"call_disposition": "answered"}, latency=(1.0, 2.0),
          tools=[{"name": "get_student_report", "status": "ok", "ms": 300}, {"name": "end_call", "status": "ok", "ms": 5}])
    _call(world, "phone-test-b", start=t + timedelta(hours=2), secs=5, turns=1, ended_by="end_call",
          record={"call_disposition": "unanswered_question"}, latency=(3.0,),
          tools=[{"name": "get_student_report", "status": "error", "ms": 900}])
    _call(world, "phone-test-c", start=t + timedelta(days=1), secs=40, turns=0)          # old call: no record
    _call(world, "phone-test-o", start=t, secs=30, direction="outbound")                 # other agent
    r = A.report("inbound", "2026-10-21", "2026-10-22")
    o = r["overview"]
    assert (o["attempted"], o["connected"], o["short_calls"], o["latency_ms"]) == (3, 3, 1, 2000)
    assert o["total_minutes"] == round(105 / 60, 1) and o["outcomes"]["no record"] == 1
    e = r["engagement"]
    assert e["engaged"] == 1 and e["end_reasons"] == {"USER_ENDS": 2, "AGENT_ENDS": 1}
    assert [f["n"] for f in e["funnel"]] == [3, 3, 1, 1]
    assert all(a["n"] >= b["n"] for a, b in zip(e["funnel"], e["funnel"][1:]))   # a funnel only narrows
    tl = r["tools"]
    assert (tl["calls"], tl["unique"], tl["errors"], tl["median_ms"]) == (3, 2, 1, 300)
    assert tl["table"][0]["name"] == "get_student_report" and tl["table"][0]["errors"] == 1
    g = r["goals"]
    assert (g["eligible"], g["achieved"], g["rate"]) == (2, 1, 0.5)
    assert g["achieved_stats"]["avg_turns"] == 3 and g["not_achieved_stats"]["avg_turns"] == 1
    assert [s["date"] for s in o["series"]] == ["2026-10-21", "2026-10-22"]
    assert A.report("inbound", "2026-10-22", "2026-10-22")["overview"]["attempted"] == 1   # date filter


def test_outbound_counts_unanswered_attempts_and_the_visit_goal(world):
    camp = C.create_campaign({"name": "c1", "days": "1111111", "window_start": "00:00", "window_end": "23:59",
                              "cohort": {"rows": [{"p": "9876500001"}, {"p": "9876500002"}], "mapping": {"phone": "p"}}})
    with C.db() as con:
        rids = [r["id"] for r in con.execute("SELECT id FROM recipients ORDER BY seq")]
        now = C.now_utc()
        for i, (rid, n, result, call_id) in enumerate([(rids[0], 1, "busy", None), (rids[0], 2, "connected", "phone-test-v"),
                                                        (rids[1], 1, "no_answer", None)]):
            con.execute("INSERT INTO attempts(id, campaign_id, recipient_id, number, started_at, result, failure_reason, call_id, duration_s)"
                        " VALUES(?,?,?,?,?,?,?,?,?)", (f"att{i}", camp["id"], rid, n, C.iso(now + timedelta(seconds=i)), result,
                                                       "declined" if result == "busy" else None, call_id, 50 if call_id else None))
    _call(world, "phone-test-v", start=now.astimezone(C.IST), secs=50, direction="outbound", turns=4,
          record={"call_outcome": "visit_booked", "language_used": "english"}, campaign={"id": camp["id"], "name": "c1"})
    _call(world, "phone-test-m", start=now.astimezone(C.IST), secs=20, direction="outbound", turns=1,
          record={"call_outcome": "not_interested"})                                     # a manual outbound test call
    r = A.report("outbound")
    assert r["overview"]["attempted"] == 4 and r["overview"]["connected"] == 2
    assert r["connectivity"]["results"] == {"connected": 2, "busy": 1, "no_answer": 1}
    assert r["overview"]["failure_reasons"] == {"declined": 1, "no_answer": 1}
    camp_row = r["connectivity"]["campaigns"][0]
    assert (camp_row["attempted"], camp_row["connected"], camp_row["unique_recipients"], camp_row["unique_connects"]) == (3, 1, 2, 1)
    assert r["engagement"]["by_attempt"] == [{"attempt": 2, "calls": 1, "engaged": 1, "rate": 1.0}]
    assert r["engagement"]["avg_attempts_to_engage"] == 2.0
    assert (r["goals"]["eligible"], r["goals"]["achieved"]) == (2, 1)
    assert {x["kind"] for x in r["calls"]} == {"call", "attempt"} and len(r["calls"]) == 4
    only = A.report("outbound", campaign=camp["id"])
    assert only["overview"]["attempted"] == 3 and only["overview"]["connected"] == 1


def test_sentiment_counts_only_phone_prompt_judgements(world):
    t = datetime(2026, 10, 21, 10, 0, tzinfo=C.IST)
    judged = {"sentiment": "negative", "trend": "worsened", "admission_interest": "none", "frustrated": True,
              "frustration_reason": "check failed", "unanswered": ["What is my attendance?"], "topics": ["records"],
              "summary": "x"}
    for cid, prompt in (("phone-test-s1", "phone1"), ("phone-test-s2", "website3"), ("phone-test-s3", None)):
        _call(world, cid, start=t, secs=30, turns=2)
        if prompt:
            (world / "2026-10-21" / cid / "sentiment.json").write_text(
                json.dumps({"key": "k", "prompt": prompt, "result": judged}), encoding="utf-8")
    s = A.report("inbound", "2026-10-21", "2026-10-21")["sentiment"]
    assert (s["calls"], s["analysed"], s["not_analysed"]) == (3, 1, 2)   # the website-prompt one is not counted
    assert s["sentiment"]["negative"] == 1 and s["frustrated"] == 1
    assert s["questions"][0]["call_ids"] == ["phone-test-s1"] and s["flagged"][0]["id"] == "phone-test-s1"
