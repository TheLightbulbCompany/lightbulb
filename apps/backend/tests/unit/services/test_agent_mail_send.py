"""agent_mail_send.send — the one outbound door. Policy per recipient kind."""

import types
from unittest.mock import AsyncMock

import pytest

from core.services import agent_mail_send as mod

OWNER, AGENT = "user_1", "agent_atlas"


@pytest.fixture
def wired(monkeypatch):
    st = types.SimpleNamespace(
        row={"address": "atlas@acme.isol8.co", "owner_id": OWNER, "agent_id": AGENT, "send_to_anyone": True},
        msg={
            "owner_id": OWNER,
            "agent_id": AGENT,
            "from_addr": "bob@x.y",
            "subject": "Hi",
            "headers": {"Message-ID": "<m1@x>", "References": "<r0@x>"},
        },
        cap=True,
        ses_id="m",
        owner_result="sent",
        reply_result="sent",
        owner_kwargs=None,
        reply_kwargs=None,
        free_kwargs=None,
    )
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(mod, "ensure_tenant", AsyncMock(return_value="owner-user_1"))

    async def _owner(**kw):
        st.owner_kwargs = kw
        return st.owner_result

    async def _reply(**kw):
        st.reply_kwargs = kw
        return st.reply_result

    async def _free(**kw):
        st.free_kwargs = kw
        return st.ses_id

    monkeypatch.setattr(mod, "send_to_owner", _owner)
    monkeypatch.setattr(mod, "send_reply", _reply)
    monkeypatch.setattr(mod.ses_email, "send_ses_email", _free)
    monkeypatch.setattr(mod, "ensure_agent_address", AsyncMock(return_value=(st.row["address"], "ok")))
    # _owner_email reads the paperclip-companies table — stub the whole helper, not just lookup_owner_email.
    monkeypatch.setattr(mod, "_owner_email", AsyncMock(return_value="owner@x.y"))
    monkeypatch.setattr(mod.agent_email_repo, "get_by_address", AsyncMock(side_effect=lambda a: st.row))
    monkeypatch.setattr(mod, "get_message", AsyncMock(side_effect=lambda a, k: st.msg))
    monkeypatch.setattr(
        mod.container_repo, "try_consume_daily_email_send", AsyncMock(side_effect=lambda *a, **k: st.cap)
    )
    monkeypatch.setattr(mod, "put_metric", lambda *a, **k: None)
    return st


async def _send(**kw):
    base = dict(owner_id=OWNER, agent_id=AGENT, subject="s", body_text="b")
    base.update(kw)
    return await mod.send(**base)


@pytest.mark.asyncio
async def test_owner_path_delegates_with_agent_and_tenant(wired):
    assert await _send() == "sent"
    assert wired.owner_kwargs["agent_id"] == AGENT
    assert wired.owner_kwargs["tenant_name"] == "owner-user_1"


@pytest.mark.asyncio
async def test_reply_path_threads_from_stored_message(wired):
    assert await _send(reply_key="atlas@acme.isol8.co/2026#m1") == "sent"
    ctx = wired.reply_kwargs["ctx"]
    assert ctx["reply_to"] == "bob@x.y" and ctx["in_reply_to"] == "<m1@x>" and ctx["references"] == "<r0@x>"
    assert wired.reply_kwargs["tenant_name"] == "owner-user_1"


@pytest.mark.asyncio
async def test_reply_path_rejects_other_owners_message(wired):
    wired.msg = dict(wired.msg, owner_id="user_2")
    assert await _send(reply_key="atlas@acme.isol8.co/2026#m1") == "bad_reply_key"
    assert wired.reply_kwargs is None


@pytest.mark.asyncio
async def test_reply_path_bad_key(wired):
    assert await _send(reply_key="garbage") == "bad_reply_key"


@pytest.mark.asyncio
async def test_free_path_requires_send_to_anyone(wired):
    wired.row = dict(wired.row, send_to_anyone=False)
    assert await _send(to="vendor@x.y") == "forbidden_recipient"
    assert wired.free_kwargs is None


@pytest.mark.asyncio
async def test_free_path_blocked_when_outbound_disabled(wired):
    wired.row = dict(wired.row, outbound_disabled="complaint")
    assert await _send(to="vendor@x.y") == "outbound_disabled"


@pytest.mark.asyncio
async def test_free_path_sends_with_tenant_and_cap(wired):
    assert await _send(to="vendor@x.y") == "sent"
    assert wired.free_kwargs["to"] == "vendor@x.y"
    assert wired.free_kwargs["from_addr"] == "atlas@acme.isol8.co"
    assert wired.free_kwargs["tenant_name"] == "owner-user_1"
    assert wired.free_kwargs["kind"] == "agent_free_send"


@pytest.mark.asyncio
async def test_free_path_rate_limited_by_daily_cap(wired):
    wired.cap = False
    assert await _send(to="vendor@x.y") == "rate_limited"


@pytest.mark.asyncio
async def test_reply_path_also_capped(wired):
    wired.cap = False
    assert await _send(reply_key="atlas@acme.isol8.co/2026#m1") == "rate_limited"


@pytest.mark.asyncio
async def test_disabled_when_domain_unset(wired, monkeypatch):
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await _send(to="vendor@x.y") == "disabled"


@pytest.mark.asyncio
async def test_to_and_reply_key_together_is_error(wired):
    assert await _send(to="a@b.c", reply_key="x/y") == "error"
