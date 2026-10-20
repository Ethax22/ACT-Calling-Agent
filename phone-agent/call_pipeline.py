"""The phone call's pipeline: voice-pipeline's build_pipeline with the phone's profile,
the caller identified first, the records tools, and the call housekeeping."""
import os
from dataclasses import dataclass

import config  # noqa: F401  (puts voice-pipeline on sys.path before the imports below)
from prompts import GREETING

# What the pipeline (and so Sarvam's listening) receives. 8000 is the phone line as it
# is; 16000 upsamples first. Tested 2026-10-20: a near tie, so 8000 stays the default.
STT_RATE = int(os.environ.get("PHONE_STT_RATE", "8000"))


@dataclass
class PhoneCall:
    worker: object
    control: object
    caller_number: str
    role: str
    first_name: str


async def start_phone_call(websocket, session_id: str, serializer, caller_number: str = "") -> PhoneCall:
    from pipeline import PipelineProfile, build_pipeline

    from background_sound import make_mixer
    from call_control import NUDGE_SECS, CallControl
    from erp_client import ErpClient
    from tools import TOOLS, make_handlers

    erp = ErpClient()
    who = await erp.identify(caller_number)  # before the greeting; unknown if the lookup fails
    role, first_name = who.get("role", "unknown"), who.get("first_name", "")
    headers = {"X-Source": "phone", "X-Caller-Role": role, "X-Caller-Name": first_name}

    control = CallControl(erp, max_secs=float(config.CALL_MAX_MINUTES) * 60)
    profile = PipelineProfile(
        serializer=serializer,
        audio_in_rate=STT_RATE,
        audio_out_rate=8000,
        tts_rate=8000,
        greetings={"en": GREETING, "ta": GREETING},
        llm_headers=headers,
        tools=TOOLS,
        function_handlers=make_handlers(erp, control, caller_number),
        user_idle_timeout=NUDGE_SECS,
        on_built=control.attach,
        audio_out_mixer=make_mixer(),  # quiet office under the call, unless PHONE_BACKGROUND=off
    )
    worker = build_pipeline(websocket, "en", session_id, profile)
    return PhoneCall(worker, control, caller_number, role, first_name)
