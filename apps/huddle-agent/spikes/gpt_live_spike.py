# ruff: noqa: ASYNC230, SIM115  (throwaway spike: plain file handles are fine)
"""Task 0 spike: measure the GPT-Live client-delegation contract on livekit-plugins-openai 1.8.11.

Runs one GPTLiveSession straight against the plugin (no LiveKit room): pushes a WAV in real time,
answers each delegation with a fixed stub after STUB_DELAY_S, and logs every server/client event
(audio payloads elided) with its time since start to a JSONL file.

    OPENAI_API_KEY=... uv run python spikes/gpt_live_spike.py OUT.jsonl ask.wav [interrupt.wav]

With a second WAV it is pushed 1.0 s after the stub's answer starts playing (interruption test).
Deleted at the end of Task 5.
"""

import array
import asyncio
import json
import math
import sys
import time
import wave

from livekit import rtc
from livekit.plugins.openai.realtime import GPTLiveModel

# OpenAI's recommended template (developers.openai.com/api/docs/guides/live-prompting),
# filled in for Lightbulb; only [name], [service] and Backend tools are ours.
PROMPT = """You are Lightbulb, a calm, friendly voice assistant for the Lightbulb team.
Speak warmly and naturally, at an unhurried pace. Be clear and direct, not overly cheerful.
If the user is frustrated, acknowledge it briefly and focus on the next helpful step.

Backchannel policy: Use moderate backchannels. Acknowledge naturally without competing with the main response.

Interruption policy: Stop speaking when the user interrupts. Listen to what they say.

Delegation policy:
Backend tools:
- The Router: the team's brain. It knows the team's work across GitHub, email, calendar and docs.
- Memory: the Router remembers and recalls what the team has said and decided.
- Work threads: the Router starts work threads that hand tasks to the team's agents.

Generate <|SpawnThinking|> when:
- The request needs a backend capability or careful reasoning.
- A correction changes the work already requested.

Do not generate <|SpawnThinking|> when:
- You can answer from the conversation or a still-current result.
- You need a brief clarification to understand the request.

Generate <|SpawnThinking|> before giving an answer that depends on backend work.
Do not guess the result while waiting."""

STUB = (
    "This week the team shipped three things: Apps hosting on the Vercel free tier, "
    "the phone receptionist on dev, and CLI version 0.9.11 with the A2A issuer key."
)
STUB_DELAY_S = 2.0  # stands in for the Router's turn
SR = 24000
FRAME = SR // 10  # 100 ms
LOUD = 50  # int16 RMS; the service's silence is all zeros
MAX_S = 75.0


def wav_frames(path):
    with wave.open(path) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2), (
            path
        )
        data = w.readframes(w.getnframes())
    step = FRAME * 2
    return [data[i : i + step].ljust(step, b"\0") for i in range(0, len(data), step)]


def rms(pcm: bytes) -> float:
    a = array.array("h", pcm)
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


async def main(out_path, ask_wav, interrupt_wav=None):
    t0 = time.perf_counter()
    now = lambda: round(time.perf_counter() - t0, 3)
    log = open(out_path, "w")
    marks: dict[str, float] = {}

    def rec(kind, **kw):
        log.write(json.dumps({"t": now(), "kind": kind, **kw}) + "\n")
        log.flush()

    def mark(name):
        if name not in marks:
            marks[name] = now()
            rec("mark", name=name)

    model = GPTLiveModel(delegation="client")
    sess = model.session()

    def on_server(ev):
        ev = dict(ev)
        if ev.get("type") == "session.output_audio.delta":
            return  # timed below from the decoded frames
        rec("server", event=ev)

    def on_client(ev):
        if ev.get("type") == "session.input_audio.append":
            return
        rec("client", event=ev)

    async def answer(d):
        rec("delegation", id=d.id, pending_transcript=d.pending_transcript)
        mark("delegation")
        await asyncio.sleep(STUB_DELAY_S)
        sess.append_commentary(STUB, delegation_id=d.id)
        mark("commentary_sent")

    sess.on("openai_server_event_received", on_server)
    sess.on("openai_client_event_queued", on_client)
    sess.on("delegation_created", lambda d: asyncio.ensure_future(answer(d)))
    sess.on(
        "error", lambda e: rec("error", error=repr(e.error), recoverable=e.recoverable)
    )
    await sess._update_session(instructions=PROMPT)

    out = wave.open(out_path.replace(".jsonl", ".out.wav"), "wb")
    out.setnchannels(1), out.setsampwidth(2), out.setframerate(SR)

    async def read_audio():
        loud = False
        last_loud = None
        async for f in sess.audio_stream:
            pcm = f.frame.data.tobytes()
            out.writeframes(pcm)
            r = rms(pcm)
            if r > LOUD and not loud:
                loud = True
                rec("audio_burst_start")
                if "user_end" in marks:
                    mark("first_audio_after_user")
                if "commentary_sent" in marks:
                    mark("first_audio_after_commentary")
            elif r <= LOUD and loud:
                loud = False
                rec("audio_burst_end")
            if r > LOUD:
                last_loud = now()
            marks["last_loud"] = last_loud or 0

    reader = asyncio.ensure_future(read_audio())
    silence = b"\0" * FRAME * 2
    queue = [silence] * 10 + wav_frames(
        ask_wav
    )  # frames still to push; silence once empty
    pending_interrupt = interrupt_wav and wav_frames(interrupt_wav)
    start, sent = time.perf_counter(), 0
    while now() < MAX_S:
        if not queue:
            mark("interrupt_end" if "interrupt_start" in marks else "user_end")
        if (
            pending_interrupt
            and now() - marks.get("first_audio_after_commentary", 1e9) >= 1.0
        ):
            mark("interrupt_start")
            queue, pending_interrupt = pending_interrupt, None
        sess.push_audio(
            rtc.AudioFrame(queue.pop(0) if queue else silence, SR, 1, FRAME)
        )
        sent += 1
        # done: the stubbed answer was spoken and 5 s of quiet followed (6 s past any interrupt)
        tail = 6.0 if "interrupt_start" in marks else 5.0
        if (
            "first_audio_after_commentary" in marks
            and not pending_interrupt
            and not queue
            and now() - max(marks.get("last_loud", 0), marks.get("interrupt_end", 0))
            > tail
        ):
            break
        await asyncio.sleep(max(0, start + sent * 0.1 - time.perf_counter()))
    mark("closing")
    await sess.aclose()
    reader.cancel()
    await model.aclose()
    out.close()
    rec("marks", marks=marks)
    log.close()
    print(json.dumps(marks, indent=1))


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:]))
