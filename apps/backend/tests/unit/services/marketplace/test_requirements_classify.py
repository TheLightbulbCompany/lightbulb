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


def test_config_declared_mcp_is_included_not_phantom():
    """A tool resolving to the agent's OWN configured MCP server (declared in
    its openclaw.json mcp.servers) ships in the artifact — it must not be
    classified UNAVAILABLE 'no implementation found' and block publish."""
    reqs = _classify([Hit("tool", "my-custom-mcp", "p")], config_mcps={"my-custom-mcp": {"command": "node"}})
    assert reqs["my-custom-mcp"].disposition == Disposition.INCLUDED
    assert reqs["my-custom-mcp"].kind == "mcp"


def test_stripe_mismatch_is_byo_regardless_of_hit_order():
    """The wiring-mismatch downgrade must not depend on whether the host or the
    env hit is seen first."""
    host = Hit("host", "api.stripe.com", "p")
    env = Hit("env", "STRIPE_API_KEY", "p")
    for order in ([host, env], [env, host]):
        reqs = _classify(order)
        assert reqs["stripe"].disposition == Disposition.BYO, order
        assert "mismatch" in (reqs["stripe"].reason or "").lower(), order


def test_stripe_correct_env_stays_connect():
    """Referencing the env var the integration's MCP actually wires
    (STRIPE_SECRET_KEY) is connectable, not a mismatch."""
    reqs = _classify([Hit("host", "api.stripe.com", "p"), Hit("env", "STRIPE_SECRET_KEY", "p")])
    assert reqs["stripe"].disposition == Disposition.CONNECT


def test_whatsapp_is_not_a_backend_channel():
    """whatsapp isn't a backend-provisionable channel; it must not classify as a
    connect channel (would tell buyers to connect it where it can't be)."""
    reqs = _classify([Hit("tool", "whatsapp", "p")])
    # Falls through to phantom skill (no implementation), NOT connect/channel.
    assert reqs["whatsapp"].disposition != Disposition.CONNECT
    assert reqs["whatsapp"].kind != "channel"
