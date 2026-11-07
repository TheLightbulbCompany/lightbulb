import asyncio
import logging

GOODBYE = "We're out of time. In one short sentence, say a warm goodbye; don't ask anything."


async def end_after(seconds: float, session, end, before=None) -> None:
    """Cost cap (gate finding 14): after `seconds` the assistant says a short goodbye, then `end()` closes it."""
    await asyncio.sleep(seconds)
    try:
        if before:
            before()  # e.g. meet: let exactly this reply past the reply gate
        await asyncio.wait_for(session.generate_reply(instructions=GOODBYE, allow_interruptions=False), 20)
    except Exception:  # noqa: BLE001 -- a lost goodbye must not keep the session (and its bill) running
        logging.getLogger("huddle_agent").warning("time limit: goodbye failed", exc_info=True)
    await end()
