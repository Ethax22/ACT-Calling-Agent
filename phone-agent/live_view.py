"""The browser test page's live view of a call: what the caller said, what the bot is
saying, the tools it calls and their results, sent to the page as the call happens.

A Pipecat observer, so nothing in the call's pipeline changes. It sees each frame once
and sends small JSON messages back down the same socket as urgent transport messages
(the browser test line's serializer passes them on as text). Only the browser test line
uses it; a real phone line has nobody to show them to.

Also measures the bot's response time per turn (caller stops speaking -> bot starts),
which call_history.py saves with the call.
"""
import json
import time
from collections import deque

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    FunctionCallInProgressFrame,
    FunctionCallResultFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

# Tool arguments the page must not show: what a caller proves who they are with.
HIDDEN_ARGS = ("reg_no", "date_of_birth", "faculty_id")


def masked_args(arguments) -> dict:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return {"arguments": "<unreadable>"}
    if not isinstance(arguments, dict):
        return {}
    return {k: ("<hidden>" if k in HIDDEN_ARGS and v else v) for k, v in arguments.items()}


def _plain(value):
    """A tool result as something JSON can carry."""
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class LiveView(BaseObserver):
    def __init__(self, session_id: str):
        super().__init__(observe_every_push=False)  # each frame once
        self.session_id = session_id
        self.worker = None                 # set once the pipeline is built
        self.response_times: list[float] = []
        self.user_turns = 0
        self._rid = 0                      # one id per bot reply, so the page joins its pieces
        self._reply_open = False
        self._user_stopped_at = None
        self._bot_speaking = False
        self._seen = deque(maxlen=512)     # frame ids, in case a frame is reported twice
        self._tool_events: set[tuple[str, str]] = set()  # tool frames travel both ways: one card each
        self.router = None                 # the call's ConversationRouter, to notice a language change
        self._language = "en"
        self.language_changes: list[dict] = []  # saved with the call: {"after_user_turn": n, "language": "ta"}
        self.tool_runs: list[dict] = []    # saved with the call: {"name", "ms", "status"} (Agent Analytics' Tools tab)
        self._tool_started: dict[str, float] = {}

    async def send(self, message: dict) -> None:
        if self.worker:
            await self.worker.queue_frames([OutputTransportMessageUrgentFrame(message=message)])

    async def ended(self, reason: str) -> None:
        """Called by CallControl when the call is ended from our side."""
        await self.send({"type": "ended", "reason": reason})

    async def on_pipeline_started(self):
        await self.send({"type": "session", "id": self.session_id})

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        if isinstance(frame, OutputTransportMessageUrgentFrame) or frame.id in self._seen:
            return
        self._seen.append(frame.id)
        if isinstance(frame, (LLMFullResponseStartFrame, TTSSpeakFrame)):
            await self._check_language()
        msg = self._message_for(frame)
        if msg and msg["type"] in ("tool_call", "tool_result"):
            key = (msg["type"], msg["id"])
            if key in self._tool_events:
                return
            self._tool_events.add(key)
            if msg["type"] == "tool_call":
                self._tool_started[msg["id"]] = time.monotonic()
            else:
                started = self._tool_started.pop(msg["id"], None)
                result = msg.get("result")
                status = result.get("status", "") if isinstance(result, dict) else ""
                self.tool_runs.append({"name": msg["name"], "status": status or "ok",
                                       "ms": round((time.monotonic() - started) * 1000) if started else None})
        if msg:
            await self.send(msg)

    async def _check_language(self) -> None:
        """Before each bot reply: has the call switched language since the last one?"""
        language = getattr(self.router, "language", None)
        if language and language != self._language:
            self._language = language
            self.language_changes.append({"after_user_turn": self.user_turns, "language": language})
            await self.send({"type": "language", "language": language})

    def _message_for(self, frame) -> dict | None:
        from greeting_cache import PlayGreetingFrame

        if isinstance(frame, InterimTranscriptionFrame):
            return {"type": "user", "text": frame.text, "final": False} if frame.text.strip() else None
        if isinstance(frame, TranscriptionFrame):
            if not frame.text.strip():
                return None
            self.user_turns += 1
            self._reply_open = False
            return {"type": "user", "text": frame.text, "final": True}
        if isinstance(frame, LLMFullResponseStartFrame):
            self._rid += 1
            self._reply_open = True
            return None
        if isinstance(frame, LLMTextFrame):
            if not self._reply_open:
                self._rid += 1
                self._reply_open = True
            return {"type": "bot", "rid": self._rid, "text": frame.text, "append": True} if frame.text else None
        if isinstance(frame, (TTSSpeakFrame, PlayGreetingFrame)):  # greeting, nudges, goodbyes, transfer line
            self._rid += 1
            self._reply_open = False
            return {"type": "bot", "rid": self._rid, "text": frame.text, "append": False}
        if isinstance(frame, FunctionCallInProgressFrame):
            self._reply_open = False  # text after the tool result is a new bubble
            return {"type": "tool_call", "id": frame.tool_call_id, "name": frame.function_name,
                    "args": masked_args(frame.arguments)}
        if isinstance(frame, FunctionCallResultFrame):
            return {"type": "tool_result", "id": frame.tool_call_id, "name": frame.function_name,
                    "result": _plain(frame.result)}
        if isinstance(frame, UserStoppedSpeakingFrame):
            self._user_stopped_at = time.monotonic()
            return None
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            if self._user_stopped_at is not None:
                self.response_times.append(round(time.monotonic() - self._user_stopped_at, 2))
                self._user_stopped_at = None
            return None
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            return None
        if isinstance(frame, InterruptionFrame) and self._bot_speaking:
            return {"type": "interrupted", "rid": self._rid}
        return None
