"""Phone agent server. Phase 3: the browser test line with records, transfer and call
housekeeping. Vobiz routes arrive in Phase 5."""
import asyncio
import os
import uuid
from pathlib import Path

from fastapi import Body, FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

import config  # noqa: F401  (loads .env files and the shared voice-pipeline path)
from prompts import GREETING

app = FastAPI(title="Agni AI Phone Agent")

# The test page lets anyone who reaches it start a call that spends vendor credits.
# Switch it off (ENABLE_TEST_PAGE=0) on any server the public can reach.
TEST_PAGE_ENABLED = os.environ.get("ENABLE_TEST_PAGE", "1") == "1"
STATIC = Path(__file__).parent / "static"


def _warm_up():
    """Import what a call imports, and load the turn-taking models, before the first call."""
    import call_control, call_pipeline, erp_client, phone_audio, post_call, tools  # noqa: F401
    import background_sound  # noqa: F401
    import conversation_router, latency_metrics, pipeline, speech_text_filter  # noqa: F401
    import pipecat.processors.audio.audio_buffer_processor  # noqa: F401
    import pipecat.services.cartesia.tts, pipecat.services.openai.llm, pipecat.services.sarvam.stt  # noqa: F401
    import pipecat.transports.websocket.fastapi  # noqa: F401
    from turn_taking import build_user_aggregator_params

    build_user_aggregator_params("en")


@app.on_event("startup")
async def _startup():
    import greeting_cache
    from conversation_logger import prune_old_logs

    prune_old_logs()  # 90-day retention, shared with the website's call logs
    asyncio.create_task(greeting_cache.ensure_rendered("en", GREETING))
    if TEST_PAGE_ENABLED:  # campaign calls ring on the test page until the Vobiz line exists
        from dialler import dialler

        dialler.start()
    # Load the turn-taking models now: the first call after a restart waited about 4 s for
    # them (2026-10-20, seen in a recording: the greeting began 5 s after the call opened).
    await asyncio.to_thread(_warm_up)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/test")
async def test_page():
    if not TEST_PAGE_ENABLED:
        return JSONResponse({"detail": "disabled"}, status_code=404)
    return FileResponse(STATIC / "test.html")


# Call history for the test page. Under /test so the server's nginx password covers it
# (deploy/nginx/act-agent.conf): transcripts and recordings can hold student records.
@app.get("/test/api/calls")
async def calls(page: int = 1, per_page: int = 20):
    if not TEST_PAGE_ENABLED:
        return JSONResponse({"detail": "disabled"}, status_code=404)
    from call_history import list_calls

    return await asyncio.to_thread(list_calls, page, per_page)


@app.get("/test/api/calls/{call_id}")
async def call_details(call_id: str):
    from call_history import call_detail

    detail = await asyncio.to_thread(call_detail, call_id) if TEST_PAGE_ENABLED else None
    return detail if detail else JSONResponse({"detail": "not found"}, status_code=404)


@app.get("/test/api/calls/{call_id}/audio")
async def call_audio(call_id: str):
    from call_history import audio_path

    path = audio_path(call_id) if TEST_PAGE_ENABLED else None
    if not path:
        return Response(status_code=404)
    return FileResponse(path, media_type="audio/wav", filename=f"{call_id}.wav")


# ---------------------------------------------------------------- outbound campaigns (test page)
def _off():
    return JSONResponse({"detail": "disabled"}, status_code=404)


def _bad(e: Exception):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.get("/test/api/campaigns")
async def campaigns_list():
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    return {"campaigns": await asyncio.to_thread(C.list_campaigns)}


@app.post("/test/api/campaigns")
async def campaigns_create(spec: dict = Body(...)):
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    try:
        return await asyncio.to_thread(C.create_campaign, spec)
    except (ValueError, TypeError) as e:
        return _bad(e)


@app.get("/test/api/campaigns/{campaign_id}")
async def campaigns_detail(campaign_id: str, status: str = ""):
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    camp = await asyncio.to_thread(C.get_campaign, campaign_id)
    if not camp:
        return JSONResponse({"detail": "not found"}, status_code=404)
    camp["calls"] = await asyncio.to_thread(C.campaign_calls, campaign_id, status)
    camp["timeline"] = await asyncio.to_thread(C.campaign_timeline, campaign_id)
    camp["stop_count"] = await asyncio.to_thread(C.stop_count, campaign_id)
    return camp


@app.get("/test/api/analytics")
async def analytics_report(agent: str = "inbound", date_from: str = "", date_to: str = "", campaign: str = "",
                           language: str = "", role: str = ""):
    """Agent Analytics (all tabs) for the inbound or outbound agent, computed from the saved calls."""
    if not TEST_PAGE_ENABLED:
        return _off()
    import analytics

    try:
        return await asyncio.to_thread(analytics.report, agent, date_from or None, date_to or None, campaign, language, role)
    except ValueError as e:
        return _bad(e)


@app.get("/test/api/sentiment/status")
async def sentiment_status():
    if not TEST_PAGE_ENABLED:
        return _off()
    import sentiment

    return sentiment.status()


@app.post("/test/api/sentiment/update")
async def sentiment_update(date_from: str = "", date_to: str = ""):
    """Build the full sentiment report (website and phone) for a range, in the background."""
    if not TEST_PAGE_ENABLED:
        return _off()
    from datetime import date, timedelta

    import sentiment

    try:
        end = date.fromisoformat(date_to) if date_to else date.today()
        start = date.fromisoformat(date_from) if date_from else end - timedelta(days=6)
    except ValueError as e:
        return _bad(e)
    if start > end or (end - start).days > 92:
        return _bad(ValueError("Pick a range of at most 3 months."))
    return sentiment.start_update(start, end)  # starts a background task, returns the progress


@app.get("/test/sentiment-report")
async def sentiment_report_page():
    """The newest full report (website chat and voice, phone in and out) as a page."""
    if not TEST_PAGE_ENABLED:
        return _off()
    import sentiment

    path = sentiment.latest_report()
    if not path:
        return PlainTextResponse("No sentiment report yet. Use Update on the Analytics page's Sentiment tab.", status_code=404)
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/test/api/attempts")
async def attempts(hours: float = 24):
    """Every campaign attempt in the last `hours` (the campaigns list's calls chart)."""
    if not TEST_PAGE_ENABLED:
        return _off()
    from datetime import timedelta

    import campaigns as C

    since = C.iso(C.now_utc() - timedelta(hours=max(0.1, min(hours, 24 * 90))))
    rows = await asyncio.to_thread(C.all_attempts, since)
    return {"attempts": [{k: r[k] for k in ("started_at", "result", "campaign_id", "campaign_name")} for r in rows]}


@app.post("/test/api/campaigns/{campaign_id}/cancel")
async def campaigns_cancel(campaign_id: str):
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C
    from dialler import dialler

    try:
        camp = await asyncio.to_thread(C.cancel_campaign, campaign_id)
    except ValueError as e:
        return _bad(e)
    cur = dialler.current
    if cur and cur["campaign_id"] == campaign_id and cur["state"] == "ringing":
        dialler.finish(cur["attempt_id"], "cancelled", "campaign cancelled")
    return camp


@app.post("/test/api/campaigns/{campaign_id}/cohorts")
async def campaigns_add_cohort(campaign_id: str, cohort: dict = Body(...)):
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    try:
        return await asyncio.to_thread(C.add_cohort, campaign_id, cohort)
    except ValueError as e:
        return _bad(e)


@app.post("/test/api/campaigns/{campaign_id}/stops")
async def campaigns_stops(campaign_id: str, body: dict = Body(...)):
    """A stop file: numbers this campaign must not call (a list, or CSV/plain text, one per line)."""
    if not TEST_PAGE_ENABLED:
        return _off()
    import re

    import campaigns as C

    # One number per line (or comma/semicolon separated); spaces inside a number are kept ("98765 43210").
    phones = body.get("phones") or re.split(r"[\r\n,;\t]+", body.get("text") or "")
    return {"added": await asyncio.to_thread(C.stop_add, campaign_id, phones),
            "total": await asyncio.to_thread(C.stop_count, campaign_id)}


@app.post("/test/api/recipients/parse")
async def recipients_parse(body: dict = Body(...)):
    """An uploaded CSV (the page reads the file and sends its text): columns, rows, suggested mapping."""
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    parsed = C.parse_csv(body.get("text") or "")
    if not parsed["columns"]:
        return _bad(ValueError("The file has no header row."))
    return parsed


@app.post("/test/api/recipients/validate")
async def recipients_validate(body: dict = Body(...)):
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    valid, invalid = C.build_recipients(body.get("rows") or [], body.get("mapping") or {})
    return {"valid": len(valid), "invalid": invalid, "preview": [{**r, "phone": C.mask(r["phone"])} for r in valid[:5]]}


@app.get("/test/api/recipients/sheet")
async def recipients_sheet():
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    try:
        return await asyncio.to_thread(C.leads_sheet_rows)
    except Exception as e:
        return _bad(e)


@app.get("/test/api/recipients/sample.csv")
async def recipients_sample():
    import campaigns as C

    return PlainTextResponse(C.sample_csv(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=outbound_sample.csv"})


@app.get("/test/api/dnd")
async def dnd_list():
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    return {"dnd": await asyncio.to_thread(C.dnd_list)}


@app.post("/test/api/dnd")
async def dnd_add(body: dict = Body(...)):
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    ok = await asyncio.to_thread(C.dnd_add, body.get("phone") or "", body.get("reason") or "", "manual")
    return {"ok": True} if ok else _bad(ValueError("Not a valid 10-digit mobile number."))


@app.delete("/test/api/dnd/{phone}")
async def dnd_remove(phone: str):
    if not TEST_PAGE_ENABLED:
        return _off()
    import campaigns as C

    return {"removed": await asyncio.to_thread(C.dnd_remove, phone)}


@app.get("/test/api/ringing")
async def ringing():
    """Polled by the test page: an incoming campaign call to answer, if any (also tells the dialler a page is open)."""
    if not TEST_PAGE_ENABLED:
        return _off()
    from dialler import dialler

    return {"ringing": dialler.ringing(), **dialler.status()}


@app.post("/test/api/ringing/{attempt_id}/decline")
async def ringing_decline(attempt_id: str):
    if not TEST_PAGE_ENABLED:
        return _off()
    from dialler import dialler

    return {"declined": dialler.decline(attempt_id)}


_PAGE_FILES = {"campaigns.js": "application/javascript", "campaigns.css": "text/css",
               "analytics.js": "application/javascript", "analytics.css": "text/css"}


@app.get("/test/static/{name}")
async def test_static(name: str):
    """The test page's own script and styles (a fixed list: nothing else in the folder is served)."""
    if not TEST_PAGE_ENABLED or name not in _PAGE_FILES:
        return Response(status_code=404)
    return FileResponse(STATIC / name, media_type=_PAGE_FILES[name], headers={"Cache-Control": "no-cache"})


@app.websocket("/test/ws")
async def test_ws(websocket: WebSocket):
    if not TEST_PAGE_ENABLED:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    from pipecat.pipeline.runner import PipelineRunner

    from call_history import write_meta
    from call_pipeline import start_phone_call
    from phone_audio import BrowserPhoneSerializer
    from post_call import record_call

    from loguru import logger

    from prompts import Lead

    q = websocket.query_params
    lead, campaign = None, None
    attempt_id = q.get("attempt") or ""
    if attempt_id:  # the page answered a ringing campaign call
        from dialler import dialler

        info = dialler.answer(attempt_id)
        if not info:
            await websocket.close(code=4404)  # that call is no longer ringing
            return
        lead = Lead(name=info["lead_name"], course=info["lead_course"], source=info["enquiry_source"],
                    date=info["enquiry_date"], phone=info["phone"])
        campaign = {"id": info["campaign_id"], "name": info["campaign_name"], "attempt_id": attempt_id}
    elif q.get("direction") == "outbound":  # the page plays a lead the college is calling back
        clip = lambda k, n=60: (q.get(k) or "").strip()[:n]
        lead = Lead(name=clip("lead_name", 40), course=clip("lead_course"), source=clip("enquiry_source"),
                    date=clip("enquiry_date", 20), phone=clip("caller", 15))
    session_id = f"phone-test-{uuid.uuid4().hex}"
    started = asyncio.get_running_loop().time()
    try:
        call = await start_phone_call(
            websocket, session_id, BrowserPhoneSerializer(), lead.phone if campaign else q.get("caller", ""),
            live_view=True, lead=lead
        )
    except Exception as e:
        logger.exception("[call] could not start")
        if campaign:
            dialler.call_finished(attempt_id, session_id, 0, error=f"could not start the call: {e}")
        await websocket.close(code=1011)
        return
    try:
        await PipelineRunner().run(call.worker)
    finally:
        if campaign:
            dialler.call_finished(attempt_id, session_id, asyncio.get_running_loop().time() - started)
        try:
            write_meta(session_id, started_at=call.started_at, caller_number=call.caller_number, role=call.role,
                       ended_by=call.control.ended_by, transfer_to=call.control.transfer_to,
                       response_times=call.live.response_times if call.live else None,
                       user_turns=call.live.user_turns if call.live else 0,
                       direction=call.direction, lead=call.lead,
                       language_changes=call.live.language_changes if call.live else None,
                       campaign=campaign, tool_runs=call.live.tool_runs if call.live else None)
        except Exception:
            logger.exception("[call-meta] failed")

        # After the call: structured record + Sheet row. Runs on its own so a slow
        # extraction never holds the connection open. A campaign call's outcome then goes
        # back to the campaign (and "do not call" to the DND list).
        async def after_call():
            record = await record_call(
                session_id, call.role, call.caller_number, call.control.ended_by, call.control.transfer_to,
                direction=call.direction, lead=call.lead)
            if campaign:
                dialler.record_outcome(attempt_id, record)
            # Then the sentiment judgement for the Analytics page (same script as the website report).
            import sentiment
            from call_history import call_dir
            await sentiment.analyse_call(call_dir(session_id))

        asyncio.create_task(after_call())
