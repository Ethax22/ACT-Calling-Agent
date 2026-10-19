"""Settings for the phone agent.

Reads the repo-root .env first, then voice-pipeline's .env for anything still unset
(so the vendor keys and turn-taking settings tuned for the website are not typed
twice; a value in the root .env wins). Also puts the shared voice-pipeline modules on
sys.path, so turn-taking, the language router and the logger are imported, not copied.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Default: the website repo checked out next to this one.
VOICE_PIPELINE_DIR = os.environ.get("VOICE_PIPELINE_DIR", "").strip() or str(
    ROOT.parent / "ACT-Voice-Assistant" / "voice-pipeline"
)
load_dotenv(Path(VOICE_PIPELINE_DIR) / ".env")  # does not override values already set
if VOICE_PIPELINE_DIR not in sys.path:
    sys.path.insert(0, VOICE_PIPELINE_DIR)

CALL_MAX_MINUTES = int(os.environ.get("CALL_MAX_MINUTES", "15"))


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing from {ROOT / '.env'}")
    return value
