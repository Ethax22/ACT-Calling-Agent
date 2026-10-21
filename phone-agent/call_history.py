"""Past phone calls for the test page's "Recent calls" table and "Call details" panel.

Reads the call folders the pipeline already writes (voice-pipeline/logs/<UTC date>/phone-<id>/):
transcript.json, audio.wav, call_record.json (post_call.py) and call_meta.json (written
here when a call ends: start and end time, masked caller, role, how it ended, response
times). Calls from before call_meta.json existed get their times from the transcript and
the recording's length, and their caller and role from call_record.json when there is one.
"""
import json
import re
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config  # noqa: F401  (voice-pipeline on sys.path)
from conversation_logger import LOGS_DIR, _session_dir

CALL_ID = re.compile(r"phone-[A-Za-z0-9_-]{4,100}")


def mask_number(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    return ("XXXXXX" + digits[-4:]) if len(digits) >= 4 else ""


def call_dir(call_id: str) -> Path | None:
    """The folder of one call, or None. Only ids that look like ours, so nothing outside the
    log folder can be asked for."""
    if not CALL_ID.fullmatch(call_id or ""):
        return None
    found = sorted(p for p in LOGS_DIR.glob(f"*/{call_id}") if p.is_dir())
    return found[-1] if found else None


def _json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _audio(path: Path) -> dict | None:
    try:
        with wave.open(str(path), "rb") as w:
            return {"channels": w.getnchannels(), "sample_rate": w.getframerate(),
                    "duration_s": round(w.getnframes() / w.getframerate(), 2)}
    except Exception:
        return None


def _parse(ts: str | None):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def write_meta(call_id: str, *, started_at: str, caller_number: str, role: str, ended_by: str,
               transfer_to, response_times: list[float] | None = None, user_turns: int = 0,
               direction: str = "inbound", lead=None, language_changes: list | None = None,
               campaign: dict | None = None) -> Path:
    ended = datetime.now(timezone.utc)
    start = _parse(started_at) or ended
    times = response_times or []
    meta = {
        "call_id": call_id,
        "started_at": start.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_s": round((ended - start).total_seconds(), 1),
        "caller": mask_number(caller_number),
        "role": role or "unknown",
        "ended_by": ended_by or "caller",  # empty: nothing on our side ended it, so the caller hung up
        "transferred_to": transfer_to[0] if transfer_to else "",
        "transfer_number": transfer_to[1] if transfer_to else "",
        "user_turns": user_turns,
        "response_times_s": times,
        "avg_response_s": round(sum(times) / len(times), 2) if times else None,
        "channel": "browser test line",
        "direction": direction,
        "language_changes": language_changes or [],
    }
    if lead is not None:
        meta["lead"] = {"name": lead.name, "course": lead.course, "source": lead.source, "date": lead.date}
    if campaign:
        meta["campaign"] = campaign  # id, name, attempt_id
    folder = call_dir(call_id) or _session_dir(call_id)
    path = folder / "call_meta.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _summary(folder: Path) -> dict:
    meta = _json(folder / "call_meta.json") or {}
    record = _json(folder / "call_record.json") or {}
    transcript = _json(folder / "transcript.json") or {}
    audio = _audio(folder / "audio.wav") if (folder / "audio.wav").exists() else None
    duration = meta.get("duration_s") or (audio or {}).get("duration_s")
    ended = _parse(meta.get("ended_at")) or _parse(transcript.get("logged_at"))
    if ended is None:
        ended = datetime.fromtimestamp(folder.stat().st_mtime, timezone.utc)
    started = _parse(meta.get("started_at")) or (ended - timedelta(seconds=duration or 0))
    messages = transcript.get("messages") or []
    return {
        "id": folder.name,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_s": duration,
        "caller": meta.get("caller") or record.get("caller_number", ""),
        "role": meta.get("role") or record.get("caller_role") or "",
        "ended_by": meta.get("ended_by") or record.get("ended_by") or "",
        "transferred_to": meta.get("transferred_to") or record.get("transferred_to") or "",
        "language": transcript.get("language", ""),
        "messages": sum(1 for m in messages if m.get("role") in ("user", "assistant") and m.get("content")),
        "has_record": bool(record),
        "has_audio": audio is not None,
        "direction": meta.get("direction") or record.get("direction") or "inbound",
        "lead_name": (meta.get("lead") or {}).get("name", ""),
        "outcome": record.get("call_outcome") or record.get("call_disposition") or "",
        "campaign_id": (meta.get("campaign") or {}).get("id", ""),
        "campaign_name": (meta.get("campaign") or {}).get("name", ""),
    }


def list_calls(page: int = 1, per_page: int = 20) -> dict:
    folders = [p for p in LOGS_DIR.glob("*/phone-*") if p.is_dir()] if LOGS_DIR.exists() else []
    rows = sorted((_summary(p) for p in folders), key=lambda r: r["started_at"], reverse=True)
    per_page = max(1, min(int(per_page), 100))
    page = max(1, int(page))
    return {"total": len(rows), "page": page, "per_page": per_page,
            "calls": rows[(page - 1) * per_page: page * per_page]}


def call_detail(call_id: str) -> dict | None:
    folder = call_dir(call_id)
    if not folder:
        return None
    meta = _json(folder / "call_meta.json") or {}
    transcript = _json(folder / "transcript.json") or {}
    return {
        **_summary(folder),
        "meta": meta,
        "record": _json(folder / "call_record.json"),
        "audio": _audio(folder / "audio.wav") if (folder / "audio.wav").exists() else None,
        "transcript": transcript.get("messages") or [],
    }


def audio_path(call_id: str) -> Path | None:
    folder = call_dir(call_id)
    path = folder / "audio.wav" if folder else None
    return path if path and path.exists() else None
