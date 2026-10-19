"""Settings for the phone agent, read from the repo-root .env file.

Also puts the shared voice-pipeline modules on sys.path (VOICE_PIPELINE_DIR), so
turn-taking, the language router and the logger are imported, not copied.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

VOICE_PIPELINE_DIR = os.environ.get("VOICE_PIPELINE_DIR", "").strip()
if VOICE_PIPELINE_DIR and VOICE_PIPELINE_DIR not in sys.path:
    sys.path.insert(0, VOICE_PIPELINE_DIR)

CALL_MAX_MINUTES = int(os.environ.get("CALL_MAX_MINUTES", "15"))


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing from {ROOT / '.env'}")
    return value
