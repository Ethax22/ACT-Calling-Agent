"""Places campaign calls, one at a time.

Every second it looks for a campaign that is due (between its start and end, on an allowed
day, inside its calling window in IST) and its next lead that is due (not yet tried, or a
retry whose time has come). Numbers on the DND list or the campaign's stop list are skipped.
The lead's call then rings, which for now means: on the browser test page. Answered, the
page opens the call and plays the lead; declined counts as busy; not answered within
DIALLER_RING_SECS counts as no answer. Retries follow the campaign's rules.

It only rings while a test page is open (it asked within the last few seconds), so leaving
the page closed does not burn through every lead as "no answer". When the Vobiz line exists,
ring() will place a real call instead and the rest stays the same.
"""
import asyncio
import os
from datetime import datetime, timedelta

from loguru import logger

import campaigns as C

RING_SECS = float(os.environ.get("DIALLER_RING_SECS", "30"))
PAGE_SEEN_SECS = 15
STUCK_CALL_SECS = 20 * 60          # a call that never reported its end (the 15-minute cap plus margin)


class Dialler:
    def __init__(self):
        self.current: dict | None = None     # the call that is ringing or in progress
        self.page_seen: datetime | None = None
        self.last_ring_end: datetime | None = None
        self._task = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.recover()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self):
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("[dialler] tick failed")
            await asyncio.sleep(1)

    def recover(self) -> None:
        """After a restart nothing is ringing: calls left ringing or in progress become failed attempts."""
        now = C.iso(C.now_utc())
        with C._lock, C.db() as con:
            stale = con.execute("SELECT id FROM attempts WHERE result IS NULL").fetchall()
            for a in stale:
                con.execute("UPDATE attempts SET result='failed', failure_reason='agent restarted', ended_at=? WHERE id=?",
                            (now, a["id"]))
            con.execute("UPDATE recipients SET status='retry', next_attempt_at=? WHERE status IN ('ringing','in_call')", (now,))

    # ------------------------------------------------------------ the page
    def page_ping(self) -> None:
        self.page_seen = C.now_utc()

    def ringing(self) -> dict | None:
        """What the test page shows as an incoming call (and the page saying it is open)."""
        self.page_ping()
        cur = self.current
        if not cur or cur["state"] != "ringing":
            return None
        left = RING_SECS - (C.now_utc() - cur["ring_started"]).total_seconds()
        return {"attempt_id": cur["attempt_id"], "campaign_id": cur["campaign_id"], "campaign_name": cur["campaign_name"],
                "lead_name": cur["lead"]["lead_name"], "lead_course": cur["lead"]["lead_course"],
                "phone_masked": C.mask(cur["lead"]["phone"]), "attempt": cur["number"], "seconds_left": max(0, round(left))}

    def status(self) -> dict:
        cur = self.current
        seen = self.page_seen and (C.now_utc() - self.page_seen).total_seconds() <= PAGE_SEEN_SECS
        return {"page_open": bool(seen), "state": cur["state"] if cur else "idle",
                "campaign_id": cur["campaign_id"] if cur else None}

    # ------------------------------------------------------------ choosing the next call
    def tick(self, now: datetime | None = None) -> None:
        now = now or C.now_utc()
        with C._lock:
            cur = self.current
            if cur:
                waited = (now - cur["ring_started"]).total_seconds()
                if cur["state"] == "ringing" and waited > RING_SECS:
                    self.finish(cur["attempt_id"], "no_answer", "not answered", now=now)
                elif cur["state"] == "in_call" and waited > STUCK_CALL_SECS:
                    self.finish(cur["attempt_id"], "failed", "call never reported its end", now=now)
                return
            con = C.db()
            try:
                for row in con.execute("SELECT * FROM campaigns WHERE status IN ('scheduled','running') ORDER BY created_at"):
                    camp = C._campaign_row(row)
                    if self._due_campaign(con, camp, now) and self._ring_next(con, camp, now):
                        return
            finally:
                con.close()

    def _due_campaign(self, con, camp: dict, now: datetime) -> bool:
        if now >= C.parse_time(camp["end_at"]):
            self._complete(con, camp, "end time reached")
            return False
        if now < C.parse_time(camp["start_at"]):
            return False
        if camp["status"] == "scheduled":
            con.execute("UPDATE campaigns SET status='running' WHERE id=?", (camp["id"],))
            con.commit()
            camp["status"] = "running"
        if not C.in_window(camp, now):
            return False
        page_open = self.page_seen and (now - self.page_seen).total_seconds() <= PAGE_SEEN_SECS
        gap = timedelta(seconds=1 / camp["cps"]) if camp["cps"] else timedelta(0)
        return bool(page_open) and (self.last_ring_end is None or now - self.last_ring_end >= gap)

    def _ring_next(self, con, camp: dict, now: datetime) -> bool:
        while True:
            r = con.execute("""SELECT * FROM recipients WHERE campaign_id=? AND status IN ('pending','retry')
                               AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                               ORDER BY next_attempt_at IS NOT NULL, next_attempt_at, seq LIMIT 1""",
                            (camp["id"], C.iso(now))).fetchone()
            if not r:
                left = con.execute("""SELECT COUNT(*) FROM recipients WHERE campaign_id=?
                                      AND status IN ('pending','retry','ringing','in_call')""", (camp["id"],)).fetchone()[0]
                if not left:
                    self._complete(con, camp, "every recipient done")
                return False
            why = C.blocked(con, camp["id"], r["phone"])
            if why:
                con.execute("UPDATE recipients SET status=?, failure_reason=? WHERE id=?",
                            ("skipped_dnd" if why == "dnd" else "stopped",
                             "on the DND list" if why == "dnd" else "on the campaign's stop list", r["id"]))
                con.commit()
                continue
            self._ring(con, camp, dict(r), now)
            return True

    def _ring(self, con, camp: dict, r: dict, now: datetime) -> None:
        number = (r["attempts"] or 0) + 1
        attempt_id = C._new_id("att")
        con.execute("INSERT INTO attempts(id, campaign_id, recipient_id, number, started_at) VALUES(?,?,?,?,?)",
                    (attempt_id, camp["id"], r["id"], number, C.iso(now)))
        con.execute("UPDATE recipients SET status='ringing', attempts=?, last_attempt_at=? WHERE id=?",
                    (number, C.iso(now), r["id"]))
        con.commit()
        self.current = {"attempt_id": attempt_id, "campaign_id": camp["id"], "campaign_name": camp["name"],
                        "recipient_id": r["id"], "number": number, "state": "ringing", "ring_started": now,
                        "lead": {k: r[k] for k in C.LEAD_FIELDS}}
        logger.info(f"[dialler] ringing {C.mask(r['phone'])} for {camp['name']} (attempt {number})")

    def _complete(self, con, camp: dict, why: str) -> None:
        con.execute("UPDATE campaigns SET status='completed', ended_at=? WHERE id=?", (C.iso(C.now_utc()), camp["id"]))
        con.commit()
        logger.info(f"[dialler] campaign {camp['name']} completed ({why})")

    # ------------------------------------------------------------ what happened to the call
    def answer(self, attempt_id: str) -> dict | None:
        """The page answered: returns the lead for the call, or None if that call is not ringing."""
        with C._lock:
            cur = self.current
            if not cur or cur["attempt_id"] != attempt_id or cur["state"] != "ringing":
                return None
            cur["state"] = "in_call"
            now = C.iso(C.now_utc())
            with C.db() as con:
                con.execute("UPDATE attempts SET answered_at=? WHERE id=?", (now, attempt_id))
                con.execute("UPDATE recipients SET status='in_call' WHERE id=?", (cur["recipient_id"],))
            return {**cur["lead"], "campaign_id": cur["campaign_id"], "campaign_name": cur["campaign_name"],
                    "attempt_id": attempt_id}

    def decline(self, attempt_id: str) -> bool:
        cur = self.current
        if not cur or cur["attempt_id"] != attempt_id or cur["state"] != "ringing":
            return False
        self.finish(attempt_id, "busy", "declined")
        return True

    def call_finished(self, attempt_id: str, call_id: str, duration_s: float, error: str = "") -> None:
        """The answered call is over (or could not start)."""
        if error:
            self.finish(attempt_id, "internal_error", error[:200], call_id=call_id, duration_s=duration_s)
            return
        with C.db() as con:
            row = con.execute("SELECT c.retry_on FROM attempts a JOIN campaigns c ON c.id=a.campaign_id WHERE a.id=?",
                              (attempt_id,)).fetchone()
        short = row and "short_duration" in (row["retry_on"] or "") and duration_s < C.SHORT_CALL_SECS
        self.finish(attempt_id, "short_duration" if short else "connected",
                    f"call lasted {duration_s:.0f} s" if short else "", call_id=call_id, duration_s=duration_s)

    def finish(self, attempt_id: str, result: str, reason: str = "", call_id: str | None = None,
               duration_s: float | None = None, now: datetime | None = None) -> None:
        now = now or C.now_utc()
        with C._lock, C.db() as con:
            a = con.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not a or a["result"]:
                return
            con.execute("UPDATE attempts SET ended_at=?, result=?, failure_reason=?, call_id=?, duration_s=? WHERE id=?",
                        (C.iso(now), result, reason or None, call_id, duration_s, attempt_id))
            r = con.execute("SELECT * FROM recipients WHERE id=?", (a["recipient_id"],)).fetchone()
            camp = C._campaign_row(con.execute("SELECT * FROM campaigns WHERE id=?", (a["campaign_id"],)).fetchone())
            status, next_at, err = self._after(camp, dict(r), result, now)
            con.execute("""UPDATE recipients SET status=?, next_attempt_at=?, failure_reason=?, last_call_id=COALESCE(?, last_call_id),
                           error_attempts=error_attempts+? WHERE id=?""",
                        (status, next_at, reason or None, call_id, err, r["id"]))
        if self.current and self.current["attempt_id"] == attempt_id:
            self.current = None
            self.last_ring_end = now
        logger.info(f"[dialler] attempt {attempt_id} {result}{' (' + reason + ')' if reason else ''} -> {status}")

    @staticmethod
    def _after(camp: dict, r: dict, result: str, now: datetime) -> tuple[str, str | None, int]:
        """(recipient status, next attempt time, error-attempt increment) after an attempt."""
        if result == "connected":
            return "completed", None, 0
        if camp["status"] == "cancelled":
            return "cancelled", None, 0
        if result in ("provider_error", "internal_error"):
            rule = (camp["error_retries"] or {}).get(result) or {}
            if result in camp["retry_on"] or rule:
                if (r["error_attempts"] or 0) < int(rule.get("max", 0)):
                    return "retry", C.iso(now + timedelta(minutes=int(rule.get("interval_min", 5)))), 1
            return "failed", None, 1
        if result in camp["retry_on"] and (r["attempts"] or 0) <= camp["max_retries"]:
            return "retry", C.iso(now + timedelta(minutes=camp["retry_interval_min"])), 0
        return result, None, 0

    def record_outcome(self, attempt_id: str, record: dict | None) -> None:
        """The post-call record is ready: keep the outcome, and put "do not call" numbers on the DND list."""
        if not record:
            return
        with C._lock, C.db() as con:
            a = con.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if not a:
                return
            outcome, dnc = record.get("call_outcome", ""), record.get("do_not_call", "")
            con.execute("UPDATE attempts SET outcome=? WHERE id=?", (outcome, attempt_id))
            con.execute("UPDATE recipients SET outcome=?, do_not_call=? WHERE id=?", (outcome, dnc, a["recipient_id"]))
            phone = con.execute("SELECT phone FROM recipients WHERE id=?", (a["recipient_id"],)).fetchone()["phone"]
        if dnc == "yes":
            C.dnd_add(phone, "asked not to be called again", f"call {a['call_id'] or attempt_id}")
            logger.info(f"[dialler] {C.mask(phone)} added to the DND list (asked not to be called)")


dialler = Dialler()
