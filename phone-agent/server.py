"""Phone agent server. Phase 1: health only. Call routes arrive in Phases 2 and 5."""
from fastapi import FastAPI

import config  # noqa: F401  (loads .env and the shared voice-pipeline path)

app = FastAPI(title="Agni AI Phone Agent")


@app.get("/health")
async def health():
    return {"status": "ok"}
