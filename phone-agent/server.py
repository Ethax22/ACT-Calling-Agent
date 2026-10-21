"""Phone agent server. Phase 3: the browser test line with records, transfer and call
housekeeping. Vobiz routes arrive in Phase 5."""
import asyncio
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

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

    session_id = f"phone-test-{uuid.uuid4().hex}"
    call = await start_phone_call(
        websocket, session_id, BrowserPhoneSerializer(), websocket.query_params.get("caller", ""), live_view=True
    )
    try:
        await PipelineRunner().run(call.worker)
    finally:
        try:
            write_meta(session_id, started_at=call.started_at, caller_number=call.caller_number, role=call.role,
                       ended_by=call.control.ended_by, transfer_to=call.control.transfer_to,
                       response_times=call.live.response_times if call.live else None,
                       user_turns=call.live.user_turns if call.live else 0)
        except Exception:
            from loguru import logger

            logger.exception("[call-meta] failed")
        # After the call: structured record + Sheet row. Runs on its own so a slow
        # extraction never holds the connection open.
        asyncio.create_task(record_call(
            session_id, call.role, call.caller_number, call.control.ended_by, call.control.transfer_to))
