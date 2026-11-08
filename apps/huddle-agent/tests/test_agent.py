import json
from types import SimpleNamespace

from huddle_agent import agent


async def test_one_deployed_agent_routes_phone_meet_and_owner_and_drops_the_rest(monkeypatch, caplog):
    """The LiveKit plan allows one deployed agent: `lightbulb-huddle` takes phone calls (the SIP dispatch rule's
    `phone-` room prefix), Meets and owner Live. Any other room has no handler."""
    went = []

    async def to(where, *_):
        went.append(where)

    monkeypatch.setattr(agent.phone, "entrypoint", lambda ctx: to("phone"))
    monkeypatch.setattr(agent.meet, "run", lambda ctx, meta: to("meet"))
    monkeypatch.setattr(agent.owner, "run", lambda ctx, meta: to("owner"))

    def job(room, meta):
        return SimpleNamespace(job=SimpleNamespace(room=SimpleNamespace(name=room), metadata=meta))

    await agent.entrypoint(job("phone-prod-_+15551234567_aB3x", ""))  # a SIP dispatch carries no metadata
    await agent.entrypoint(job("meet-prod-r1", json.dumps({"source": "meet", "roomId": "r1"})))
    await agent.entrypoint(job("huddle-prod-r1", json.dumps({"mode": "owner", "roomId": "r1"})))
    await agent.entrypoint(job("huddle-prod-r2", json.dumps({"roomId": "r2"})))
    assert went == ["phone", "meet", "owner"]
    assert "no handler for room huddle-prod-r2" in caplog.text


def test_no_retiring_openai_models():
    # developers.openai.com/api/docs/deprecations: gpt-realtime and -mini shut down 20 Jan 2027, gpt-4o-mini-transcribe 26 Feb 2027
    import pathlib
    import re

    src = pathlib.Path(agent.__file__).parent
    bad = re.compile(r"gpt-realtime(?![-\w.])|gpt-realtime-mini|gpt-4o-mini-transcribe|gpt-4o-transcribe")
    hits = [f"{p.name}:{n}" for p in src.glob("*.py") for n, line in enumerate(p.read_text().splitlines(), 1)
            if bad.search(line) and not line.lstrip().startswith("#") and "deprecated" not in line and "shut" not in line]
    assert not hits, hits


def test_default_engines_are_openai(monkeypatch):
    import importlib

    from huddle_agent import phone

    for k in ("PHONE_ENGINE", "PHONE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(phone)
    assert phone.OPENAI and phone.MODEL == "gpt-realtime-2.1-mini"
