import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame

import server
from vobiz_serializer import VobizFrameSerializer


def test_health():
    assert TestClient(server.app).get("/health").json() == {"status": "ok"}


def _serializer():
    s = VobizFrameSerializer(stream_id="s1", params=VobizFrameSerializer.InputParams(auto_hang_up=False))
    asyncio.run(s.setup(type("Setup", (), {"audio_in_sample_rate": 16000})()))
    return s


def test_media_in_becomes_pcm_at_pipeline_rate():
    s = _serializer()
    msg = json.dumps({"event": "media", "media": {"payload": base64.b64encode(b"\xff" * 160).decode()}})
    # The streaming resampler holds back the first chunks, so feed a second of audio.
    got = [f for f in (asyncio.run(s.deserialize(msg)) for _ in range(50)) if f is not None]
    assert got and all(isinstance(f, InputAudioRawFrame) and f.sample_rate == 16000 for f in got)


def test_audio_out_is_playaudio_mulaw_8k_with_stream_id():
    s = _serializer()
    frame = OutputAudioRawFrame(audio=b"\x00\x10" * 480, sample_rate=24000, num_channels=1)
    out = json.loads(next(o for o in (asyncio.run(s.serialize(frame)) for _ in range(50)) if o))
    assert out["event"] == "playAudio" and out["streamId"] == "s1"
    assert out["media"]["contentType"] == "audio/x-mulaw" and out["media"]["sampleRate"] == 8000


def test_start_event_sets_stream_id_and_barge_in_clears_audio():
    from pipecat.frames.frames import InterruptionFrame
    s = VobizFrameSerializer(stream_id="", params=VobizFrameSerializer.InputParams(auto_hang_up=False))
    asyncio.run(s.deserialize(json.dumps({"event": "start", "start": {"streamId": "abc", "callId": "c1",
                                          "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}})))
    assert json.loads(asyncio.run(s.serialize(InterruptionFrame())))["streamId"] == "abc"
