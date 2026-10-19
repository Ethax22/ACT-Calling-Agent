"""Phone agent server. Phase 3: the browser test line with records, transfer and call
housekeeping. Vobiz routes arrive in Phase 5."""
import asyncio
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse

import config  # noqa: F401  (loads .env files and the shared voice-pipeline path)
from prompts import GREETING

app = FastAPI(title="Agni AI Phone Agent")

# The test page lets anyone who reaches it start a call that spends vendor credits.
# Switch it off (ENABLE_TEST_PAGE=0) on any server the public can reach.
TEST_PAGE_ENABLED = os.environ.get("ENABLE_TEST_PAGE", "1") == "1"
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
async def _startup():
    import greeting_cache
    from conversation_logger import prune_old_logs

    prune_old_logs()  # 90-day retention, shared with the website's call logs
    asyncio.create_task(greeting_cache.ensure_rendered("en", GREETING))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/test")
async def test_page():
    if not TEST_PAGE_ENABLED:
        return JSONResponse({"detail": "disabled"}, status_code=404)
    return FileResponse(STATIC / "test.html")


@app.websocket("/test/ws")
async def test_ws(websocket: WebSocket):
    if not TEST_PAGE_ENABLED:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    from pipecat.pipeline.runner import PipelineRunner

    from call_pipeline import start_phone_call
    from phone_audio import BrowserPhoneSerializer
    from post_call import record_call

    session_id = f"phone-test-{uuid.uuid4().hex}"
    call = await start_phone_call(
        websocket, session_id, BrowserPhoneSerializer(), websocket.query_params.get("caller", "")
    )
    try:
        await PipelineRunner().run(call.worker)
    finally:
        # After the call: structured record + Sheet row. Runs on its own so a slow
        # extraction never holds the connection open.
        asyncio.create_task(record_call(
            session_id, call.role, call.caller_number, call.control.ended_by, call.control.transfer_to))
