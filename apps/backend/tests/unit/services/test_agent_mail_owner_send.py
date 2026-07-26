"""Tests for core.services.agent_mail_owner_send.

The load-bearing behaviours here are the ones that failed silently in the
previous incarnation of this feature: the once-only guard being derived from
SERVER state rather than a client label, and the owner-facts rewrite that
stops the agent being told it has no address.
"""

import types

import pytest

from core.services import agent_mail_owner_send as mod


@pytest.fixture
def wired(monkeypatch):
    """Wire every collaborator to a recorder so each test can flip one thing."""
    st = types.SimpleNamespace(
        cap_allows=True,
        claim_returns=True,
        claimed=False,
        settled=None,
        settle_reason=None,
        first_contact_state="not_started",
        company_name="Acme Robotics",
        owner_email="owner@example.com",
        company_id="co_1",
        friday_agent_id="agent_friday",
        slug="acme-robotics",
        minted="friday@acme-robotics.isol8.co",
        facts_written=False,
        facts_raises=False,
        ses_calls=0,
        ses_returns=None,
        sent_subject=None,
        sent_to=None,
        sent_from=None,
        slug_name_seen=None,
    )

    monkeypatch.setattr(mod.settings, "AGENT_EMAIL_DOMAIN", "isol8.co")
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod")

    async def _cap(owner_id, *, day, cap):
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

    async def _company_name(company_id):
        return st.company_name

    async def _owner_email(*, org_id, fallback_user_id):
        return st.owner_email

    async def _slug(*, owner_id, name):
        st.slug_name_seen = name
        return st.slug

    async def _mint(*, owner_id, agent_id, agent_name, owner_email, slug):
        return st.minted

    async def _facts(owner_id):
        if st.facts_raises:
            raise RuntimeError("efs down")
        st.facts_written = True

    async def _send(*, from_addr, to, subject, body_text, body_html, headers):
        st.ses_calls += 1
        st.sent_subject = subject
        st.sent_to = to
        st.sent_from = from_addr
        return st.ses_returns

    monkeypatch.setattr(mod.container_repo, "try_consume_daily_email_send", _cap)
    monkeypatch.setattr(mod, "PaperclipRepo", lambda: _Repo())
    monkeypatch.setattr(mod, "_resolve_company_name", _company_name)
    monkeypatch.setattr(mod, "lookup_owner_email", _owner_email)
    monkeypatch.setattr(mod, "allocate_company_slug", _slug)
    monkeypatch.setattr(mod, "mint_agent_email_address", _mint)
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
    assert wired.sent_from == "friday@acme-robotics.isol8.co"
    assert wired.sent_to == "owner@example.com"


@pytest.mark.asyncio
async def test_slug_derives_from_the_LIVE_company_name(wired):
    """Not the provisioning placeholder — that is the whole point of minting
    late."""
    wired.company_name = "Renamed By Friday"
    await _send()
    assert wired.slug_name_seen == "Renamed By Friday"


@pytest.mark.asyncio
async def test_routine_label_still_claims_the_first_contact_slot(wired):
    """The LABEL must not select the guard — policy comes from state."""
    assert await _send(purpose="routine") == "sent"
    assert wired.claimed is True


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
async def test_ses_failure_settles_failed(wired):
    wired.ses_returns = None
    assert await _send() == "error"
    assert wired.settled == "failed"
    assert wired.settle_reason == "ses_send_failed"


@pytest.mark.asyncio
async def test_unreachable_paperclip_fails_rather_than_minting_a_junk_slug(wired):
    """company_slug_repo has no delete path, so a fallback name would freeze
    permanently."""
    wired.company_name = None
    assert await _send() == "error"
    assert wired.settled == "failed"
    assert wired.settle_reason == "company_name_unavailable"
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
