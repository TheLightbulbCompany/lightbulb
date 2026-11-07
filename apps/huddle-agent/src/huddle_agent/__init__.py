import asyncio
import logging

GOODBYE = "We're out of time. In one short sentence, say a warm goodbye; don't ask anything."


async def end_after(seconds: float, session, end, on_speech=None) -> None:
    """Cost cap (gate finding 14): after `seconds` the assistant says a short goodbye, then `end()` closes it."""
    await asyncio.sleep(seconds)
    try:
        speech = session.generate_reply(instructions=GOODBYE, allow_interruptions=False)
        if on_speech:
            on_speech(speech)  # meet: only this speech passes the reply gate
        await asyncio.wait_for(speech, 20)
    except Exception:  # noqa: BLE001 -- a lost goodbye must not keep the session (and its bill) running
        logging.getLogger("huddle_agent").warning("time limit: goodbye failed", exc_info=True)
    await end()


def usage_log(session, what: str) -> None:
    """The realtime model's tokens over the session, logged once when it closes: what a call or a meeting cost.
    Cached counts are part of the in_ counts (the API's usage shape)."""
    n = dict.fromkeys(("in_audio", "in_text", "cached_audio", "cached_text", "out_audio", "out_text"), 0)

    def add(ev) -> None:
        m = ev.metrics
        if getattr(m, "type", "") != "realtime_model_metrics":
            return
        i, o, c = m.input_token_details, m.output_token_details, m.input_token_details.cached_tokens_details
        n["in_audio"] += i.audio_tokens
        n["in_text"] += i.text_tokens
        n["cached_audio"] += c.audio_tokens if c else 0
        n["cached_text"] += c.text_tokens if c else 0
        n["out_audio"] += o.audio_tokens
        n["out_text"] += o.text_tokens

    session.on("metrics_collected", add)
    session.on("close", lambda _ev: logging.getLogger("huddle_agent").info("%s usage %s", what, n))
