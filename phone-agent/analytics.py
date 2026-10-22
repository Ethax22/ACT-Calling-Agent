"""Agent Analytics: the numbers behind the test page's Analytics tab, for the inbound agent
(people calling the college) or the outbound agent (the college calling leads back).

Modelled on the Indus Agent Analytics page (Prakash's recording, 2026-10-21): Overview,
Connectivity, Engagement, Tools, Goals, Group by and Call logs. Everything is computed on
request from what is already saved: each call's folder (call_meta.json, call_record.json,
transcript.json; see call_history.py) and, for outbound, the campaign attempts in
campaigns.db (attempts that were never answered have no call folder).

Definitions (kept here so the page and this file agree):
- connected: a call that was answered (every inbound call; an outbound attempt with result
  "connected", or a manual outbound test call).
- engaged: a connected call where the caller spoke at least twice.
- goal: outbound = call_outcome "visit_booked" (as on Indus); inbound = the question was
  answered or a lead was captured (call_disposition "answered" or "lead_captured").
  Eligible = connected calls that have a call record.
- latency: the time from the caller stopping speaking to the bot starting to speak, per
  turn (recorded since 2026-10-21; older calls have none).
- short call: connected and under 10 seconds.
- agent ends: the call was ended from our side (end_call, idle, time limit, voicemail,
  transfer); user ends: the caller hung up.
- sentiment: Sarvam 105B's reading of the caller's side (sentiment.json beside each call,
  written right after the call or by the report; rag-backend/sentiment_report.py). Not
  checked against hand-labelled calls.
"""
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

import call_history as H
import campaigns as C
import sentiment

SHORT_CALL_SECS = 10
AGENT_ENDS = {"end_call", "idle", "time_limit", "voicemail", "transfer"}
TOOL_ERRORS = {"error", "internal_error"}
INBOUND_GOAL = {"answered", "lead_captured"}


def _ist_date(dt: datetime) -> date:
    return dt.astimezone(C.IST).date()


def _median(xs):
    return round(statistics.median(xs)) if xs else None


def _p95(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))])


def _avg(xs, nd=1):
    return round(sum(xs) / len(xs), nd) if xs else None


def _rate(a, b):
    return round(a / b, 4) if b else None


def _bucket(value, edges, labels):
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


TURN_EDGES, TURN_LABELS = [1, 3, 6, 11, 21, 10 ** 9], ["0", "1-2", "3-5", "6-10", "11-20", "21+"]
DUR_EDGES, DUR_LABELS = [10, 30, 60, 120, 300, 10 ** 9], ["<10s", "10-30s", "30s-1m", "1-2m", "2-5m", "5m+"]


# ---------------------------------------------------------------- reading calls
def _tools_from_transcript(messages: list) -> list[dict]:
    """Tool calls of an older call (before tool_runs were saved): names and result status, no latency."""
    status = {}
    for m in messages:
        if m.get("role") == "tool":
            try:
                status[m.get("tool_call_id")] = (json.loads(m.get("content") or "{}") or {}).get("status", "ok")
            except (json.JSONDecodeError, AttributeError):
                status[m.get("tool_call_id")] = "ok"
    runs = []
    for m in messages:
        for c in m.get("tool_calls") or []:
            runs.append({"name": (c.get("function") or {}).get("name", "?"), "status": status.get(c.get("id"), "ok"), "ms": None})
    return runs


def load_calls(direction: str) -> list[dict]:
    """One compact row per saved phone call in that direction."""
    out = []
    if not H.LOGS_DIR.exists():
        return out
    for folder in H.LOGS_DIR.glob("*/phone-*"):
        if not folder.is_dir():
            continue
        s = H._summary(folder)
        if s["direction"] != direction:
            continue
        meta = H._json(folder / "call_meta.json") or {}
        record = H._json(folder / "call_record.json") or {}
        messages = (H._json(folder / "transcript.json") or {}).get("messages") or []
        turns = meta.get("user_turns")
        if turns is None:
            turns = sum(1 for m in messages if m.get("role") == "user" and (m.get("content") or "").strip())
        if direction == "outbound":
            outcome = record.get("call_outcome", "")
            goal = (outcome == "visit_booked") if record else None
        else:
            outcome = record.get("call_disposition", "")
            goal = (outcome in INBOUND_GOAL) if record else None
        language = record.get("language_used") or {"en": "english", "ta": "tamil", "ta_en": "tanglish"}.get(s["language"], s["language"] or "unknown")
        out.append({
            "id": s["id"], "started_at": s["started_at"], "started": H._parse(s["started_at"]),
            "duration_s": s["duration_s"] or 0, "user_turns": turns, "ended_by": s["ended_by"],
            "language": language or "unknown", "role": s["role"] or "unknown", "outcome": outcome, "goal": goal,
            "has_record": bool(record), "caller": s["caller"], "lead_name": s.get("lead_name", ""),
            "campaign_id": s.get("campaign_id", ""), "campaign_name": s.get("campaign_name", ""),
            "latency_ms": [round(x * 1000) for x in (meta.get("response_times_s") or [])],
            "tools": meta.get("tool_runs") if meta.get("tool_runs") is not None else _tools_from_transcript(messages),
            "do_not_call": record.get("do_not_call", ""), "summary": record.get("call_summary", ""),
            "sentiment": sentiment.judged(folder),
        })
    return out


# ---------------------------------------------------------------- the report
def load_website_conversations() -> list[dict]:
    """One row per saved website conversation (typed chat and voice), shaped like load_calls()
    so the Sentiment tab can use the same code. Judged ones carry their cached judgement."""
    m = sentiment.module()
    out = []
    for conv in m._load_conversations(date(2000, 1, 1), datetime.now(timezone.utc).date() + timedelta(days=1), "website"):
        folder = conv["path"].parent
        started = datetime.fromtimestamp(conv["path"].stat().st_mtime, tz=timezone.utc)
        out.append({"id": conv["session_id"], "started": started, "started_at": started.isoformat(),
                    "user_turns": conv["user_turns"], "language": conv["language"] or "unknown",
                    "role": conv["channel"], "caller": "", "lead_name": "",
                    "sentiment": sentiment.judged(folder, website=True)})
    return out


def website_report(date_from: str | None, date_to: str | None, language: str = "", role: str = "") -> dict:
    """Agent Analytics for the website bot: only the Sentiment tab has data (chat and voice)."""
    all_convs = load_website_conversations()
    today = datetime.now(C.IST).date()
    d_to = date.fromisoformat(date_to) if date_to else today
    if date_from:
        d_from = date.fromisoformat(date_from)
    else:
        firsts = [_ist_date(c["started"]) for c in all_convs]
        d_from = min(firsts) if firsts else today - timedelta(days=6)
    d_from = max(d_from, d_to - timedelta(days=365))
    convs = [c for c in all_convs if d_from <= _ist_date(c["started"]) <= d_to
             and (not language or c["language"] == language) and (not role or c["role"] == role)]
    days = [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]
    return {"agent": "website", "from": d_from.isoformat(), "to": d_to.isoformat(),
            "filters": {"campaigns": [], "languages": sorted({c["language"] for c in all_convs}),
                        "roles": sorted({c["role"] for c in all_convs})},
            "sentiment": _sentiment(convs, days), "calls": []}


def report(direction: str = "inbound", date_from: str | None = None, date_to: str | None = None,
           campaign: str = "", language: str = "", role: str = "") -> dict:
    if direction == "website":
        return website_report(date_from, date_to, language, role)
    direction = "outbound" if direction == "outbound" else "inbound"
    all_calls = load_calls(direction)
    attempts = C.all_attempts() if direction == "outbound" else []

    # filter choices come from everything, before filtering
    choices = {
        "campaigns": sorted({(a["campaign_id"], a["campaign_name"] or a["campaign_id"]) for a in attempts if a["campaign_id"]},
                            key=lambda x: x[1]),
        "languages": sorted({c["language"] for c in all_calls}),
        "roles": sorted({c["role"] for c in all_calls}),
    }
    today = datetime.now(C.IST).date()
    d_to = date.fromisoformat(date_to) if date_to else today
    if date_from:
        d_from = date.fromisoformat(date_from)
    else:
        firsts = [_ist_date(c["started"]) for c in all_calls if c["started"]] + \
                 [_ist_date(C.parse_time(a["started_at"])) for a in attempts if a["started_at"]]
        d_from = min(firsts) if firsts else today - timedelta(days=6)
    d_from = max(d_from, d_to - timedelta(days=365))

    def in_range(dt):
        return dt is not None and d_from <= _ist_date(dt) <= d_to

    calls = [c for c in all_calls if in_range(c["started"])
             and (not campaign or c["campaign_id"] == campaign)
             and (not language or c["language"] == language) and (not role or c["role"] == role)]
    if direction == "outbound":
        attempts = [a for a in attempts if in_range(C.parse_time(a["started_at"])) and (not campaign or a["campaign_id"] == campaign)]
        if language or role:  # unanswered attempts have no language/role: keep only those whose call matches
            ids = {c["id"] for c in calls}
            attempts = [a for a in attempts if a["call_id"] in ids]
    days = [(d_from + timedelta(days=i)) for i in range((d_to - d_from).days + 1)]

    # ---- attempts and connections
    if direction == "outbound":
        att_ids = {a["call_id"] for a in attempts if a["call_id"]}
        manual = [c for c in calls if c["id"] not in att_ids]          # outbound test calls without a campaign
        results = Counter(a["result"] or "in progress" for a in attempts) + Counter({"connected": len(manual)})
        attempted = len(attempts) + len(manual)
        failure_reasons = Counter((a["failure_reason"] or a["result"]) for a in attempts
                                  if a["result"] and a["result"] != "connected")
        attempt_rows = [{"started": C.parse_time(a["started_at"]), "connected": a["result"] == "connected"} for a in attempts] + \
                       [{"started": c["started"], "connected": True} for c in manual]
    else:
        results = Counter({"connected": len(calls)})
        attempted = len(calls)
        failure_reasons = Counter()
        attempt_rows = [{"started": c["started"], "connected": True} for c in calls]
    connected = results.get("connected", 0)

    lat_all = [x for c in calls for x in c["latency_ms"]]
    durations = [c["duration_s"] for c in calls]
    by_day = defaultdict(lambda: {"attempted": 0, "connected": 0, "engaged": 0, "minutes": 0.0, "tool_calls": 0, "tool_errors": 0})
    for a in attempt_rows:
        if a["started"]:
            d = by_day[_ist_date(a["started"]).isoformat()]
            d["attempted"] += 1
            d["connected"] += a["connected"]
    for c in calls:
        d = by_day[_ist_date(c["started"]).isoformat()]
        d["engaged"] += c["user_turns"] >= 2
        d["minutes"] += c["duration_s"] / 60
        d["tool_calls"] += len(c["tools"])
        d["tool_errors"] += sum(1 for t in c["tools"] if t["status"] in TOOL_ERRORS)
    series = [{"date": d.isoformat(), **{k: round(v, 1) if isinstance(v, float) else v for k, v in by_day[d.isoformat()].items()}}
              for d in days]

    outcomes = Counter(c["outcome"] or "no record" for c in calls)
    overview = {
        "attempted": attempted, "connected": connected, "latency_ms": _median(lat_all),
        "avg_duration_s": _avg(durations), "total_minutes": round(sum(durations) / 60, 1),
        "short_calls": sum(1 for c in calls if c["duration_s"] < SHORT_CALL_SECS),
        "series": series, "outcomes": dict(outcomes.most_common()), "failure_reasons": dict(failure_reasons.most_common(8)),
    }

    # ---- connectivity
    hours = defaultdict(lambda: {"attempted": 0, "connected": 0})
    for a in attempt_rows:
        if a["started"]:
            h = hours[a["started"].astimezone(C.IST).hour]
            h["attempted"] += 1
            h["connected"] += a["connected"]
    uniq_recipients = len({a["recipient_id"] for a in attempts}) + (len(manual) if direction == "outbound" else len({c["caller"] or c["id"] for c in calls}))
    uniq_connects = len({a["recipient_id"] for a in attempts if a["result"] == "connected"}) + (len(manual) if direction == "outbound" else uniq_recipients)
    agent_row = {"agent": "Agni Outbound agent" if direction == "outbound" else "Agni Inbound agent", "volume": attempted,
                 "connected": connected, "connectivity": _rate(connected, attempted), "unique_recipients": uniq_recipients,
                 "unique_connects": uniq_connects, "avg_duration_s": _avg(durations)}
    camp_rows = []
    if direction == "outbound":
        by_camp = defaultdict(list)
        for a in attempts:
            by_camp[(a["campaign_id"], a["campaign_name"])].append(a)
        for (cid, name), rows in sorted(by_camp.items(), key=lambda x: x[0][1] or ""):
            conn = [a for a in rows if a["result"] == "connected"]
            camp_rows.append({"campaign_id": cid, "campaign": name, "attempted": len(rows), "connected": len(conn),
                              "connectivity": _rate(len(conn), len(rows)), "unique_recipients": len({a["recipient_id"] for a in rows}),
                              "unique_connects": len({a["recipient_id"] for a in conn}),
                              "avg_duration_s": _avg([a["duration_s"] for a in conn if a["duration_s"] is not None])})
    connectivity = {
        "results": dict(results.most_common()), "rate": _rate(connected, attempted),
        "by_hour": [{"hour": h, **hours[h]} for h in range(24)],
        "agent": agent_row, "campaigns": camp_rows, "failure_reasons": dict(failure_reasons.most_common(8)),
    }

    # ---- engagement
    engaged = [c for c in calls if c["user_turns"] >= 2]
    ends = Counter("AGENT_ENDS" if c["ended_by"] in AGENT_ENDS else "USER_ENDS" if c["ended_by"] == "caller" else "UNKNOWN"
                   for c in calls)
    eh = defaultdict(lambda: {"calls": 0, "engaged": 0})
    for c in calls:
        h = eh[c["started"].astimezone(C.IST).hour]
        h["calls"] += 1
        h["engaged"] += c["user_turns"] >= 2
    by_attempt = []
    if direction == "outbound":
        call_by_id = {c["id"]: c for c in calls}
        ba = defaultdict(lambda: {"calls": 0, "engaged": 0})
        for a in attempts:
            if a["result"] == "connected" and a["call_id"] in call_by_id:
                b = ba[a["number"] or 1]
                b["calls"] += 1
                b["engaged"] += call_by_id[a["call_id"]]["user_turns"] >= 2
        by_attempt = [{"attempt": k, **v, "rate": _rate(v["engaged"], v["calls"])} for k, v in sorted(ba.items())]
        eng_ids = {c["id"] for c in engaged}
        per_rcp = defaultdict(int)
        engaged_rcp = {}
        for a in sorted(attempts, key=lambda a: a["started_at"]):
            per_rcp[a["recipient_id"]] += 1
            if a["call_id"] in eng_ids and a["recipient_id"] not in engaged_rcp:
                engaged_rcp[a["recipient_id"]] = per_rcp[a["recipient_id"]]
        attempts_to_engage = _avg(list(engaged_rcp.values()), 2)
    else:
        attempts_to_engage = 1.0 if engaged else None
    # A funnel narrows at every step: the last step counts engaged calls that reached the goal (a
    # one-question inbound call can reach the goal without being "engaged").
    goal_n = sum(1 for c in engaged if c["goal"])
    engagement = {
        "engaged": len(engaged), "connected": connected, "avg_duration_s": _avg(durations),
        "avg_attempts_to_engage": attempts_to_engage, "latency_ms": _median(lat_all), "series": series,
        "end_reasons": dict(ends.most_common()),
        "by_hour": [{"hour": h, **eh[h], "rate": _rate(eh[h]["engaged"], eh[h]["calls"])} for h in range(24)],
        "by_attempt": by_attempt,
        "turns": [{"bucket": b, "n": n} for b, n in _hist([c["user_turns"] for c in calls], TURN_EDGES, TURN_LABELS)],
        "durations": [{"bucket": b, "n": n} for b, n in _hist(durations, DUR_EDGES, DUR_LABELS)],
        "funnel": [{"stage": "Attempted", "n": attempted}, {"stage": "Connected", "n": connected},
                   {"stage": "Engaged", "n": len(engaged)}, {"stage": "Engaged and goal achieved", "n": goal_n}],
    }

    # ---- tools
    runs = [t for c in calls for t in c["tools"]]
    timed = [t["ms"] for t in runs if t.get("ms") is not None]
    per_tool = defaultdict(list)
    for t in runs:
        per_tool[t["name"]].append(t)
    tools = {
        "calls": len(runs), "unique": len(per_tool), "errors": sum(1 for t in runs if t["status"] in TOOL_ERRORS),
        "median_ms": _median(timed), "p95_ms": _p95(timed), "timed_calls": len(timed), "series": series,
        "table": sorted(({"name": n, "calls": len(ts), "errors": sum(1 for t in ts if t["status"] in TOOL_ERRORS),
                          "statuses": dict(Counter(t["status"] for t in ts).most_common()),
                          "median_ms": _median([t["ms"] for t in ts if t.get("ms") is not None]),
                          "p95_ms": _p95([t["ms"] for t in ts if t.get("ms") is not None])} for n, ts in per_tool.items()),
                        key=lambda r: -r["calls"]),
    }
    tools["error_rate"] = _rate(tools["errors"], tools["calls"])

    # ---- goals
    eligible = [c for c in calls if c["goal"] is not None]
    yes = [c for c in eligible if c["goal"]]
    no = [c for c in eligible if not c["goal"]]

    def stats(cs):
        return {"calls": len(cs), "avg_duration_s": _avg([c["duration_s"] for c in cs]),
                "avg_turns": _avg([c["user_turns"] for c in cs]),
                "avg_latency_ms": _median([x for c in cs for x in c["latency_ms"]])}

    gh = defaultdict(lambda: {"eligible": 0, "achieved": 0})
    for c in eligible:
        h = gh[c["started"].astimezone(C.IST).hour]
        h["eligible"] += 1
        h["achieved"] += bool(c["goal"])
    voice = os.environ.get("CARTESIA_VOICE_ID", "").strip()
    intro = ("Hello {lead_name}, this is Agni AI calling from Agni College of Technology. This call may be recorded. "
             "You recently enquired about {lead_course}. Is this a good time to talk for a minute?") if direction == "outbound" else \
        __import__("prompts").GREETING
    goals = {
        "goal": "Visit booked (call_outcome = visit_booked)" if direction == "outbound"
        else "Question answered or lead captured (call_disposition = answered or lead_captured)",
        "eligible": len(eligible), "achieved": len(yes), "rate": _rate(len(yes), len(eligible)),
        "avg_turns_to_goal": _avg([c["user_turns"] for c in yes]), "avg_turns_not": _avg([c["user_turns"] for c in no]),
        "achieved_stats": stats(yes), "not_achieved_stats": stats(no),
        "by_hour": [{"hour": h, **gh[h], "rate": _rate(gh[h]["achieved"], gh[h]["eligible"])} for h in range(24)],
        "voice": [{"voice": f"Cartesia {voice[:8]}…" if voice else "Cartesia (default)", "calls": len(eligible),
                   "rate": _rate(len(yes), len(eligible))}],
        "intro": [{"text": intro, "calls": len(eligible), "rate": _rate(len(yes), len(eligible))}],
        "turns_by_cohort": _cohort_split(yes, no, lambda c: _bucket(c["user_turns"], TURN_EDGES, TURN_LABELS), TURN_LABELS),
        "language_by_cohort": _cohort_split(yes, no, lambda c: c["language"], sorted({c["language"] for c in eligible})),
    }

    sentiment_tab = _sentiment(calls, days)

    # ---- call logs (connected calls; outbound also lists unanswered attempts)
    logs = [{"kind": "call", "id": c["id"], "started_at": c["started_at"], "duration_s": c["duration_s"],
             "caller": c["caller"], "lead_name": c["lead_name"], "campaign": c["campaign_name"], "language": c["language"],
             "role": c["role"], "outcome": c["outcome"], "goal": c["goal"], "ended_by": c["ended_by"], "turns": c["user_turns"],
             "latency_ms": _median(c["latency_ms"]), "tool_calls": len(c["tools"]), "connected": True,
             "sentiment": (c["sentiment"] or {}).get("sentiment", "")} for c in calls]
    if direction == "outbound":
        with C.db() as con:
            phones = {r["id"]: (r["phone"], r["lead_name"]) for r in con.execute("SELECT id, phone, lead_name FROM recipients")}
        logs += [{"kind": "attempt", "id": a["id"], "started_at": a["started_at"], "duration_s": None,
                  "caller": C.mask(phones.get(a["recipient_id"], ("", ""))[0]), "lead_name": phones.get(a["recipient_id"], ("", ""))[1],
                  "campaign": a["campaign_name"], "outcome": a["result"] or "in progress", "failure_reason": a["failure_reason"],
                  "connected": False} for a in attempts if a["result"] != "connected"]
    logs.sort(key=lambda r: r["started_at"] or "", reverse=True)

    return {"agent": direction, "from": d_from.isoformat(), "to": d_to.isoformat(), "filters": choices,
            "overview": overview, "connectivity": connectivity, "engagement": engagement, "tools": tools,
            "goals": goals, "sentiment": sentiment_tab, "calls": logs}


def _hist(values, edges, labels):
    counts = Counter(_bucket(v, edges, labels) for v in values)
    return [(label, counts.get(label, 0)) for label in labels]


def _cohort_split(yes, no, key, labels):
    """Share of each cohort (goal achieved / not) per label, in percent of that cohort."""
    cy, cn = Counter(key(c) for c in yes), Counter(key(c) for c in no)
    return [{"label": label, "achieved_pct": round(100 * cy.get(label, 0) / len(yes), 1) if yes else 0,
             "not_pct": round(100 * cn.get(label, 0) / len(no), 1) if no else 0} for label in labels]


SENTIMENTS = ("positive", "neutral", "negative")


def _sentiment(calls: list[dict], days: list[date]) -> dict:
    """The Sentiment tab: calls the caller spoke in, as judged (sentiment.json)."""
    spoke = [c for c in calls if c["user_turns"] >= 1]
    done = [c for c in spoke if c["sentiment"]]
    s = [c["sentiment"] for c in done]
    by_day = defaultdict(Counter)
    for c in done:
        by_day[_ist_date(c["started"]).isoformat()][c["sentiment"]["sentiment"]] += 1

    def split(key):
        groups = defaultdict(Counter)
        for c in done:
            groups[key(c)][c["sentiment"]["sentiment"]] += 1
        return [{"label": k, "calls": sum(v.values()), **{x: v.get(x, 0) for x in SENTIMENTS}}
                for k, v in sorted(groups.items(), key=lambda kv: -sum(kv[1].values()))]

    questions = defaultdict(list)
    for c in done:
        for q in c["sentiment"].get("unanswered") or []:
            questions[q.strip().rstrip("?").lower()].append((q, c["id"]))
    flagged = [c for c in done if c["sentiment"]["frustrated"] or c["sentiment"]["sentiment"] == "negative"]
    flagged.sort(key=lambda c: c["started_at"] or "", reverse=True)
    return {
        "calls": len(spoke), "analysed": len(done), "not_analysed": len(spoke) - len(done),
        "sentiment": {x: sum(1 for r in s if r["sentiment"] == x) for x in SENTIMENTS},
        "trend": dict(Counter(r["trend"] for r in s)),
        "interest": dict(Counter(r["admission_interest"] for r in s)),
        "frustrated": sum(1 for r in s if r["frustrated"]),
        "with_unanswered": sum(1 for r in s if r.get("unanswered")),
        "topics": dict(Counter(t for r in s for t in r.get("topics") or []).most_common()),
        "series": [{"date": d.isoformat(), **{x: by_day[d.isoformat()].get(x, 0) for x in SENTIMENTS}} for d in days],
        "by_role": split(lambda c: c["role"]),
        "by_language": split(lambda c: c["language"]),
        "questions": sorted(({"question": v[0][0], "count": len(v), "call_ids": [cid for _, cid in v][:5]}
                             for v in questions.values()), key=lambda q: -q["count"])[:30],
        # Every analysed call, newest first: what the interest and topic bars list when clicked.
        "items": [{"id": c["id"], "started_at": c["started_at"], "caller": c.get("caller", ""),
                   "role": c["role"], "language": c["language"], "lead_name": c["lead_name"],
                   "interest": c["sentiment"]["admission_interest"], "topics": c["sentiment"].get("topics") or [],
                   "sentiment": c["sentiment"]["sentiment"], "summary": c["sentiment"].get("summary", "")}
                  for c in sorted(done, key=lambda c: c["started_at"] or "", reverse=True)],
        "flagged": [{"id": c["id"], "started_at": c["started_at"], "role": c["role"], "language": c["language"],
                     "lead_name": c["lead_name"], "sentiment": c["sentiment"]["sentiment"], "trend": c["sentiment"]["trend"],
                     "frustrated": c["sentiment"]["frustrated"], "reason": c["sentiment"].get("frustration_reason", ""),
                     "summary": c["sentiment"].get("summary", "")} for c in flagged[:50]],
    }

