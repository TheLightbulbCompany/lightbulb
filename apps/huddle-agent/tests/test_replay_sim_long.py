"""The replay's simulated voice stops a unit past its sound limit as `long`, and lets its retry run free, as
agent.py `_DrivenLive` does (Codex P2 on #3917: the replay never produced `long`)."""

import asyncio
import time
from types import SimpleNamespace

from replay import sim as sim_mod


async def test_a_unit_past_its_sound_limit_is_long_and_its_retry_is_free(monkeypatch):
    monkeypatch.setattr(sim_mod, "sound_limit", lambda unit: 0.05)
    receipts = []
    model = SimpleNamespace(
        hear=lambda: 0.0, first_audio=lambda: 0.0, silent=lambda: False, lead=lambda: 0.0,
        unit=lambda unit: 0.2, stop=lambda: 0.0, trailing=lambda: 0.0,
    )  # fmt: skip
    h = SimpleNamespace(
        may_say=lambda *a: True, on_agent_state=lambda *a: None, on_unit_audible=lambda *a: None,
        on_unit=lambda agent, order, index, unit, outcome, evidence: receipts.append((outcome, evidence["loudMs"])),
    )  # fmt: skip
    sim = SimpleNamespace(model=model, h=h, now=time.monotonic, audio=lambda *a: None)
    mouth = sim_mod.Mouth(sim, "friday", 0)
    assert await mouth._speak(1, 0, "A long sentence.") == "long"
    assert await mouth._speak(1, 0, "A long sentence.") == "said"  # its next try has no limit
    (first, cut_at), (second, heard) = receipts
    assert cut_at < 150 <= heard  # stopped near the limit; the retry played the whole unit
    assert await mouth._speak(1, 1, "Another one.") == "long"  # only the unit stopped last runs free
    await asyncio.sleep(0)
