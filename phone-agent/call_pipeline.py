"""The phone call's pipeline: voice-pipeline's build_pipeline with the phone's profile."""
import os

import config  # noqa: F401  (puts voice-pipeline on sys.path before the import below)
from prompts import GREETING

# What the pipeline (and so Sarvam's listening) receives. 8000 is the phone line as it
# is; 16000 upsamples first. Step 2.2 settles which hears better.
STT_RATE = int(os.environ.get("PHONE_STT_RATE", "8000"))


def build_phone_pipeline(websocket, session_id: str, serializer, caller_number: str = ""):
    from pipeline import PipelineProfile, build_pipeline

    profile = PipelineProfile(
        serializer=serializer,
        audio_in_rate=STT_RATE,
        audio_out_rate=8000,
        tts_rate=8000,
        greetings={"en": GREETING, "ta": GREETING},
        llm_headers={"X-Source": "phone"},
    )
    return build_pipeline(websocket, "en", session_id, profile)
