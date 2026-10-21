"""Call housekeeping: quiet-caller nudges, the call-length cap, ending the call, and
handing it to a person.

Indus did these from its settings (nudge after 10 s then 10 s more then hang up, 15
minutes, forward to a number). Same behaviour here. The Tamil lines were written by
Claude and have not been checked by a native speaker.
"""
import asyncio
import os

from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, OutputTransportMessageFrame, TTSSpeakFrame

NUDGE_SECS = float(os.environ.get("PHONE_NUDGE_SECS", "10"))

NUDGE = {
    "en": "Hello, are you still there? Happy to help with anything about Agni College of Technology.",
    "ta": "ஹலோ, நீங்க இன்னும் லைன்ல இருக்கீங்களா? Agni College of Technology பத்தி எதுவும் கேளுங்க.",
}
GOODBYE = {
    "en": "Thank you for calling Agni College of Technology. Have a good day.",
    "ta": "Agni College of Technology-ஐ அழைத்ததற்கு நன்றி. நல்ல நாளாக அமையட்டும்.",
}
TIME_LIMIT = {
    "en": "We have reached the time limit for this call. Thank you for calling Agni College of Technology. Goodbye.",
    "ta": "இந்த அழைப்பின் நேர வரம்பை எட்டிவிட்டோம். Agni College of Technology-ஐ அழைத்ததற்கு நன்றி. வணக்கம்.",
}


class CallControl:
    def __init__(self, erp, max_secs: float):
        self._erp = erp
        self._max_secs = max_secs
        self._ns = None
        self._idle = 0
        self._cap_task = None
        self.ended_by = ""            # why the call ended from our side: end_call, idle, time_limit, transfer
        self.transfer_to = None       # (department, number) once a transfer was asked for
        self.on_end = None            # async callback(reason): the test page's live view

    def attach(self, ns) -> None:
        """Pipeline hook (PipelineProfile.on_built)."""
        self._ns = ns
        ns.user_aggregator.add_event_handler("on_user_turn_idle", self._on_idle)
        ns.user_aggregator.add_event_handler("on_user_turn_started", self._on_user_started)
        ns.transport.add_event_handler("on_client_disconnected", self._on_disconnected)
        self._cap_task = asyncio.get_running_loop().create_task(self._cap())

    def _lang(self) -> str:
        return "ta" if self._ns.router.language == "ta" else "en"

    async def _on_user_started(self, *_):
        self._idle = 0

    async def _on_idle(self, *_):
        self._idle += 1
        if self._idle == 1:
            await self._ns.worker.queue_frames([TTSSpeakFrame(NUDGE[self._lang()])])
        else:
            await self.say_and_end(GOODBYE[self._lang()], "idle")

    async def _cap(self):
        await asyncio.sleep(self._max_secs)
        await self.say_and_end(TIME_LIMIT[self._lang()], "time_limit")

    async def _on_disconnected(self, *_):
        if self._cap_task:
            self._cap_task.cancel()

    async def say_and_end(self, line: str, reason: str) -> None:
        self.ended_by = self.ended_by or reason
        if self.on_end:
            await self.on_end(self.ended_by)
        frames = ([TTSSpeakFrame(line)] if line.strip() else []) + [EndWorkerFrame(reason=reason)]
        await self._ns.worker.queue_frames(frames)

    async def transfer(self, department: str, line: str) -> bool:
        """Say the line, then hand the call over. False if there is no number to dial."""
        route = await self._erp.route(department)
        number = route.get("transfer_number", "none")
        if route.get("status") != "ok" or number == "none":
            logger.error(f"[transfer] no number for {department}")
            return False
        self.transfer_to = (route.get("department", department), number)
        logger.info(f"[transfer] {self.transfer_to[0]} -> {number}")
        # Browser test line: tell the page, then end. The Vobiz line will carry the call
        # to `number` instead of hanging up (Phase 5).
        await self._ns.worker.queue_frames(
            [OutputTransportMessageFrame(message={"type": "transfer", "department": self.transfer_to[0], "number": number})]
        )
        await self.say_and_end(line, "transfer")
        return True
