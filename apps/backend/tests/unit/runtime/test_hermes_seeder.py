"""Hermes IdentitySeeder facet (H2.4): per-agent persona files + send_chat wiring.

Hermes carries an agent's persona as the per-run ``instructions`` blob, so the
seeder persists one ``personas/{agent_id}.txt`` per seed agent from the SHARED
``OwnerProfile.facts`` + roster, and ``send_chat`` reads it back as
``instructions``. Three layers:
  1. ``_build_persona`` — pure content: Friday/Atlas persona from facts + roster.
  2. ``seed_owner_agents`` — real tmp EFS: the right files land with the right
     content; empty roster + write-failure degrade best-effort.
  3. ``send_chat`` — the loop is closed: a seeded persona rides ``instructions``.
"""

import json

import httpx
import pytest

from core.runtime.base import OwnerFacts, OwnerProfile
from core.runtime.hermes.adapter import (
    HermesRuntime,
    _build_persona,
    _persona_path,
    _read_persona,
)

pytestmark = pytest.mark.asyncio


_ROSTER = [
    {"id": "friday-uuid", "role": "ceo", "name": "Friday"},
    {"id": "atlas-uuid", "role": "general", "name": "Atlas"},
]

_PERSONAL_FACTS = OwnerFacts(
    owner_type="personal",
    provider_choice="bedrock_claude",
    byo_provider=None,
    display_name="Sam",
    first_name="Sam",
    full_name="Sam Rivers",
    email="sam@example.com",
)


# ---------------------------------------------------------------------------
# _build_persona — pure content from facts + roster
# ---------------------------------------------------------------------------


def test_build_friday_persona_uses_owner_and_roster():
    text = _build_persona("friday", facts=_PERSONAL_FACTS, owner_type="personal", agents=_ROSTER)
    assert "You are Friday" in text
    assert "CEO of Sam's AI company" in text
    # Roster names appear; Atlas is role=general so it is not doubled with "(general)".
    assert "Atlas" in text
    assert "(general)" not in text
    # Provider note rendered from the raw provider_choice.
    assert "Bedrock" in text


def test_build_atlas_persona_is_recruiter():
    text = _build_persona("atlas", facts=_PERSONAL_FACTS, owner_type="personal", agents=_ROSTER)
    assert "You are Atlas" in text
    assert "recruiter" in text.lower()
    assert "Sam" in text


def test_build_persona_without_facts_uses_fallback_owner_and_no_provider():
    # facts=None (a resolve failure) still yields a usable persona.
    text = _build_persona("friday", facts=None, owner_type="org", agents=_ROSTER)
    assert "your team" in text
    # No provider note when facts are absent.
    assert "Bedrock" not in text
    assert "ChatGPT" not in text


def test_build_persona_org_owner_display():
    org_facts = OwnerFacts(
        owner_type="org",
        provider_choice="chatgpt_oauth",
        byo_provider=None,
        display_name="Acme Inc",
        first_name="team",
        full_name="Acme Inc",
        email="founder@acme.com",
        org_name="Acme Inc",
    )
    text = _build_persona("friday", facts=org_facts, owner_type="org", agents=_ROSTER)
    assert "Acme Inc's AI company" in text
    assert "ChatGPT" in text


# ---------------------------------------------------------------------------
# seed_owner_agents — real files on a tmp EFS root
# ---------------------------------------------------------------------------


async def test_seed_writes_friday_and_atlas_personas(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = HermesRuntime()
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER, owner_type="personal", facts=_PERSONAL_FACTS))

    friday_file = _persona_path("org_1", "friday-uuid")
    atlas_file = _persona_path("org_1", "atlas-uuid")
    assert friday_file.exists()
    assert atlas_file.exists()
    # Content rendered from the shared facts.
    assert "You are Friday" in friday_file.read_text()
    assert "CEO of Sam's AI company" in friday_file.read_text()
    assert "You are Atlas" in atlas_file.read_text()


async def test_seed_only_covers_the_friday_atlas_seed_pair(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    roster = _ROSTER + [{"id": "hire-uuid", "role": "general", "name": "Pulse"}]
    rt = HermesRuntime()
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=roster, owner_type="personal", facts=_PERSONAL_FACTS))

    # A non-seed marketplace hire (Pulse) gets no persona in this spike.
    assert not _persona_path("org_1", "hire-uuid").exists()
    assert _persona_path("org_1", "friday-uuid").exists()


async def test_seed_empty_roster_writes_nothing(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = HermesRuntime()
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=[], owner_type="personal", facts=_PERSONAL_FACTS))

    personas_dir = tmp_path / "org_1" / "personas"
    assert not personas_dir.exists()


async def test_seed_without_facts_still_writes_personas(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = HermesRuntime()
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER, owner_type="org", facts=None))

    friday_file = _persona_path("org_1", "friday-uuid")
    assert friday_file.exists()
    assert "your team" in friday_file.read_text()


async def test_seed_write_failure_is_best_effort(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core.runtime.hermes.adapter._write_efs_file", boom)
    rt = HermesRuntime()
    # Must NOT raise — provisioning is never blocked by a seed failure.
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER, owner_type="personal", facts=_PERSONAL_FACTS))


async def test_seed_overwrites_on_reprovision(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = HermesRuntime()
    friday_file = _persona_path("org_1", "friday-uuid")
    friday_file.parent.mkdir(parents=True, exist_ok=True)
    friday_file.write_text("STALE")

    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER, owner_type="personal", facts=_PERSONAL_FACTS))
    # Deterministic-from-facts: the stale blob is replaced (no write-if-missing).
    assert "STALE" not in friday_file.read_text()
    assert "You are Friday" in friday_file.read_text()


# ---------------------------------------------------------------------------
# _read_persona + send_chat — the loop is closed
# ---------------------------------------------------------------------------


def test_read_persona_missing_returns_none(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    assert _read_persona("org_1", "no-such-agent") is None


_IP = "10.0.0.9"
_KEY = "api-key-abc"
_RUN_ID = "run_1"


def _install(monkeypatch, handler):
    async def fake_resolve(self, owner_id):
        return _IP, _KEY

    monkeypatch.setattr(HermesRuntime, "_resolve", fake_resolve)
    monkeypatch.setattr(
        "core.runtime.hermes.adapter._client_factory",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_send_chat_passes_seeded_persona_as_instructions(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    # Seed a persona for the target agent.
    rt = HermesRuntime()
    await rt.seed_owner_agents("org_1", OwnerProfile(agents=_ROSTER, owner_type="personal", facts=_PERSONAL_FACTS))
    persona_text = _persona_path("org_1", "friday-uuid").read_text().strip()

    seen_bodies: list[dict] = []

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            seen_bodies.append(json.loads(request.content))
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=b'data: {"event": "run.completed"}\n\n')
        return httpx.Response(404)

    _install(monkeypatch, handler)

    async def on_event(ev):
        pass

    await rt.send_chat("org_1", "friday-uuid", "hi", "agent:friday-uuid:u1", on_event)

    assert seen_bodies
    body = seen_bodies[0]
    assert body["instructions"] == persona_text
    assert body["input"] == "hi"
    assert body["session_id"] == "agent:friday-uuid:u1"


async def test_send_chat_omits_instructions_when_no_persona(tmp_path, monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    rt = HermesRuntime()  # no persona seeded for this agent

    seen_bodies: list[dict] = []

    def handler(request):
        if request.method == "POST" and request.url.path == "/v1/runs":
            seen_bodies.append(json.loads(request.content))
            return httpx.Response(202, json={"run_id": _RUN_ID, "status": "started"})
        if request.url.path == f"/v1/runs/{_RUN_ID}/events":
            return httpx.Response(200, content=b'data: {"event": "run.completed"}\n\n')
        return httpx.Response(404)

    _install(monkeypatch, handler)

    async def on_event(ev):
        pass

    await rt.send_chat("org_1", "unseeded-agent", "hi", "agent:unseeded-agent:u1", on_event)

    assert seen_bodies
    assert "instructions" not in seen_bodies[0]
