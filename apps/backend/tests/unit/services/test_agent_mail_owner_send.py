"""Tests for core.services.agent_mail_owner_send.

The load-bearing behaviours here are the ones that failed silently in the
previous incarnation of this feature: the once-only guard being derived from
SERVER state rather than a client label, and the owner-facts rewrite that
stops the agent being told it has no address.
"""

import types
from unittest.mock import AsyncMock

import pytest

from core.services import agent_mail_owner_send as mod


@pytest.fixture
def wired(monkeypatch):
    """Wire every collaborator to a recorder so each test can flip one thing."""
    st = types.SimpleNamespace(
        cap_allows=True,
        cap_calls=0,
        claim_returns=True,
        claimed=False,
        settled=None,
        settle_reason=None,
        first_contact_state="not_started",
        owner_email="owner@example.com",
        company_id="co_1",
        friday_agent_id="agent_friday",
        live_ceo_agent_id="agent_ceo_live",
        live_ceo_calls=0,
        minted="agents@acme-robotics.isol8.co",
        ensure_agent_id=None,
        ensure_reason="mint_failed",
        facts_written=False,
        facts_raises=False,
        ses_calls=0,
        ses_returns=None,
        sent_subject=None,
        sent_to=None,
        sent_from=None,
        sent_headers=None,
        sent_body_text=None,
        sent_body_html=None,
        footer_calls=[],
        footer_text_suffix="",
        footer_html_suffix="",
    )

    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod")

    async def _cap(owner_id, *, day, cap):
        st.cap_calls += 1
        return st.cap_allows

    class _Repo:
        async def get_onboarding_state(self, owner_id):
            return {
                "onboarding_generation": "01G",
                "user_id": owner_id,
                "company_id": st.company_id,
                "friday_agent_id": st.friday_agent_id,
                "first_contact_state": st.first_contact_state,
            }

        async def claim_first_contact(self, user_id, *, onboarding_generation):
            if st.claim_returns:
                st.claimed = True
            return st.claim_returns

        async def settle_first_contact(self, user_id, *, onboarding_generation, state, reason=None):
            st.settled = state
            st.settle_reason = reason

    async def _owner_email(*, org_id, fallback_user_id):
        return st.owner_email

    async def _ensure(owner_id, owner_email):
        return (st.minted, "ok") if st.minted else (None, st.ensure_reason)

    async def _from(owner_id, agent_id, address, agent_name=None):
        st.ensure_agent_id = agent_id
        return f'"Agent · Acme agents" <{address}>'

    st.put_sent = AsyncMock()

    async def _live_ceo(*, owner_id):
        st.live_ceo_calls += 1
        return st.live_ceo_agent_id

    async def _facts(owner_id):
        if st.facts_raises:
            raise RuntimeError("efs down")
        st.facts_written = True

    async def _send(*, from_addr, to, subject, body_text, body_html, headers, tenant_name=None):
        st.ses_calls += 1
        st.sent_tenant = tenant_name
        st.sent_subject = subject
        st.sent_to = to
        st.sent_from = from_addr
        st.sent_headers = headers
        st.sent_body_text = body_text
        st.sent_body_html = body_html
        return st.ses_returns

    async def _append_footer(**kwargs):
        st.footer_calls.append(kwargs)
        rendered_html = kwargs["body_html"]
        if rendered_html is not None:
            rendered_html += st.footer_html_suffix
        return kwargs["body_text"] + st.footer_text_suffix, rendered_html

    monkeypatch.setattr(mod.container_repo, "try_consume_daily_email_send", _cap)

    # Signature MUST mirror the real PaperclipRepo (table_name is required).
    # This fake previously took zero args, which meant the suite passed only
    # while the production call was `PaperclipRepo()` — a TypeError that
    # crashed every send in prod and could not be caught here, because the
    # mock had encoded the wrong arity as the contract.
    def _repo_factory(*, table_name):
        assert table_name == "paperclip-companies"
        return _Repo()

    monkeypatch.setattr(mod, "PaperclipRepo", _repo_factory)
    monkeypatch.setattr(mod, "lookup_owner_email", _owner_email)
    monkeypatch.setattr(mod, "ensure_team_address", _ensure)
    monkeypatch.setattr(mod, "from_header", _from)
    st.agent_name = "Agent"
    monkeypatch.setattr(mod, "resolve_agent_name", AsyncMock(side_effect=lambda **_: st.agent_name))
    monkeypatch.setattr(mod, "append_agent_mail_footer", _append_footer)
    monkeypatch.setattr(mod.agent_mail_inbox_repo, "put_sent", st.put_sent)
    monkeypatch.setattr(mod, "resolve_apex_ceo_agent_id", _live_ceo, raising=False)
    monkeypatch.setattr(mod, "write_owner_facts", _facts)
    monkeypatch.setattr(mod.ses_email, "send_ses_reply", _send)

    st.ses_returns = "msg-1"
    return st


async def _send(purpose="first_contact"):
    return await mod.send_to_owner(owner_id="user_a", purpose=purpose, subject="Hello", body_text="body")


@pytest.mark.asyncio
async def test_disabled_when_domain_unset(monkeypatch, wired):
    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "")
    assert await _send() == "disabled"
    assert wired.ses_calls == 0


@pytest.mark.asyncio
async def test_happy_path_sends_and_settles_sent(wired):
    assert await _send() == "sent"
    assert wired.ses_calls == 1
    assert wired.settled == "sent"
    assert wired.sent_from == '"Agent · Acme agents" <agents@acme-robotics.isol8.co>'
    kw = wired.put_sent.await_args.kwargs
    assert (kw["address"], kw["message_id"], kw["text"]) == ("agents@acme-robotics.isol8.co", "msg-1", "body")
    assert wired.sent_to == "owner@example.com"
    assert wired.sent_headers == {"Auto-Submitted": "auto-generated"}


@pytest.mark.asyncio
async def test_owner_directed_agent_mail_applies_attribution_footer(wired):
    wired.footer_text_suffix = "\n\nattribution"
    wired.footer_html_suffix = "<p>attribution</p>"

    result = await mod.send_to_owner(
        owner_id="user_a",
        purpose="routine",
        subject="Hello",
        body_text="body",
        body_html="<p>body</p>",
        agent_id="agent_atlas",
    )

    assert result == "sent"
    assert wired.footer_calls == [
        {
            "owner_id": "user_a",
            "agent_id": "agent_atlas",
            "address": "agents@acme-robotics.isol8.co",
            "body_text": "body",
            "body_html": "<p>body</p>",
            "agent_name": "Agent",
        }
    ]
    assert wired.sent_body_text == "body\n\nattribution"
    assert wired.sent_body_html == "<p>body</p><p>attribution</p>"
    assert wired.put_sent.await_args.kwargs["text"] == "body\n\nattribution"


@pytest.mark.asyncio
async def test_an_unknown_agent_is_never_recorded_as_sender(wired):
    wired.agent_name = None
    assert await _send() == "sent"
    wired.put_sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_routine_label_still_claims_the_first_contact_slot(wired):
    """The LABEL must not select the guard — policy comes from state."""
    assert await _send(purpose="routine") == "sent"
    assert wired.claimed is True


@pytest.mark.asyncio
async def test_system_notice_never_claims_the_first_contact_slot(wired):
    """A platform-originated notice (the outbound-paused complaint mail) sends
    while first contact is still owed, but must not consume the slot — the
    agent's own hello would otherwise be permanently marked `sent`."""
    wired.first_contact_state = "not_started"
    result = await mod.send_to_owner(
        owner_id="user_a",
        purpose="routine",
        subject="Outbound email paused",
        body_text="body",
        system_notice=True,
    )
    assert result == "sent"
    assert wired.ses_calls == 1
    assert wired.claimed is False
    assert wired.settled is None


@pytest.mark.asyncio
async def test_system_notice_does_not_apply_attribution_footer(wired):
    result = await mod.send_to_owner(
        owner_id="user_a",
        purpose="routine",
        subject="Outbound email paused",
        body_text="platform body",
        body_html="<p>platform body</p>",
        system_notice=True,
    )

    assert result == "sent"
    assert wired.footer_calls == []
    assert wired.sent_body_text == "platform body"
    assert wired.sent_body_html == "<p>platform body</p>"


@pytest.mark.asyncio
async def test_first_contact_label_confers_nothing_once_sent(wired):
    """A send labelled first_contact after the slot is taken is NOT blocked —
    that is the routine case, and it must go out."""
    wired.first_contact_state = "sent"
    assert await _send(purpose="first_contact") == "sent"
    assert wired.claimed is False
    assert wired.ses_calls == 1


@pytest.mark.asyncio
async def test_losing_the_claim_reports_already_sent_without_sending(wired):
    wired.claim_returns = False
    assert await _send() == "already_sent"
    assert wired.ses_calls == 0


@pytest.mark.asyncio
async def test_failed_state_is_retried(wired):
    """A prior failure must stay claimable, or the first failure is terminal."""
    wired.first_contact_state = "failed"
    assert await _send() == "sent"
    assert wired.claimed is True


@pytest.mark.asyncio
async def test_facts_are_rewritten_after_the_mint(wired):
    """THE deadlock guard: a stale null email_address makes SKILL.md tell
    Friday she has no address, and she declines to send."""
    await _send()
    assert wired.facts_written is True


@pytest.mark.asyncio
async def test_facts_failure_does_not_block_the_send(wired):
    wired.facts_raises = True
    assert await _send() == "sent"
    assert wired.ses_calls == 1


@pytest.mark.asyncio
async def test_cap_exceeded_releases_the_claim_rather_than_wedging_it(wired):
    """The cap is charged AFTER the claim, so a cap hit must settle `failed`.
    `sending` is neither re-claimable nor nudged — returning without settling
    would wedge first contact permanently."""
    wired.cap_allows = False
    assert await _send() == "rate_limited"
    assert wired.ses_calls == 0
    assert wired.settled == "failed"
    assert wired.settle_reason == "daily_cap_exhausted"


@pytest.mark.asyncio
async def test_system_notice_ignores_the_daily_cap(wired):
    """The cap fences a LOOPING AGENT. The outbound-paused notice has to reach
    the owner precisely when that fence has tripped, so a system notice must
    not even consult it."""
    wired.cap_allows = False
    result = await mod.send_to_owner(
        owner_id="user_a",
        purpose="routine",
        subject="Outbound email paused",
        body_text="body",
        system_notice=True,
    )
    assert result == "sent"
    assert wired.cap_calls == 0
    assert wired.ses_calls == 1


@pytest.mark.asyncio
async def test_ses_failure_settles_failed(wired):
    wired.ses_returns = None
    assert await _send() == "error"
    assert wired.settled == "failed"
    assert wired.settle_reason == "ses_send_failed"


@pytest.mark.asyncio
async def test_unreachable_paperclip_fails_rather_than_minting_a_junk_slug(wired):
    """company_slug_repo has no delete path, so a fallback name would freeze
    permanently. (The company-name→slug resolution itself lives inside
    ensure_team_address, tested in test_agent_email.py; here we only verify
    that owner_send propagates the reason and fails closed.)"""
    wired.minted = None
    wired.ensure_reason = "slug_allocation_failed"
    assert await _send() == "no_address"
    assert wired.settled == "failed"
    assert wired.settle_reason == "slug_allocation_failed"
    assert wired.ses_calls == 0


@pytest.mark.asyncio
async def test_missing_owner_email_fails_closed(wired):
    wired.owner_email = None
    assert await _send() == "error"
    assert wired.ses_calls == 0


@pytest.mark.asyncio
async def test_non_prod_subject_is_env_prefixed(monkeypatch, wired):
    """Dev and prod both send from an isol8.co address into a real inbox."""
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev")
    await _send()
    assert wired.sent_subject == "[dev] Hello"


@pytest.mark.asyncio
async def test_prod_subject_is_untouched(wired):
    await _send()
    assert wired.sent_subject == "Hello"


# --- Review fixes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_purpose_is_rejected_in_the_SERVICE(wired):
    """The routines caller invokes send_to_owner IN-PROCESS, bypassing the
    router's check. `purpose` is a metric dimension, so an unvalidated value
    is unbounded CloudWatch cardinality."""
    result = await mod.send_to_owner(owner_id="user_a", purpose="whatever-i-like", subject="s", body_text="b")
    assert result == "error"
    assert wired.ses_calls == 0
    assert wired.claimed is False


@pytest.mark.asyncio
async def test_already_sent_does_NOT_burn_daily_budget(wired):
    """Charging on entry would let an agent re-calling first_contact exhaust
    the day on no-ops and starve a legitimate routine send."""
    consumed = []

    async def _cap(owner_id, *, day, cap):
        consumed.append(day)
        return True

    import core.repositories.container_repo as cr

    object.__setattr__(cr, "try_consume_daily_email_send", _cap)
    mod.container_repo.try_consume_daily_email_send = _cap

    wired.claim_returns = False
    assert await _send() == "already_sent"
    assert consumed == []


@pytest.mark.asyncio
async def test_a_real_send_DOES_burn_daily_budget(wired):
    consumed = []

    async def _cap(owner_id, *, day, cap):
        consumed.append(day)
        return True

    mod.container_repo.try_consume_daily_email_send = _cap

    assert await _send() == "sent"
    assert len(consumed) == 1


@pytest.mark.asyncio
async def test_unset_environment_still_prefixes(monkeypatch, wired):
    """Fail toward marking. A misconfigured deploy must not send mail that
    looks identical to production."""
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "")
    await _send()
    assert wired.sent_subject == "[non-prod] Hello"


@pytest.mark.asyncio
async def test_send_to_owner_uses_calling_agent_for_from_address(wired):
    await mod.send_to_owner(owner_id="user_1", purpose="routine", subject="s", body_text="b", agent_id="agent_atlas")
    assert wired.ensure_agent_id == "agent_atlas"


@pytest.mark.asyncio
async def test_send_to_owner_defaults_to_live_root_ceo(wired):
    await mod.send_to_owner(owner_id="user_1", purpose="routine", subject="s", body_text="b")
    assert wired.ensure_agent_id == "agent_ceo_live"


@pytest.mark.asyncio
async def test_send_to_owner_falls_back_to_stored_ceo_identity(wired):
    wired.live_ceo_agent_id = None
    await mod.send_to_owner(owner_id="user_1", purpose="routine", subject="s", body_text="b")
    assert wired.ensure_agent_id == "agent_friday"


def test_daily_cap_is_fifty():
    assert mod.DAILY_SEND_CAP == 50


# --- send_to_member (T14: the teammate welcome; later the away email) --------------------------


@pytest.fixture
def member_wired(wired, monkeypatch):
    wired.row = {"address": wired.minted, "verified_senders": ["owner@example.com"]}
    wired.update = AsyncMock(return_value={})
    monkeypatch.setattr(mod.agent_email_repo, "get_by_address", AsyncMock(side_effect=lambda _a: wired.row))
    monkeypatch.setattr(mod.agent_email_repo, "update_settings", wired.update)
    wired.tenant = AsyncMock(return_value="tenant-org_a")
    monkeypatch.setattr(mod, "ensure_tenant", wired.tenant)
    return wired


async def _send_member(email="Sam@Example.com"):
    return await mod.send_to_member(owner_id="org_a", user_id="user_s", email=email, subject="Hi", body_text="body")


@pytest.mark.asyncio
async def test_member_send_reaches_the_member_and_lets_them_reply(member_wired):
    assert await _send_member() == "sent"
    assert member_wired.sent_to == "sam@example.com"
    assert member_wired.sent_headers == {"Auto-Submitted": "auto-generated"}
    member_wired.update.assert_awaited_once_with(
        member_wired.minted, verified_senders=["owner@example.com", "sam@example.com"]
    )
    # Never the owner's first-contact slot: a teammate's mail is not the owner's hello.
    assert member_wired.claimed is False and member_wired.settled is None


@pytest.mark.asyncio
async def test_member_send_does_not_relist_a_known_sender(member_wired):
    member_wired.row["verified_senders"] = ["sam@example.com"]
    assert await _send_member() == "sent"
    member_wired.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_member_send_shares_the_daily_cap(member_wired):
    member_wired.cap_allows = False
    assert await _send_member() == "rate_limited"
    assert member_wired.ses_calls == 0


@pytest.mark.asyncio
async def test_member_send_before_provisioning_sends_nothing(member_wired):
    member_wired.company_id = None
    assert await _send_member() == "error"
    assert member_wired.ses_calls == 0 and member_wired.cap_calls == 0


@pytest.mark.asyncio
async def test_member_send_goes_through_the_owners_ses_tenant(member_wired):
    assert await _send_member() == "sent"
    member_wired.tenant.assert_awaited_once_with("org_a")
    assert member_wired.sent_tenant == "tenant-org_a"


@pytest.mark.asyncio
async def test_member_send_honours_the_complaint_pause(member_wired):
    member_wired.row["outbound_disabled"] = "complaint"
    assert await _send_member() == "outbound_disabled"
    assert member_wired.ses_calls == 0
    member_wired.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_apex_ceo_degrades_to_none_when_paperclip_fetch_fails(monkeypatch):
    """Moved from agent_export: a Paperclip outage yields None, never a failed send."""
    import core.services.paperclip_openclaw_pairing as pairing

    async def _boom(**_):
        raise RuntimeError("down")

    monkeypatch.setattr(pairing, "fetch_paperclip_agents_for_owner", _boom)
    assert await mod.resolve_apex_ceo_agent_id(owner_id="user_abc") is None

    async def _agents(**_):
        return [{"id": "div", "role": "ceo", "reportsTo": "fri"}, {"id": "fri", "role": "ceo", "reportsTo": None}]

    monkeypatch.setattr(pairing, "fetch_paperclip_agents_for_owner", _agents)
    assert await mod.resolve_apex_ceo_agent_id(owner_id="user_abc") == "fri"
