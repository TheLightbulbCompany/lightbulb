"""The phone receptionist (voice-agents spec §4.1b, §4.3): one Gemini Live session per inbound call.

Dispatched by the LiveKit SIP dispatch rule to its own agent name (`lightbulb-phone-dev`), never the huddle
worker's. It greets with the compliance line, then listens and answers; bookings, messages, transfer and
open-ended work go to the backend's /internal/voice routes (apps/backend/routers/voice_phone.py), which
also keep the call record. Run: `AGENT_MODULE=huddle_agent.phone uv run python -m huddle_agent.phone dev`.
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx
from livekit import api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    cli,
    function_tool,
    room_io,
)
from livekit.plugins import google

logger = logging.getLogger("phone")
# LIVEKIT_AGENT_NAME wins over the decorator's kwarg (livekit-agents 1.8); the image's ENV says lightbulb-huddle.
os.environ["LIVEKIT_AGENT_NAME"] = os.environ.get(
    "PHONE_AGENT_NAME", "lightbulb-phone-dev"
)
MODEL = os.environ.get("PHONE_MODEL", "gemini-3.8-live")  # 10-voice-models.md §5
VOICE = os.environ.get(
    "PHONE_VOICE", "Kore"
)  # one of Gemini's prebuilt voices; the greeting's TTS uses the same
# One warm process is plenty for a line; a busy host gets a longer start and its own load ceiling.
server = AgentServer(
    num_idle_processes=1,
    initialize_process_timeout=60,
    load_threshold=float(os.environ.get("PHONE_LOAD_THRESHOLD", "0.7")),
)

INSTRUCTIONS = """You are {assistant}, the AI receptionist on the phone for {business}. You already greeted the
caller. Speak briefly and warmly, one or two sentences at a time, like a good front desk. Today is {today}.
{caller}What you may tell callers about the business: {facts}
{can}

Times you send to tools are local "YYYY-MM-DDTHH:MM". Offer only slots check_availability returned. Before
booking, get the caller's name and the service, then confirm the day and time back. If you don't know something,
call didnt_know with the question, say so, and offer to take a message; never invent prices or policies. If a tool
answers not_allowed, don't retry it: offer to take a message. Never share anything about other customers. You
can't take payments, change prices, or send email; for those take a message for the owner.
{instructions}"""


class Backend:
    def __init__(self) -> None:
        base = (
            os.environ["LIGHTBULB_API_URL"].rstrip("/").removesuffix("/api/v1")
            + "/api/v1"
        )
        self._http = httpx.AsyncClient(
            base_url=base,
            headers={
                "X-Lightbulb-Huddle-Secret": os.environ["LIGHTBULB_HUDDLE_SECRET"]
            },
            timeout=10,
        )
        self.call_id = ""

    async def post(self, path: str, body: dict | None = None) -> dict:
        r = await self._http.post(
            path
            if path.startswith("/")
            else f"/internal/voice/calls/{self.call_id}/{path}",
            json=body or {},
        )
        if r.is_success:
            return r.json()
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = None
        return {
            "error": (detail or {}).get("code", f"http_{r.status_code}")
            if isinstance(detail, dict)
            else f"http_{r.status_code}"
        }


class Receptionist(Agent):
    def __init__(self, backend: Backend, info: dict, transfer) -> None:
        can = info.get("can") or {}
        off = [
            w
            for k, w in (
                ("book", "book appointments"),
                ("move", "move or cancel appointments"),
            )
            if not can.get(k)
        ]
        super().__init__(instructions=INSTRUCTIONS.format(**{
            "assistant": info["assistantName"], "business": info["businessName"], "today": info["today"],
            "facts": info.get("facts") or "nothing; take a message for any question about the business.",
            "caller": f"The caller is {info['callerName']}, a returning client.\n" if info.get("callerName") else "",
            "can": f"On this line you can't {' or '.join(off)}." if off else "",
            "instructions": f"The owner's instructions: {info['instructions']}" if info.get("instructions") else "",
        }))  # fmt: skip
        self._b, self._transfer = backend, transfer
        self.did: list[str] = []  # what the call did, the record's summary

    @function_tool
    async def check_availability(self, date: str, service: str = "") -> dict:
        """Free appointment start times on a date for a service. date: "YYYY-MM-DD" (local)."""
        return await self._b.post("availability", {"date": date, "service": service})

    @function_tool
    async def book(self, start: str, service: str, name: str) -> dict:
        """Book the caller. start: local "YYYY-MM-DDTHH:MM" from check_availability; service; the caller's name."""
        out = await self._b.post(
            "book", {"start": start, "service": service, "name": name}
        )
        if out.get("booked"):
            self.did.append(f"Booked {service} {start} for {name}")
        return out

    @function_tool
    async def reschedule_or_cancel(
        self, start: str, new_start: str | None = None
    ) -> dict:
        """Move (new_start given) or cancel (new_start empty) one of THIS caller's bookings. Call my_bookings first
        if you don't know its start. Times: local "YYYY-MM-DDTHH:MM"."""
        out = await self._b.post(
            "change", {"start": start, "newStart": new_start or None}
        )
        if not out.get("error"):
            self.did.append(
                f"Moved {start} to {new_start}" if new_start else f"Cancelled {start}"
            )
        return out

    @function_tool
    async def my_bookings(self) -> dict:
        """The upcoming bookings made from this caller's phone number."""
        return await self._b.post("my-bookings")

    @function_tool
    async def take_message(
        self, text: str, name: str = "", callback_number: str = ""
    ) -> dict:
        """Leave a message for the owner (anything you can't answer or do)."""
        self.did.append(f"Message: {text[:120]}")
        return await self._b.post(
            "message", {"text": text, "name": name, "callbackNumber": callback_number}
        )

    @function_tool
    async def didnt_know(self, question: str) -> dict:
        """Note a caller's question you couldn't answer, for the owner."""
        return await self._b.post("unknown", {"question": question})

    @function_tool
    async def transfer_to_human(self) -> dict:
        """Hand the caller to a person at the business. If none is available, offer to take a message."""
        out = await self._b.post("transfer")
        if not out.get("transferTo"):
            return {
                "transferred": False,
                "reason": "nobody is available to take calls right now",
            }
        try:
            await self._transfer(out["transferTo"])
        except Exception:  # noqa: BLE001 -- a SIP refusal: the record says the hand-off was missed
            logger.warning("phone %s: transfer failed", self._b.call_id, exc_info=True)
            await self._b.post("transfer", {"failed": True})
            return {"transferred": False, "reason": "the transfer didn't go through"}
        self.did.append("Transferred to a person")
        return {"transferred": True}

    @function_tool
    async def start_work(self, title: str, brief: str) -> dict:
        """Hand an open-ended request to the team to do after the call (a quote, a callback that needs research).
        Tell the caller the team will follow up; don't wait for it."""
        self.did.append(f"Follow-up: {title}")
        return await self._b.post("start-work", {"title": title, "brief": brief})


@server.rtc_session(agent_name=os.environ["LIVEKIT_AGENT_NAME"])
async def entrypoint(ctx: JobContext) -> None:
    started = time.monotonic()
    await ctx.connect()
    caller = await ctx.wait_for_participant()
    meta = json.loads(
        ctx.job.metadata or "{}"
    )  # a test dispatch (lk dispatch) names the numbers itself
    called = (
        caller.attributes.get("sip.trunkPhoneNumber") or meta.get("calledNumber") or ""
    )
    caller_number = (
        caller.attributes.get("sip.phoneNumber") or meta.get("callerNumber") or ""
    )
    backend = Backend()
    info = await backend.post(
        "/internal/voice/calls",
        {
            "calledNumber": called,
            "callerNumber": caller_number,
            "livekitRoom": ctx.room.name,
        },
    )
    if info.get("error"):
        logger.error("phone: call refused (%s)", info["error"])
        return
    backend.call_id = info["callId"]
    logger.debug("phone: call %s started", backend.call_id)

    async def transfer(to: str) -> None:
        await ctx.api.sip.transfer_sip_participant(
            api.TransferSIPParticipantRequest(
                room_name=ctx.room.name,
                participant_identity=caller.identity,
                transfer_to=f"tel:{to}",
            )
        )

    agent = Receptionist(backend, info, transfer)
    key = os.environ["GOOGLE_API_KEY"]
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=MODEL, voice=VOICE, api_key=key, api_version="v1beta"
        ),
    )
    transcript: list[dict] = []

    @session.on("conversation_item_added")
    def _item(ev) -> None:
        text = getattr(ev.item, "text_content", None)
        if text:
            transcript.append({"role": ev.item.role, "text": text})

    async def on_end(_reason: str = "") -> None:
        out = await backend.post(
            "end",
            {
                "durationSec": int(time.monotonic() - started),
                "summary": "; ".join(agent.did),
                "transcript": transcript,
            },
        )
        logger.info(
            "phone: call %s ended outcome=%s", backend.call_id, out.get("outcome", out)
        )

    ctx.add_shutdown_callback(on_end)
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=caller.identity, close_on_disconnect=True
        ),
        record=True,  # the greeting says the call is recorded
    )
    # The Live model speaks the compliance greeting. Dev test 7 Nov: a TTS say() and caller audio switched off while it
    # played both stalled the Gemini Live session (the reply never finished), so the caller can talk over it.
    # The plugin drops this reply's transcription ("no active generation"), so the record gets the line it was told.
    transcript.append({"role": "assistant", "text": info["greeting"]})
    session.generate_reply(
        instructions=f'Greet the caller by saying exactly this, word for word, and nothing else: "{info["greeting"]}"'
    )


if __name__ == "__main__":
    cli.run_app(server)
