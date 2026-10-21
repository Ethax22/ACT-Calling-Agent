"""Outbound campaigns and the dialler: recipients, calling window, ringing, retries, DND."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

os.environ.setdefault("SARVAM_API_KEY", "t")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: F401
import campaigns as C
import dialler as D

IST = C.IST


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "campaigns.db")


CSV = ("Mobile,Name,Course,Source,Date\n"
       "+91 98765 43210,Ravi,B.E. CSE,chat,2026-10-04 12:30:00\n"
       "09876501234,Priya,B.E. ECE,phone,recently\n"
       "12345,Bad,,,\n"
       "9876543210,Dup,,,\n"
       ",NoPhone,,,\n")


def _campaign(**over):
    parsed = C.parse_csv(CSV)
    spec = {"name": "demo", "start_at": "now", "end_at": (datetime.now(IST) + timedelta(days=2)).isoformat(),
            "days": "1111111", "window_start": "00:00", "window_end": "23:59", "max_retries": 2,
            "retry_interval_min": 30, "retry_on": ["busy", "no_answer", "failed"],
            "cohort": {"name": "Outbound_Demo", "source": "csv", "file_name": "demo.csv",
                       "rows": parsed["rows"], "mapping": parsed["suggested"]}}
    spec.update(over)
    return C.create_campaign(spec)


def test_csv_columns_are_guessed_and_rows_validated():
    parsed = C.parse_csv(CSV)
    assert parsed["suggested"] == {"phone": "Mobile", "lead_name": "Name", "lead_course": "Course",
                                   "enquiry_source": "Source", "enquiry_date": "Date"}
    valid, invalid = C.build_recipients(parsed["rows"], parsed["suggested"])
    assert [v["phone"] for v in valid] == ["9876543210", "9876501234"]
    assert valid[0]["enquiry_source"] == "our website chat" and valid[0]["enquiry_date"] == "2026-10-04"
    assert [i["reason"].split(":")[0] for i in invalid] == ["not a valid mobile number", "duplicate number", "no phone number"]
    # the wizard's Required column: a row without a required field is left out
    valid, invalid = C.build_recipients(parsed["rows"], {**parsed["suggested"], "_required": ["lead_course"]})
    assert [v["lead_name"] for v in valid] == ["Ravi", "Priya"]
    valid, invalid = C.build_recipients([{"m": "9876543210", "n": ""}], {"phone": "m", "lead_name": "n", "_required": ["lead_name"]})
    assert not valid and invalid[0]["reason"] == "no lead name"


def test_campaign_needs_a_name_valid_numbers_and_a_sensible_schedule():
    with pytest.raises(ValueError):
        _campaign(name="")
    with pytest.raises(ValueError):
        _campaign(cohort={"rows": [{"p": "123"}], "mapping": {"phone": "p"}})
    with pytest.raises(ValueError):
        _campaign(window_start="18:00", window_end="09:00")
    camp = _campaign()
    assert camp["status"] == "scheduled" and camp["stats"]["audience"] == 2
    assert camp["cohorts"][0]["valid"] == 2 and camp["cohorts"][0]["invalid"] == 3


def test_calling_window_is_india_time_and_skips_sunday():
    camp = {"days": "1111110", "window_start": "09:00", "window_end": "19:00"}
    sat_10 = datetime(2026, 10, 24, 10, 0, tzinfo=IST)
    assert C.in_window(camp, sat_10) and C.in_window(camp, sat_10.astimezone(timezone.utc))
    assert not C.in_window(camp, datetime(2026, 10, 25, 10, 0, tzinfo=IST))          # Sunday
    assert not C.in_window(camp, datetime(2026, 10, 24, 19, 0, tzinfo=IST))          # 7 PM: closed
    assert not C.in_window(camp, datetime(2026, 10, 24, 8, 59, tzinfo=IST))


def test_rings_only_with_a_page_open_then_decline_retry_and_no_answer():
    camp = _campaign()
    d = D.Dialler()
    t = C.now_utc()
    d.tick(t)
    assert d.current is None                       # no test page open: nothing rings
    d.page_seen = t
    d.tick(t)
    assert d.current and d.current["lead"]["lead_name"] == "Ravi" and C.get_campaign(camp["id"])["status"] == "running"
    first = d.current["attempt_id"]
    assert d.decline(first)                        # declined = busy, retried in 30 min
    calls = {c["lead_name"]: c for c in C.campaign_calls(camp["id"])}
    assert calls["Ravi"]["status"] == "retry" and calls["Ravi"]["failure_reason"] == "declined"
    t2 = t + timedelta(seconds=5); d.page_seen = t2
    d.tick(t2)
    assert d.current["lead"]["lead_name"] == "Priya"     # Ravi's retry is not due yet
    d.tick(t2 + timedelta(seconds=D.RING_SECS + 1))      # Priya does not answer
    assert d.current is None
    assert {c["lead_name"]: c["status"] for c in C.campaign_calls(camp["id"])}["Priya"] == "retry"
    # Retries come round in the order they fall due; nobody answers. Max 2 retries = 3 attempts each.
    tn, rung = t, []
    for _ in range(10):
        tn = tn + timedelta(minutes=31); d.page_seen = tn
        d.tick(tn)
        if not d.current:
            break
        rung.append((d.current["lead"]["lead_name"], d.current["number"]))
        d.tick(tn + timedelta(seconds=D.RING_SECS + 1))
    assert sorted(rung) == [("Priya", 2), ("Priya", 3), ("Ravi", 2), ("Ravi", 3)]
    assert {c["lead_name"]: c["status"] for c in C.campaign_calls(camp["id"])} == {"Ravi": "no_answer", "Priya": "no_answer"}
    stats = C.get_campaign(camp["id"])["stats"]
    assert stats["attempts"] == 6 and stats["connected"] == 0 and stats["pickup_rate"] == 0
    d.tick(tn + timedelta(minutes=1))
    assert C.get_campaign(camp["id"])["status"] == "completed"   # every recipient done


def test_answered_call_completes_and_do_not_call_goes_to_the_dnd_list():
    camp = _campaign()
    d = D.Dialler(); d.page_seen = C.now_utc()
    d.tick()
    attempt = d.current["attempt_id"]
    lead = d.answer(attempt)
    assert lead["phone"] == "9876543210" and lead["campaign_name"] == "demo" and d.current["state"] == "in_call"
    assert d.answer(attempt) is None               # cannot answer twice
    d.call_finished(attempt, "phone-test-x", 42.0)
    d.record_outcome(attempt, {"call_outcome": "not_interested", "do_not_call": "yes"})
    ravi = {c["lead_name"]: c for c in C.campaign_calls(camp["id"])}["Ravi"]
    assert ravi["status"] == "completed" and ravi["outcome"] == "not_interested" and ravi["last_call_id"] == "phone-test-x"
    assert [x["phone"] for x in C.dnd_list()] == ["9876543210"]
    # another campaign with the same number skips it
    C.cancel_campaign(camp["id"])
    camp2 = _campaign(name="second")
    d.current = None; d.last_ring_end = None; d.page_seen = C.now_utc()
    d.tick()
    rows = {c["lead_name"]: c for c in C.campaign_calls(camp2["id"])}
    assert rows["Ravi"]["status"] == "skipped_dnd" and d.current["lead"]["lead_name"] in ("Priya",)


def test_short_call_retry_end_time_and_cancel():
    camp = _campaign(retry_on=["busy", "short_duration"])
    d = D.Dialler(); d.page_seen = C.now_utc()
    d.tick()
    a = d.current["attempt_id"]; d.answer(a); d.call_finished(a, "phone-test-y", 3.0)
    assert {c["lead_name"]: c["status"] for c in C.campaign_calls(camp["id"])}["Ravi"] == "retry"
    d.tick()
    assert d.current is None                       # 0.5 calls per second: 2 s between calls
    soon = C.now_utc() + timedelta(seconds=3); d.page_seen = soon
    d.tick(soon)
    assert d.current["lead"]["lead_name"] == "Priya"
    C.cancel_campaign(camp["id"])
    d.finish(d.current["attempt_id"], "cancelled", "campaign cancelled")
    assert C.get_campaign(camp["id"])["status"] == "cancelled"
    with pytest.raises(ValueError):
        C.cancel_campaign(camp["id"])
    late = _campaign(name="late", end_at=(datetime.now(IST) + timedelta(minutes=1)).isoformat())
    d.page_seen = C.now_utc() + timedelta(minutes=2)
    d.tick(C.now_utc() + timedelta(minutes=2))
    assert C.get_campaign(late["id"])["status"] == "completed"


def test_stop_list_and_restart_recovery():
    camp = _campaign()
    assert C.stop_add(camp["id"], ["98765 43210", "bad"]) == 1
    d = D.Dialler(); d.page_seen = C.now_utc()
    d.tick()
    assert d.current["lead"]["lead_name"] == "Priya"
    assert {c["lead_name"]: c["status"] for c in C.campaign_calls(camp["id"])}["Ravi"] == "stopped"
    D.Dialler().recover()                          # the agent restarted mid-ring
    priya = {c["lead_name"]: c for c in C.campaign_calls(camp["id"])}["Priya"]
    assert priya["status"] == "retry"
