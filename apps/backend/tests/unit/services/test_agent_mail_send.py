"""agent_mail_send.send — the one outbound door. Policy per recipient kind."""

import types
from unittest.mock import AsyncMock

import pytest

from core.services import agent_mail_send as mod

OWNER, AGENT = "user_1", "agent_atlas"
TEAM = "agents@acme.isol8.co"


@pytest.fixture
def wired(monkeypatch):
    st = types.SimpleNamespace(
        row={"address": TEAM, "owner_id": OWNER, "send_to_anyone": True},
        msg={
            "owner_id": OWNER,
            "agent_id": AGENT,
            "from_addr": "bob@x.y",
            "subject": "Hi",
            "headers": {"Message-ID": "<m1@x>", "References": "<r0@x>"},
        },
        cap=True,
        ses_id="<sent1@isol8.co>",
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
    monkeypatch.setattr(mod, "ensure_team_address", AsyncMock(return_value=(TEAM, "ok")))
    monkeypatch.setattr(mod, "team_address", AsyncMock(return_value=TEAM))
    monkeypatch.setattr(mod, "from_header", AsyncMock(return_value=f'"Atlas · Acme agents" <{TEAM}>'))
    st.put_sent = AsyncMock()
    monkeypatch.setattr(
        mod,
        "WorkspaceRepo",
        lambda **_: types.SimpleNamespace(get_onboarding_state=AsyncMock(return_value={"company_id": "co_1"})),
    )
    monkeypatch.setattr(mod, "resolve_agent_name", AsyncMock(return_value="Atlas"))
    monkeypatch.setattr(mod.agent_mail_inbox_repo, "put_sent", st.put_sent)
    # _owner_email reads the paperclip-companies table — stub the whole helper, not just lookup_owner_email.
    monkeypatch.setattr(mod, "_owner_email", AsyncMock(return_value="owner@x.y"))
    monkeypatch.setattr(mod.agent_email_repo, "get_by_address", AsyncMock(side_effect=lambda a: st.row))
    monkeypatch.setattr(mod, "get_message", AsyncMock(side_effect=lambda a, k: st.msg))
    monkeypatch.setattr(
        mod.container_repo, "try_consume_daily_email_send", AsyncMock(side_effect=lambda *a, **k: st.cap)
    )
    monkeypatch.setattr(mod, "put_metric", lambda *a, **k: None)
    monkeypatch.setattr(mod.agent_email_repo, "text_line_for_owner", AsyncMock(return_value=None))
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
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "sent"
    ctx = wired.reply_kwargs["ctx"]
    assert ctx["reply_to"] == "bob@x.y" and ctx["in_reply_to"] == "<m1@x>" and ctx["references"] == "<r0@x>"
    assert wired.reply_kwargs["tenant_name"] == "owner-user_1"


@pytest.mark.asyncio
async def test_reply_path_rejects_other_owners_message(wired):
    wired.msg = dict(wired.msg, owner_id="user_2")
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "bad_reply_key"
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
    assert wired.free_kwargs["from_addr"] == f'"Atlas · Acme agents" <{TEAM}>'
    mod.from_header.assert_awaited_once_with(OWNER, AGENT, TEAM, agent_name="Atlas")
    assert mod.resolve_agent_name.await_args.kwargs == {"owner_id": OWNER, "agent_id": AGENT}
    assert wired.free_kwargs["tenant_name"] == "owner-user_1"
    assert wired.free_kwargs["kind"] == "agent_free_send"
    assert wired.free_kwargs["headers"] == {"Auto-Submitted": "auto-generated"}


@pytest.mark.asyncio
async def test_free_send_records_the_writer_under_the_returned_message_id(wired):
    assert await _send(to="vendor@x.y") == "sent"
    kw = wired.put_sent.await_args.kwargs
    assert (kw["address"], kw["message_id"], kw["agent_id"], kw["owner_id"]) == (TEAM, "<sent1@isol8.co>", AGENT, OWNER)
    assert kw["text"] == "b"  # the sent body is kept for the Mail panel


@pytest.mark.asyncio
async def test_free_send_refuses_an_agent_outside_the_company(wired, monkeypatch):
    monkeypatch.setattr(mod, "resolve_agent_name", AsyncMock(return_value=None))
    assert await _send(agent_id="agent_foreign", to="vendor@x.y") == "unknown_agent"
    assert wired.free_kwargs is None
    wired.put_sent.assert_not_called()
    mod.container_repo.try_consume_daily_email_send.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_free_send_records_nothing(wired):
    wired.ses_id = None
    assert await _send(to="vendor@x.y") == "error"
    wired.put_sent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["agent_atlas", "agent_friday", "agent_ceo"])
async def test_the_one_rows_outbound_disabled_blocks_every_agent(wired, agent):
    wired.row = dict(wired.row, outbound_disabled="complaint")
    assert await _send(agent_id=agent, to="vendor@x.y") == "outbound_disabled"
    assert await _send(agent_id=agent, reply_key=f"{TEAM}/2026#m1") == "outbound_disabled"


@pytest.mark.asyncio
async def test_reply_key_of_a_sent_row_is_refused(wired):
    wired.msg = dict(wired.msg, direction="out")
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "bad_reply_key"


@pytest.mark.asyncio
async def test_mail_reply_to_a_text_row_is_refused(wired):
    wired.msg = dict(wired.msg, channel="text")
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "bad_reply_key"
    assert wired.reply_kwargs is None


@pytest.mark.asyncio
async def test_free_path_rate_limited_by_daily_cap(wired):
    wired.cap = False
    assert await _send(to="vendor@x.y") == "rate_limited"


@pytest.mark.asyncio
async def test_reply_path_also_capped(wired):
    wired.cap = False
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "rate_limited"


@pytest.mark.asyncio
async def test_disabled_when_domain_unset(wired, monkeypatch):
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await _send(to="vendor@x.y") == "disabled"


@pytest.mark.asyncio
async def test_to_and_reply_key_together_is_error(wired):
    assert await _send(to="a@b.c", reply_key="x/y") == "error"


@pytest.mark.asyncio
async def test_free_path_rejects_multiple_recipients(wired):
    """One `to` header comma-joined IS the SES recipient list — a single cap
    unit must not buy an arbitrary-size blast."""
    assert await _send(to="v@x.y, attacker@evil.com") == "forbidden_recipient"
    assert wired.free_kwargs is None


@pytest.mark.asyncio
async def test_free_path_accepts_display_name_and_sends_bare_address(wired):
    assert await _send(to="Vendor <v@x.y>") == "sent"
    assert wired.free_kwargs["to"] == "v@x.y"


@pytest.mark.asyncio
async def test_free_path_rejects_malformed_recipient(wired):
    assert await _send(to="garbage") == "forbidden_recipient"
    assert wired.free_kwargs is None


@pytest.mark.asyncio
async def test_reply_path_requires_stored_agent_id(wired):
    """Stored state decides the replying agent — a message with no agent_id
    must not fall back to the caller-supplied agent_id."""
    wired.msg = {k: v for k, v in wired.msg.items() if k != "agent_id"}
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "bad_reply_key"
    assert wired.reply_kwargs is None


@pytest.mark.asyncio
async def test_reply_path_no_address_without_the_team_row(wired, monkeypatch):
    monkeypatch.setattr(mod, "team_address", AsyncMock(return_value=None))
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "no_address"
    assert wired.reply_kwargs is None


@pytest.mark.asyncio
async def test_reply_path_blocked_when_outbound_disabled(wired):
    wired.row = dict(wired.row, outbound_disabled="complaint")
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "outbound_disabled"
    assert wired.reply_kwargs is None


@pytest.mark.asyncio
async def test_reply_path_stranger_sender_requires_send_to_anyone(wired):
    """A stranger who emailed first (stored_not_woken) does not thereby earn
    a reply — the inbox route hands the agent every stored message's reply
    key, so without this gate a stranger only has to email first to receive
    agent-authored mail with no owner opt-in."""
    wired.row = dict(wired.row, send_to_anyone=False, verified_senders=[])
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "forbidden_recipient"
    assert wired.reply_kwargs is None
    mod.container_repo.try_consume_daily_email_send.assert_not_called()


@pytest.mark.asyncio
async def test_reply_path_stranger_sender_allowed_with_send_to_anyone(wired):
    wired.row = dict(wired.row, send_to_anyone=True, verified_senders=[])
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "sent"


@pytest.mark.asyncio
async def test_reply_path_allowlisted_sender_allowed_without_send_to_anyone(wired):
    wired.row = dict(wired.row, send_to_anyone=False, verified_senders=["bob@x.y"])
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "sent"


@pytest.mark.asyncio
async def test_reply_path_domain_allowlisted_sender_allowed_without_send_to_anyone(wired):
    wired.row = dict(wired.row, send_to_anyone=False, verified_senders=["@x.y"])
    assert await _send(reply_key=f"{TEAM}/2026#m1") == "sent"


@pytest.mark.asyncio
async def test_front_desk_agent_cannot_send_mail(wired, monkeypatch):
    line = AsyncMock(return_value={"address": "+15551234567", "agent_id": AGENT, "kind": "text"})
    monkeypatch.setattr(mod.agent_email_repo, "text_line_for_owner", line)
    ses = AsyncMock()
    monkeypatch.setattr(mod.ses_email, "send_ses_email", ses)
    assert await _send(to="vendor@x.y") == "front_desk_agent"
    ses.assert_not_awaited()
    assert wired.owner_kwargs is None and wired.reply_kwargs is None
    # another agent on the same workspace still sends
    monkeypatch.setattr(mod.agent_email_repo, "text_line_for_owner", AsyncMock(return_value={"agent_id": "other"}))
    assert await _send(to="vendor@x.y") != "front_desk_agent"
