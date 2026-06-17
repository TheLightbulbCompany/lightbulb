from core.services.marketplace.requirements import Disposition
from core.services.marketplace.requirements_classify import classify_hits
from core.services.marketplace.requirements_detect import Hit


def _classify(hits, **kw):
    base = dict(bundled_mcps=set(), bundled_skills=set(), config_skills={}, config_mcps={})
    base.update(kw)
    return {r.key: r for r in classify_hits(hits, **base)}


def test_bundled_tool_is_included():
    reqs = _classify([Hit("tool", "llm-task", "p")])
    assert reqs["llm-task"].disposition == Disposition.INCLUDED
    assert reqs["llm-task"].kind == "bundled"


def test_gog_is_unavailable_gated():
    reqs = _classify([Hit("tool", "gog", "p")])
    assert reqs["gog"].disposition == Disposition.UNAVAILABLE
    assert "Bedrock" in (reqs["gog"].reason or "")


def test_slack_tool_is_connect_channel():
    reqs = _classify([Hit("tool", "slack", "p")])
    assert reqs["slack"].disposition == Disposition.CONNECT
    assert reqs["slack"].kind == "channel"


def test_unknown_external_api_is_byo():
    reqs = _classify([Hit("host", "production.plaid.com", "p"), Hit("env", "PLAID_SECRET", "p")])
    assert reqs["plaid"].disposition == Disposition.BYO
    assert "PLAID_SECRET" in reqs["plaid"].env


def test_phantom_skill_is_unavailable():
    reqs = _classify([Hit("tool", "stripe-api", "p")])
    assert reqs["stripe-api"].disposition == Disposition.UNAVAILABLE
    assert "implementation" in (reqs["stripe-api"].reason or "")


def test_stripe_env_mismatch_is_byo_with_reason():
    # The Stripe integration wires an MCP keyed on STRIPE_SECRET_KEY; an agent
    # referencing STRIPE_API_KEY won't be satisfied by connecting it.
    reqs = _classify([Hit("env", "STRIPE_API_KEY", "p")])
    assert "stripe" in reqs
    assert reqs["stripe"].disposition == Disposition.BYO
    assert "mismatch" in (reqs["stripe"].reason or "").lower()


def test_bundled_selection_marks_row():
    reqs = _classify([Hit("host", "production.plaid.com", "p")], bundled_mcps={"plaid"})
    assert reqs["plaid"].bundled is True
