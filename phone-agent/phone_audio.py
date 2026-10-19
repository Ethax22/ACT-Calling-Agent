"""The browser test page's end of the phone line.

The page sends the same raw PCM16 16 kHz the website widget sends. This
serializer squeezes it the way a phone call would (8 kHz, mu-law) before the
pipeline sees it, and does the same to every sound the bot sends back, so a
call from the browser is judged on the audio quality a caller gets. Vobiz's
serializer (vobiz_serializer.py) hands the pipeline the same thing.
"""
import json

from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer

LINE_RATE = 8000       # a phone line
BROWSER_IN_RATE = 16000


class BrowserPhoneSerializer(FrameSerializer):
    def __init__(self):
        super().__init__()
        self._pipeline_rate = LINE_RATE
        self._to_line = create_stream_resampler()
        self._from_line = create_stream_resampler()
        self._out_to_line = create_stream_resampler()
        self._out_from_line = create_stream_resampler()

    async def setup(self, setup: FrameProcessorSetup):
        self._pipeline_rate = setup.audio_in_sample_rate

    async def deserialize(self, data):
        if not isinstance(data, (bytes, bytearray)):
            return None
        ulaw = await pcm_to_ulaw(bytes(data), BROWSER_IN_RATE, LINE_RATE, self._to_line)
        pcm = await ulaw_to_pcm(ulaw, LINE_RATE, self._pipeline_rate, self._from_line) if ulaw else None
        if not pcm:
            return None
        return InputAudioRawFrame(audio=pcm, sample_rate=self._pipeline_rate, num_channels=1)

    async def serialize(self, frame: Frame):
        if isinstance(frame, OutputAudioRawFrame):
            ulaw = await pcm_to_ulaw(frame.audio, frame.sample_rate, LINE_RATE, self._out_to_line)
            # The page plays this at 8 kHz.
            return await ulaw_to_pcm(ulaw, LINE_RATE, LINE_RATE, self._out_from_line) if ulaw else None
        if isinstance(frame, OutputTransportMessageFrame):
            return None if self.should_ignore_frame(frame) else json.dumps(frame.message)
        if isinstance(frame, InterruptionFrame):
            # Same message the website widget understands: drop queued bot audio.
            return json.dumps({"type": "UserStartedSpeaking"})
        return None
