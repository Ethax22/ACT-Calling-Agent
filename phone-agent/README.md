# phone-agent

The Agni AI phone agent: Vobiz call audio in, Sarvam listening, `rag-backend` answers, Cartesia voice out.
Plan: `ACT-Voice-Assistant/docs/Calling_Agent_DIY_Implementation_Plan.md`.

Run tests: `venv\Scripts\python.exe -m pytest tests -p no:cacheprovider`

## Vobiz protocol check (Phase 1 step 1.4, 2026-10-19)

Read from Vobiz's docs (links below) and from the source of `pipecat-vobiz` 0.0.3. Nothing here has been run
against a real Vobiz call yet; that is Phase 5.

**Answer URL.** Vobiz posts form data (`CallUUID`, `From`, `To`, `Direction`) and expects XML:

```xml
<Response>
  <Stream bidirectional="true" audioTrack="inbound" keepCallAlive="true"
          contentType="audio/x-mulaw;rate=8000">wss://HOST/vobiz/ws</Stream>
</Response>
```

`audioTrack` must be `inbound` when `bidirectional` is true (`both` fails silently, per Vobiz's Pipecat page).
Content type of the XML response is not documented; use `application/xml` and confirm on the first call.

**WebSocket.** Vobiz sends `start` (with `callId`, `streamId`, `mediaFormat`), then `media` (base64 audio, ~20 ms),
then `stop`. We send `playAudio` (`media.contentType`, `media.sampleRate`, `media.payload`, `streamId`),
`clearAudio` (on barge-in) and `stop`. Outbound audio can be mu-law 8 kHz or L16 at 8/16/24 kHz, set per `playAudio`.

**Decision: vendor the serializer.** `vobiz_serializer.py` is `pipecat-vobiz` 0.0.3 (BSD 2-Clause) adapted for
Pipecat 1.12. Reasons not to use the pip package or Pipecat's bundled `PlivoFrameSerializer`:
- `pipecat-vobiz` never calls `FrameSerializer.__init__` and its params lack `resampler_clear_after_secs`, which the
  1.12 base class reads (it targets Pipecat 1.8). Its own docs pin `pipecat-ai <1.9`.
- `PlivoFrameSerializer` ignores the `start` event, needs the stream id up front, and its hang-up calls Plivo's API.
  The wire format is otherwise the same.
- The four serializer tests in `tests/test_phone_agent.py` cover audio in/out, the `start` event and barge-in.

**Hang up.** Send `{"event":"stop","streamId":...}` on the socket; the serializer also fires
`DELETE https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_uuid}/` (headers `X-Auth-ID`, `X-Auth-Token`) as a safety net.

**Transfer.** There is no "transfer to number" field. `POST https://api.vobiz.ai/api/v1/Account/{auth_id}/Call/{call_uuid}/`
with `{"legs":"aleg","aleg_url":"https://HOST/vobiz/transfer?dept=reception","aleg_method":"POST"}`; our `aleg_url`
returns XML:

```xml
<Response><Speak>Connecting you now.</Speak><Dial callerId="OUR_VOBIZ_NUMBER"><Number>TARGET</Number></Dial></Response>
```

Only works while the call is in progress (404 otherwise). `callerId` must be a number our account owns.

**Still unverified until Phase 5:** the exact JSON for `clearAudio`/`stop`, whether `streamId` is mandatory on
`playAudio`, the answer-URL content type, and the `extraHeaders` character rules (the docs contradict themselves).

Sources: vobiz.ai/docs/integrations/pipecat, /xml/stream, /concepts/streaming-websockets, /call/transfer-call,
/call/hangup-call, /xml/overview/how-it-works.
