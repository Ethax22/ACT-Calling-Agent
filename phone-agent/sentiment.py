"""Sentiment for phone calls, using the website bot's report code (rag-backend/sentiment_report.py)
so phone calls and the website are judged by one script and land in one report.

- After each call: analyse_call() judges that call and caches sentiment.json beside it, so the
  Analytics page's Sentiment tab is current without waiting for the daily report.
- From the Analytics page: start_update() builds the full report (website chat, website voice,
  phone inbound and outbound) for a date range in the background; calls already judged are
  read from their cache and cost nothing.
- latest_report() is the newest full report, shown at /test/sentiment-report.

The module is loaded from its file, not put on sys.path: rag-backend has its own
conversation_logger.py, which would clash with voice-pipeline's.
"""
import asyncio
import importlib.util
import os
from datetime import date
from pathlib import Path

from loguru import logger

import config

RAG_BACKEND_DIR = Path(os.environ.get("RAG_BACKEND_DIR", "").strip()
                       or Path(config.VOICE_PIPELINE_DIR).parent / "rag-backend")
_module = None
_job = {"running": False, "done": 0, "total": 0, "error": "", "report": "", "finished_at": ""}


def module():
    global _module
    if _module is None:
        spec = importlib.util.spec_from_file_location("sentiment_report", RAG_BACKEND_DIR / "sentiment_report.py")
        _module = importlib.util.module_from_spec(spec)
        # The script loads rag-backend/.env when imported; keep the phone agent's own settings as
        # they are (it already has SARVAM_API_KEY, the only key the script needs).
        env = dict(os.environ)
        try:
            spec.loader.exec_module(_module)
        finally:
            os.environ.clear()
            os.environ.update(env)
    return _module


async def analyse_call(folder: Path | None) -> None:
    """Never raises: sentiment is a nice-to-have after the call."""
    if folder is None:
        return
    try:
        result = await module().analyse_folder(folder)
        if result:
            logger.info(f"[sentiment] {folder.name[:16]} {result['sentiment']} interest={result['admission_interest']}"
                        f" frustrated={result['frustrated']}")
    except Exception:
        logger.exception("[sentiment] failed")


def status() -> dict:
    return dict(_job)


def start_update(start: date, end: date) -> dict:
    """Build the full report for start..end (UTC folder dates) in the background."""
    if _job["running"]:
        return status()
    m = module()
    total = len(m._load_conversations(start, end))
    _job.update(running=True, done=0, total=total, error="", report="")

    async def run():
        original = m._analyse

        async def counted(*args, **kwargs):  # progress for the page
            try:
                return await original(*args, **kwargs)
            finally:
                _job["done"] += 1

        m._analyse = counted
        try:
            path = await m.build_report(start, end)
            _job["report"] = path.name if path else ""
            if not path:
                _job["error"] = "Nothing to report in that range." if not total else "The analysis calls failed."
        except Exception as e:
            logger.exception("[sentiment] report failed")
            _job["error"] = str(e)
        finally:
            m._analyse = original
            _job["running"] = False
            from datetime import datetime, timezone
            _job["finished_at"] = datetime.now(timezone.utc).isoformat()

    asyncio.create_task(run())
    return status()


def latest_report() -> Path | None:
    """The newest report that covers every channel (no _website / _phone suffix)."""
    reports = RAG_BACKEND_DIR / "reports"
    found = [p for p in reports.glob("sentiment_*.html") if not p.stem.endswith(("_website", "_phone"))] \
        if reports.exists() else []
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def judged(folder: Path) -> dict | None:
    """This call's judgement by the current phone prompt, or None (not judged yet, or judged
    by the website prompt before phone calls had their own)."""
    try:
        return module().cached_result(folder)
    except Exception:
        return None
