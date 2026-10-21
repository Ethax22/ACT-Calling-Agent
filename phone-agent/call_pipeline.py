"""The phone call's pipeline: voice-pipeline's build_pipeline with the phone's profile,
the caller identified first, the records tools, and the call housekeeping."""
import os
from dataclasses import dataclass

import config  # noqa: F401  (puts voice-pipeline on sys.path before the imports below)
from prompts import GREETING

# What the pipeline (and so Sarvam's listening) receives. 8000 is the phone line as it
# is; 16000 upsamples first. Tested 2026-10-20: a near tie, so 8000 stays the default.
STT_RATE = int(os.environ.get("PHONE_STT_RATE", "8000"))


# Sarvam's recogniser language for the whole call. "auto" lets it hear Tamil or English as
# spoken. Measured 2026-10-20 (one scenario at a time, synthetic caller): with the call locked
# to English, Tamil spoken first came out as an English sentence 6 times out of 6 ("How to apply
# for citizenship?" for a question about admission) and the bot answered in English; with auto,
# 5 of 6 came out as Tamil and English was unchanged. PHONE_STT_LANGUAGE=locked brings the old
# behaviour back (English at the start, switched by the router after the first Tamil turn).
_stt = os.environ.get("PHONE_STT_LANGUAGE", "auto").strip().lower()
STT_LANGUAGE = None if _stt in ("locked", "") else _stt


@dataclass
class PhoneCall:
    worker: object
    control: object
    caller_number: str
    role: str
    first_name: str
    live: object = None  # LiveView on the browser test line, else None
    started_at: str = ""  # UTC, ISO
    lead: object = None  # prompts.Lead on an outbound call, else None

    @property
    def direction(self) -> str:
        return "outbound" if self.lead else "inbound"


async def start_phone_call(websocket, session_id: str, serializer, caller_number: str = "",
                           live_view: bool = False, lead=None) -> PhoneCall:
    """An inbound call (someone called the college), or with `lead` an outbound one: the
    college calls a lead back, who hears a greeting with their name and course."""
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc).isoformat()
    from pipeline import PipelineProfile, build_pipeline

    from background_sound import make_mixer
    from call_control import NUDGE_SECS, OUTBOUND_NUDGE_SECS, CallControl
    from erp_client import ErpClient
    from prompts import admissions_phone, outbound_greeting
    from tools import OUTBOUND_TOOLS, TOOLS, make_handlers

    erp = ErpClient()
    if lead:
        caller_number = lead.phone or caller_number
        role, first_name = "lead", lead.name
        headers = {"X-Source": "phone", "X-Call-Direction": "outbound", "X-Lead-Name": lead.name,
                   "X-Lead-Course": lead.course, "X-Lead-Source": lead.source, "X-Lead-Date": lead.date,
                   "X-Admissions-Phone": admissions_phone()}
        greeting = outbound_greeting(lead)
    else:
        who = await erp.identify(caller_number)  # before the greeting; unknown if the lookup fails
        role, first_name = who.get("role", "unknown"), who.get("first_name", "")
        headers = {"X-Source": "phone", "X-Caller-Role": role, "X-Caller-Name": first_name}
        greeting = GREETING

    control = CallControl(erp, max_secs=float(config.CALL_MAX_MINUTES) * 60, outbound=bool(lead))
    live = None
    if live_view:
        from live_view import LiveView

        live = LiveView(session_id)
        control.on_end = live.ended

    def on_built(ns):
        control.attach(ns)
        if live:
            live.router = ns.router  # to show "Language changed" on the page

    profile = PipelineProfile(
        serializer=serializer,
        audio_in_rate=STT_RATE,
        audio_out_rate=8000,
        tts_rate=8000,
        greetings={"en": greeting, "ta": greeting},
        llm_headers=headers,
        tools=OUTBOUND_TOOLS if lead else TOOLS,
        function_handlers=make_handlers(erp, control, caller_number),
        user_idle_timeout=OUTBOUND_NUDGE_SECS if lead else NUDGE_SECS,
        on_built=on_built,
        stt_language_code=STT_LANGUAGE,
        audio_out_mixer=make_mixer(),  # quiet office under the call, unless PHONE_BACKGROUND=off
        audio_channels=2,  # caller and bot on separate tracks in the saved recording
        observers=[live] if live else [],
        cache_greeting=not lead,  # the outbound greeting holds the lead's name
    )
    worker = build_pipeline(websocket, "en", session_id, profile)
    if live:
        live.worker = worker
    return PhoneCall(worker, control, caller_number, role, first_name, live, started_at, lead)
