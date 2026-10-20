"""A quiet office under the call, like Indus's "Quiet office" background sound.

There is no recording of an office in the repo, so by default this makes a soft murmur
of filtered noise with a faint mains hum, once, at the phone line's 8 kHz, and loops it
under everything the bot says. To use a real recording instead, set
PHONE_BACKGROUND_FILE to a mono WAV at 8 kHz. PHONE_BACKGROUND=off removes it;
PHONE_BACKGROUND_VOLUME sets how loud (default 0.15 of a file normalised to half scale).
"""
import os
from pathlib import Path

import numpy as np
import soundfile as sf

RATE = 8000
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "quiet_office.wav"


def make_office_murmur(seconds: float = 20.0, seed: int = 7) -> np.ndarray:
    """Float samples in [-0.5, 0.5] that loop without a click."""
    rng = np.random.default_rng(seed)
    fade = RATE // 2
    n = int(seconds * RATE) + fade
    noise = rng.normal(size=n)
    # one-pole low-pass (about 900 Hz): a distant murmur, not hiss
    a = np.exp(-2 * np.pi * 900 / RATE)
    murmur = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc = a * acc + (1 - a) * noise[i]
        murmur[i] = acc
    # slow, random swell so it breathes
    swell = np.interp(np.arange(n), np.linspace(0, n, 40), rng.uniform(0.5, 1.0, 40))
    t = np.arange(n) / RATE
    sound = murmur * swell + 0.04 * np.sin(2 * np.pi * 100 * t)  # faint hum
    sound /= np.max(np.abs(sound))
    # loop seam: blend the tail into the head
    head, tail = sound[:fade], sound[-fade:]
    ramp = np.linspace(0, 1, fade)
    loop = sound[:-fade].copy()
    loop[:fade] = head * ramp + tail * (1 - ramp)
    return (loop * 0.5).astype(np.float32)


def background_file() -> str | None:
    if os.environ.get("PHONE_BACKGROUND", "quiet_office").strip().lower() in ("off", "none", "0", ""):
        return None
    custom = os.environ.get("PHONE_BACKGROUND_FILE", "").strip()
    if custom:
        return custom
    if not CACHE.exists():
        CACHE.parent.mkdir(exist_ok=True)
        sf.write(str(CACHE), make_office_murmur(), RATE, subtype="PCM_16")
    return str(CACHE)


def make_mixer():
    """The output mixer for the call, or None when the background is off."""
    path = background_file()
    if not path:
        return None
    from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

    volume = float(os.environ.get("PHONE_BACKGROUND_VOLUME", "0.15"))
    return SoundfileMixer(sound_files={"office": path}, default_sound="office", volume=volume)
