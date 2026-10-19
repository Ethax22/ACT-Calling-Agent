#
# Copyright (c) 2024-2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Vobiz WebSocket frame serializer for audio streaming.

VENDORED from the community package pipecat-vobiz 0.0.3 (BSD 2-Clause, Daily's
Pipecat licence header kept below), then adapted for Pipecat 1.12 on 2026-10-19:
the original never called FrameSerializer.__init__ (so BaseObject was not set up)
and its InputParams lacks resampler_clear_after_secs, which Pipecat 1.12's base
class reads. Changes are marked "ADAPTED". Do not pip install pipecat-vobiz: it
would shadow nothing but its pins (pipecat-ai <1.9) conflict with 1.12.

Implements the protocol described in:
  - https://docs.vobiz.ai/xml/stream/initiate
  - https://docs.vobiz.ai/xml/stream/stream-events

Wire formats supported:
  - ``audio/x-mulaw`` — 8-bit μ-law companded.
  - ``audio/x-l16``   — 16-bit signed linear PCM, **network byte order
    (big-endian)** per RFC 2586 and the Vobiz docs. Pipecat's internal
    PCM is native little-endian, so the L16 path performs a byte swap
    in both directions.

Wire sample rates supported: 8000, 16000, 24000 Hz.

Vobiz declares the authoritative ``mediaFormat`` in its first ``start``
event after the WebSocket upgrade; the values passed via ``InputParams``
are treated as hints/fallbacks and overridden (with a warning) if the
``start`` event disagrees.
"""

import array
import asyncio
import base64
import json
from typing import Optional

from loguru import logger

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer

# Supported Vobiz audio encodings.
ENCODING_MULAW = "audio/x-mulaw"
ENCODING_L16 = "audio/x-l16"
SUPPORTED_ENCODINGS = frozenset({ENCODING_MULAW, ENCODING_L16})

# Supported Vobiz wire sample rates (Hz).
SUPPORTED_SAMPLE_RATES = frozenset({8000, 16000, 24000})

# Hang-up methods.
HANGUP_WS_STOP = "ws_stop"   # send {"event":"stop"} on WebSocket only
HANGUP_REST = "rest"         # call REST DELETE only (legacy)
HANGUP_BOTH = "both"         # send stop on WS AND fire REST DELETE as safety net
SUPPORTED_HANGUP_METHODS = frozenset({HANGUP_WS_STOP, HANGUP_REST, HANGUP_BOTH})


async def parse_vobiz_start(websocket) -> dict:
    """Read and parse the Vobiz ``start`` event from a WebSocket.

    Unlike ``pipecat.runner.utils.parse_telephony_websocket`` (which
    consumes the start frame and returns a provider-agnostic subset),
    this helper returns the full Vobiz-specific payload — including
    ``mediaFormat.encoding`` and ``mediaFormat.sampleRate`` — so callers
    can construct a ``VobizFrameSerializer`` with the wire format Vobiz
    actually negotiated rather than relying on env hints.

    Args:
        websocket: Starlette/FastAPI WebSocket (already ``accept()``-ed).

    Returns:
        dict with keys:
          - ``stream_id`` (str, may be empty)
          - ``call_id`` (str, may be empty)
          - ``encoding`` (str | None) — value from ``start.mediaFormat.encoding``
          - ``sample_rate`` (int | None) — value from ``start.mediaFormat.sampleRate``
          - ``raw`` (dict) — the full parsed JSON message, for debugging

    The function reads exactly one WebSocket text message; subsequent
    media frames remain queued for the Pipecat transport to consume.
    """
    raw_text = await websocket.receive_text()
    try:
        msg = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.error(f"Vobiz first WS message was not JSON: {raw_text!r}")
        return {"stream_id": "", "call_id": "", "encoding": None, "sample_rate": None, "raw": {}}

    if msg.get("event") != "start":
        logger.warning(
            f"Vobiz first WS message was {msg.get('event')!r}, expected 'start'. "
            f"Proceeding without negotiated mediaFormat."
        )

    start = msg.get("start") or {}
    media_format = start.get("mediaFormat") or {}
    rate = media_format.get("sampleRate")
    return {
        "stream_id": start.get("streamId") or "",
        "call_id": start.get("callId") or "",
        "encoding": media_format.get("encoding"),
        "sample_rate": int(rate) if isinstance(rate, (int, float, str)) and str(rate).isdigit() else None,
        "raw": msg,
    }


def _swap16(data: bytes) -> bytes:
    """Byte-swap a buffer of 16-bit signed PCM samples (LE ↔ BE).

    Used to translate between Pipecat's internal little-endian PCM and
    Vobiz's network-byte-order L16 wire format.
    """
    a = array.array("h", data)
    a.byteswap()
    return a.tobytes()


class VobizFrameSerializer(FrameSerializer):
    """Serializer for Vobiz Audio Streaming WebSocket protocol.

    Converts between Pipecat frames and Vobiz's WebSocket audio streaming
    protocol. Supports audio conversion (μ-law or L16 at 8/16/24 kHz),
    DTMF events, barge-in clear, and automatic call termination via the
    Vobiz REST API on ``EndFrame``/``CancelFrame``.

    When ``auto_hang_up`` is enabled (default), the serializer terminates
    the Vobiz call when an ``EndFrame`` or ``CancelFrame`` is processed,
    but requires ``call_id`` / ``auth_id`` / ``auth_token`` to be set.
    """

    class InputParams(FrameSerializer.InputParams):  # ADAPTED: was BaseModel
        """Configuration parameters for VobizFrameSerializer.

        These values are *hints*. If the Vobiz ``start`` event declares a
        different ``mediaFormat``, the serializer adapts and logs a
        warning — the wire format ultimately negotiated by Vobiz wins.

        Parameters:
            vobiz_sample_rate: Expected Vobiz wire sample rate; one of
                8000, 16000, 24000. Defaults to 8000.
            sample_rate: Optional override for pipeline input sample rate
                (otherwise taken from ``StartFrame.audio_in_sample_rate``).
            encoding: Expected wire encoding; ``audio/x-mulaw`` (default)
                or ``audio/x-l16``.
            l16_byte_order: For ``audio/x-l16`` only. ``"be"`` (default,
                per RFC 2586 and Vobiz docs) byte-swaps between Pipecat's
                native LE PCM and the BE wire. ``"le"`` is a passthrough —
                use this if Vobiz actually sends/expects LE despite the
                docs (some telephony providers do this in practice).
            auto_hang_up: Whether to automatically terminate the Vobiz
                call on ``EndFrame``/``CancelFrame``.
            hangup_method: How to terminate when ``auto_hang_up`` fires.
                ``"ws_stop"`` sends Vobiz's documented
                ``{"event":"stop","streamId":...}`` over the WebSocket.
                ``"rest"`` calls the REST ``DELETE /Account/.../Call/...``
                endpoint (requires auth_id/auth_token).
                ``"both"`` (default) sends the WS stop **and** fires the
                REST DELETE in the background as a safety net — covers the
                rare case where the WebSocket closed before EndFrame fires
                and Pipecat couldn't deliver the stop frame.
        """

        vobiz_sample_rate: int = 8000
        sample_rate: Optional[int] = None
        encoding: str = ENCODING_MULAW
        l16_byte_order: str = "be"
        auto_hang_up: bool = True
        hangup_method: str = HANGUP_BOTH

    def __init__(
        self,
        stream_id: str,
        call_id: Optional[str] = None,
        auth_id: Optional[str] = None,
        auth_token: Optional[str] = None,
        params: Optional[InputParams] = None,
    ):
        """Initialize the VobizFrameSerializer.

        Args:
            stream_id: The Vobiz Stream ID (may be empty; overridden by
                the ``start`` event ``streamId`` if so).
            call_id: The associated Vobiz Call ID (required for auto hang-up).
            auth_id: Vobiz auth ID (required for auto hang-up).
            auth_token: Vobiz auth token (required for auto hang-up).
            params: Configuration parameters.

        Raises:
            ValueError: If ``params.encoding`` or ``params.vobiz_sample_rate``
                are outside the supported sets.
        """
        params = params or VobizFrameSerializer.InputParams()
        super().__init__(params)  # ADAPTED: the original skipped the base-class init
        self._params = params
        self._stream_id = stream_id
        self._call_id = call_id
        self._auth_id = auth_id
        self._auth_token = auth_token

        if self._params.encoding not in SUPPORTED_ENCODINGS:
            raise ValueError(
                f"Unsupported Vobiz encoding: {self._params.encoding!r}. "
                f"Supported: {sorted(SUPPORTED_ENCODINGS)}"
            )
        if self._params.vobiz_sample_rate not in SUPPORTED_SAMPLE_RATES:
            raise ValueError(
                f"Unsupported Vobiz sample rate: {self._params.vobiz_sample_rate}. "
                f"Supported: {sorted(SUPPORTED_SAMPLE_RATES)}"
            )
        if self._params.l16_byte_order not in ("be", "le"):
            raise ValueError(
                f"l16_byte_order must be 'be' or 'le', got {self._params.l16_byte_order!r}"
            )
        if self._params.hangup_method not in SUPPORTED_HANGUP_METHODS:
            raise ValueError(
                f"hangup_method must be one of {sorted(SUPPORTED_HANGUP_METHODS)}, "
                f"got {self._params.hangup_method!r}"
            )

        # Working values — may be reassigned when Vobiz `start` arrives.
        self._encoding = self._params.encoding
        self._vobiz_sample_rate = self._params.vobiz_sample_rate
        self._l16_swap = self._params.l16_byte_order == "be"
        self._sample_rate = 0  # Pipeline input rate, filled in setup()

        # ADAPTED: clear-after value comes from the params, as in Pipecat's own serializers.
        self._input_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )
        self._output_resampler = create_stream_resampler(
            clear_after_secs=self._params.resampler_clear_after_secs
        )
        self._hangup_attempted = False
        self._rest_hangup_task: Optional[asyncio.Task] = None

    async def setup(self, setup: FrameProcessorSetup):  # ADAPTED: 1.12 passes FrameProcessorSetup
        """Sets up the serializer with pipeline configuration."""
        self._sample_rate = self._params.sample_rate or setup.audio_in_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """Serializes a Pipecat frame to a Vobiz WebSocket JSON message.

        Returns ``None`` for frames the protocol does not represent.
        """
        if (
            self._params.auto_hang_up
            and not self._hangup_attempted
            and isinstance(frame, (EndFrame, CancelFrame))
        ):
            self._hangup_attempted = True
            method = self._params.hangup_method

            # Fire REST DELETE in the background as a safety net. If the
            # WebSocket has already closed (Vobiz hung up first, network
            # blip, etc.), this still kills the call. Held on self to keep
            # GC from cancelling the task before it runs.
            if method in (HANGUP_REST, HANGUP_BOTH):
                self._rest_hangup_task = asyncio.create_task(self._hang_up_call())

            # Send Vobiz's documented `stop` event on the WebSocket. Vobiz
            # then proceeds past the <Stream> XML and hangs up the call on
            # its side with HangupCause="End Of XML Instructions".
            if method in (HANGUP_WS_STOP, HANGUP_BOTH):
                return json.dumps({"event": "stop", "streamId": self._stream_id})

            return None
        elif isinstance(frame, InterruptionFrame):
            answer = {"event": "clearAudio", "streamId": self._stream_id}
            return json.dumps(answer)
        elif isinstance(frame, AudioRawFrame):
            data = frame.audio

            if self._encoding == ENCODING_MULAW:
                # PCM at frame's rate → 8/16/24 kHz μ-law for Vobiz.
                serialized_data = await pcm_to_ulaw(
                    data, frame.sample_rate, self._vobiz_sample_rate, self._output_resampler
                )
            else:
                # L16: resample first (operates on native LE PCM), then
                # swap LE→BE for the wire iff configured (RFC 2586 says BE,
                # but some providers actually transport LE).
                resampled = await self._output_resampler.resample(
                    data, frame.sample_rate, self._vobiz_sample_rate
                )
                if resampled is None or len(resampled) == 0:
                    return None
                if self._l16_swap:
                    if len(resampled) % 2 != 0:
                        logger.warning(
                            f"Vobiz L16 outbound: odd byte length {len(resampled)}, dropping frame"
                        )
                        return None
                    serialized_data = _swap16(resampled)
                else:
                    serialized_data = resampled

            if serialized_data is None or len(serialized_data) == 0:
                return None

            payload = base64.b64encode(serialized_data).decode("utf-8")
            answer = {
                "event": "playAudio",
                "media": {
                    "contentType": self._encoding,
                    "sampleRate": self._vobiz_sample_rate,
                    "payload": payload,
                },
                "streamId": self._stream_id,
            }
            return json.dumps(answer)
        elif isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            if self.should_ignore_frame(frame):  # ADAPTED: skip RTVI messages like Pipecat's own serializers
                return None
            return json.dumps(frame.message)

        return None

    async def _hang_up_call(self):
        """Hang up the Vobiz call using Vobiz's REST API."""
        try:
            import aiohttp

            auth_id = self._auth_id
            auth_token = self._auth_token
            call_id = self._call_id

            if not call_id or not auth_id or not auth_token:
                missing = []
                if not call_id:
                    missing.append("call_id")
                if not auth_id:
                    missing.append("auth_id")
                if not auth_token:
                    missing.append("auth_token")

                logger.warning(
                    f"Cannot hang up Vobiz call: missing required parameters: {', '.join(missing)}"
                )
                return

            endpoint = f"https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_id}/"
            headers = {
                "X-Auth-ID": auth_id,
                "X-Auth-Token": auth_token,
            }

            async with aiohttp.ClientSession() as session:
                async with session.delete(endpoint, headers=headers) as response:
                    if response.status == 204:
                        logger.debug(f"Successfully terminated Vobiz call {call_id}")
                    elif response.status == 404:
                        logger.debug(f"Vobiz call {call_id} already terminated")
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Failed to terminate Vobiz call {call_id}: "
                            f"Status {response.status}, Response: {error_text}"
                        )

        except Exception as e:
            logger.error(f"Failed to hang up Vobiz call: {e}")

    def _handle_start_event(self, message: dict) -> None:
        """Negotiate wire format from Vobiz's authoritative `start` event."""
        start = message.get("start") or {}

        stream_id = start.get("streamId")
        if stream_id and not self._stream_id:
            self._stream_id = stream_id
            logger.debug(f"Vobiz start: captured streamId {stream_id}")

        media_format = start.get("mediaFormat") or {}
        wire_encoding = media_format.get("encoding")
        wire_rate = media_format.get("sampleRate")

        if wire_encoding and wire_encoding != self._encoding:
            if wire_encoding in SUPPORTED_ENCODINGS:
                logger.warning(
                    f"Vobiz start mediaFormat.encoding={wire_encoding!r} differs from "
                    f"configured {self._encoding!r}; adopting Vobiz value."
                )
                self._encoding = wire_encoding
            else:
                logger.error(
                    f"Vobiz start advertised unsupported encoding {wire_encoding!r}; "
                    f"keeping configured {self._encoding!r}. Audio may be garbled."
                )

        if isinstance(wire_rate, int) and wire_rate != self._vobiz_sample_rate:
            if wire_rate in SUPPORTED_SAMPLE_RATES:
                logger.warning(
                    f"Vobiz start mediaFormat.sampleRate={wire_rate} differs from "
                    f"configured {self._vobiz_sample_rate}; adopting Vobiz value."
                )
                self._vobiz_sample_rate = wire_rate
            else:
                logger.error(
                    f"Vobiz start advertised unsupported sampleRate {wire_rate}; "
                    f"keeping configured {self._vobiz_sample_rate}. Audio may be garbled."
                )

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """Deserializes Vobiz WebSocket data into Pipecat frames.

        Recognised events:
          - ``start``         → negotiate mediaFormat (no Pipecat frame)
          - ``media``         → ``InputAudioRawFrame``
          - ``dtmf``          → ``InputDTMFFrame``
          - ``playedStream``  → debug-logged (no Pipecat frame)
          - ``clearedAudio``  → debug-logged (no Pipecat frame)
        """
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse JSON message: {data}")
            return None

        event = message.get("event")

        if event == "start":
            self._handle_start_event(message)
            return None

        if event == "media":
            media = message.get("media", {})
            payload_base64 = media.get("payload")
            if not payload_base64:
                return None
            payload = base64.b64decode(payload_base64)

            if self._encoding == ENCODING_MULAW:
                # Vobiz μ-law @ wire rate → PCM @ pipeline rate.
                deserialized_data = await ulaw_to_pcm(
                    payload, self._vobiz_sample_rate, self._sample_rate, self._input_resampler
                )
            else:
                # L16: convert wire byte-order to native LE before resampling.
                if self._l16_swap:
                    if len(payload) % 2 != 0:
                        logger.warning(
                            f"Vobiz L16 inbound: odd byte length {len(payload)}, dropping frame"
                        )
                        return None
                    native = _swap16(payload)
                else:
                    native = payload
                deserialized_data = await self._input_resampler.resample(
                    native, self._vobiz_sample_rate, self._sample_rate
                )

            if deserialized_data is None or len(deserialized_data) == 0:
                return None

            return InputAudioRawFrame(
                audio=deserialized_data, num_channels=1, sample_rate=self._sample_rate
            )

        if event == "dtmf":
            dtmf_data = message.get("dtmf", {})
            digit = dtmf_data.get("digit")
            if digit:
                try:
                    return InputDTMFFrame(KeypadEntry(digit))
                except ValueError:
                    logger.warning(f"Invalid DTMF digit received: {digit}")
                    return None
            return None

        if event == "playedStream":
            logger.debug(f"Vobiz playedStream: {message.get('name')}")
            return None

        if event == "clearedAudio":
            logger.debug(f"Vobiz clearedAudio: streamId={message.get('streamId')}")
            return None

        return None
