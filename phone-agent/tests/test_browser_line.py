import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

for k, v in {"SARVAM_API_KEY": "t", "RAG_BACKEND_PUBLIC_URL": "http://127.0.0.1:1", "RAG_BACKEND_AUTH_KEY": "t",
             "CARTESIA_API_KEY": "t", "CARTESIA_VOICE_ID": "v"}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient
from pipecat.frames.frames import InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame
from starlette.websockets import WebSocketDisconnect

import server
from phone_audio import BrowserPhoneSerializer


def _setup(s, rate=8000):
    asyncio.run(s.setup(type("Setup", (), {"audio_in_sample_rate": rate})()))
    return s


def test_browser_audio_in_arrives_as_8k_line_audio():
    s = _setup(BrowserPhoneSerializer())
    chunk = b"\x10\x00" * 320  # 20 ms of 16 kHz
    got = [f for f in (asyncio.run(s.deserialize(chunk)) for _ in range(50)) if f]
    assert got and all(isinstance(f, InputAudioRawFrame) and f.sample_rate == 8000 for f in got)


def test_pipeline_rate_16k_is_honoured():
    s = _setup(BrowserPhoneSerializer(), rate=16000)
    got = [f for f in (asyncio.run(s.deserialize(b"\x10\x00" * 320)) for _ in range(50)) if f]
    assert got and all(f.sample_rate == 16000 for f in got)


def test_bot_audio_goes_out_as_8k_pcm_and_interruption_clears_playback():
    s = _setup(BrowserPhoneSerializer())
    frame = OutputAudioRawFrame(audio=b"\x00\x10" * 2400, sample_rate=24000, num_channels=1)  # 100 ms at 24 kHz
    out = [o for o in (asyncio.run(s.serialize(frame)) for _ in range(10)) if o]
    assert out and isinstance(out[0], bytes)
    assert 1400 <= sum(len(o) for o in out) / 10 <= 1700  # about 100 ms at 8 kHz = 1600 bytes
    assert json.loads(asyncio.run(s.serialize(InterruptionFrame())))["type"] == "UserStartedSpeaking"


def test_test_page_and_socket_switch_off(monkeypatch):
    client = TestClient(server.app)
    assert client.get("/test").status_code == 200
    monkeypatch.setattr(server, "TEST_PAGE_ENABLED", False)
    assert client.get("/test").status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/test/ws"):
            pass


def test_phone_pipeline_uses_the_phone_profile(monkeypatch):
    import pipecat.transports.websocket.fastapi as fastapi_transport

    from call_pipeline import build_phone_pipeline

    seen = {}
    real = fastapi_transport.FastAPIWebsocketTransport

    class Spy(real):
        def __init__(self, websocket, params, **kw):
            seen["params"] = params
            super().__init__(websocket, params, **kw)

    monkeypatch.setattr(fastapi_transport, "FastAPIWebsocketTransport", Spy)
    serializer = BrowserPhoneSerializer()
    build_phone_pipeline(MagicMock(), "phone-test-x", serializer)
    p = seen["params"]
    assert p.serializer is serializer and p.audio_out_sample_rate == 8000
    assert p.audio_in_sample_rate in (8000, 16000)
