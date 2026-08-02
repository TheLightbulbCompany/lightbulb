import pytest

from core.services.credentials.registry import _load
from core.services.marketplace.requirements import Disposition
from core.services.marketplace.requirements_classify import classify_hits
from core.services.marketplace.requirements_detect import Hit


@pytest.fixture(autouse=True)
def _fresh_registry():
    """These tests classify against the REAL providers.yaml — that coupling is
    deliberate (it's what catches the catalog drifting from this module, e.g.
    the Stripe hosted-MCP cutover). Clear the registry's lru_cache on both
    sides so ambient cache state primed elsewhere in the suite can't change
    what "real" means here: the stripe expectations here were order-dependent
    (passing in some full-suite orders, failing in isolation and in CI) until
    this pin."""
    _load.cache_clear()
    yield
    _load.cache_clear()


def _classify(hits, **kw):
    base = dict(bundled_mcps=set(), bundled_skills=set(), config_skills={}, config_mcps={})
    base.update(kw)
    return {r.key: r for r in classify_hits(hits, **base)}


def test_bundled_tool_is_included():
    reqs = _classify([Hit("tool", "summarize", "p")])
    assert reqs["summarize"].disposition == Disposition.INCLUDED
    assert reqs["summarize"].kind == "bundled"


def test_engine_internals_are_suppressed():
    """Lobster engine plumbing + the fictional fast-io skill never reach the
    ledger, however they were detected (tool head, or a skills/ dir)."""
    reqs = _classify(
        [
            Hit("tool", "state", "p"),
            Hit("tool", "llm-task", "p"),
            Hit("tool", "lobster", "p"),
            Hit("tool", "fast-io", "p"),
        ],
        workspace_skills={"fast-io"},
    )
    assert reqs == {}


def test_gog_tool_is_byo():
    reqs = _classify([Hit("tool", "gog", "p")])
    assert reqs["google"].disposition == Disposition.BYO
    assert "own" in (reqs["google"].reason or "").lower()


def test_google_tool_is_byo():
    """A bare `--tool google` resolves to the byo-required google provider → BYO."""
    reqs = _classify([Hit("tool", "google", "p")])
    assert reqs["google"].disposition == Disposition.BYO


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
    # The Stripe integration connects a hosted MCP over OAuth and sets no env
    # vars; an agent referencing STRIPE_API_KEY won't be satisfied by
    # connecting it.
    reqs = _classify([Hit("env", "STRIPE_API_KEY", "p")])
    assert "stripe" in reqs
    assert reqs["stripe"].disposition == Disposition.BYO
    assert "mismatch" in (reqs["stripe"].reason or "").lower()


def test_bundled_selection_marks_row():
    reqs = _classify([Hit("host", "production.plaid.com", "p")], bundled_mcps={"plaid"})
    assert reqs["plaid"].bundled is True


def test_config_mcp_included_only_when_bundled():
    """The agent's own MCP ships ONLY if the seller bundled it (it rides the
    slice). Bundled → INCLUDED; declared-but-not-bundled → UNAVAILABLE, since
    the buyer never receives it."""
    cfg = {"my-custom-mcp": {"command": "node"}}
    bundled = _classify([Hit("tool", "my-custom-mcp", "p")], config_mcps=cfg, bundled_mcps={"my-custom-mcp"})
    assert bundled["my-custom-mcp"].disposition == Disposition.INCLUDED
    assert bundled["my-custom-mcp"].kind == "mcp"

    unbundled = _classify([Hit("tool", "my-custom-mcp", "p")], config_mcps=cfg)
    assert unbundled["my-custom-mcp"].disposition == Disposition.UNAVAILABLE
    assert "not bundled" in (unbundled["my-custom-mcp"].reason or "")


def test_stripe_mismatch_is_byo_regardless_of_hit_order():
    """The wiring-mismatch downgrade must not depend on whether the host or the
    env hit is seen first."""
    host = Hit("host", "api.stripe.com", "p")
    env = Hit("env", "STRIPE_API_KEY", "p")
    for order in ([host, env], [env, host]):
        reqs = _classify(order)
        assert reqs["stripe"].disposition == Disposition.BYO, order
        assert "mismatch" in (reqs["stripe"].reason or "").lower(), order


def test_stripe_formerly_wired_key_is_byo_after_hosted_cutover():
    """STRIPE_SECRET_KEY was the wired key of the old stdio MCP; the hosted
    OAuth MCP sets no env vars at all, so an agent scripting against it is no
    longer satisfied by connecting — honest answer is BYO."""
    reqs = _classify([Hit("host", "api.stripe.com", "p"), Hit("env", "STRIPE_SECRET_KEY", "p")])
    assert reqs["stripe"].disposition == Disposition.BYO
    assert "mismatch" in (reqs["stripe"].reason or "").lower()


def test_stripe_host_only_stays_connect():
    """Host/tool usage without env references is exactly what the hosted MCP
    serves — stays CONNECT (the downgrade must not over-reach)."""
    reqs = _classify([Hit("host", "api.stripe.com", "p")])
    assert reqs["stripe"].disposition == Disposition.CONNECT


def test_tool_named_after_registry_provider_resolves_connect():
    """A bare `--tool stripe` resolves to the registry provider via the tool
    path (not the host/env path) and is connectable."""
    reqs = _classify([Hit("tool", "stripe", "p")])
    assert reqs["stripe"].disposition == Disposition.CONNECT
    assert reqs["stripe"].kind == "mcp"


def test_cli_pkg_is_included_runtime_resolved():
    """An npx/uvx package re-resolves on the buyer's container, so it's INCLUDED
    ( rides along for free) — NOT phantom 'no implementation found'."""
    reqs = _classify([Hit("cli_pkg", "@notionhq/notion-mcp-server", "p")])
    row = reqs["@notionhq/notion-mcp-server"]
    assert row.disposition == Disposition.INCLUDED
    assert row.kind == "cli"
    assert "npx" in (row.note or "")


def test_gh_cli_bin_maps_to_github_provider():
    """`gh issue list` → head `gh`, which is github's declared CLI bin, so it
    resolves to the connectable github provider — not a phantom skill."""
    reqs = _classify([Hit("tool", "gh", "p")])
    assert "github" in reqs
    assert reqs["github"].disposition == Disposition.CONNECT
    assert "gh" not in reqs  # not classified as a bare phantom


def test_workspace_skill_is_included():
    """A pipeline `--tool custom-skill` with skills/custom-skill.md present
    auto-ships → INCLUDED, not phantom."""
    reqs = _classify([Hit("tool", "custom-skill", "p")], workspace_skills={"custom-skill"})
    assert reqs["custom-skill"].disposition == Disposition.INCLUDED
    assert reqs["custom-skill"].kind == "skill"


def test_allow_bundled_skill_is_included():
    """An image-bundled skill exposed via skills.allowBundled is present on the
    buyer's image → INCLUDED."""
    reqs = _classify([Hit("tool", "search", "p")], allow_bundled_skills={"search"})
    assert reqs["search"].disposition == Disposition.INCLUDED


def test_entries_skill_unbundled_is_unavailable():
    """A skills.entries skill the seller didn't select won't ship → UNAVAILABLE."""
    reqs = _classify([Hit("tool", "jira", "p")], config_skills={"jira": {"apiKey": ""}})
    assert reqs["jira"].disposition == Disposition.UNAVAILABLE
    assert "not bundled" in (reqs["jira"].reason or "")


def test_bundled_provider_mcp_seeds_ledger_row():
    """A bundled registry-provider MCP (in the slice) appears in the ledger even
    when no pipeline references it — matching the deploy credential gate."""
    reqs = _classify([], bundled_mcps={"stripe"})
    assert "stripe" in reqs
    assert reqs["stripe"].disposition == Disposition.CONNECT
    assert reqs["stripe"].bundled is True


def test_wiring_mismatch_applies_to_tool_detected_provider():
    """`--tool stripe` (kind=mcp) + a stray $STRIPE_API_KEY must still downgrade
    to BYO, not just host/env-detected (external_api) rows."""
    reqs = _classify([Hit("tool", "stripe", "p"), Hit("env", "STRIPE_API_KEY", "p")])
    assert reqs["stripe"].disposition == Disposition.BYO
    assert "mismatch" in (reqs["stripe"].reason or "").lower()


def test_whatsapp_is_not_a_backend_channel():
    """whatsapp isn't a backend-provisionable channel; it must not classify as a
    connect channel (would tell buyers to connect it where it can't be)."""
    reqs = _classify([Hit("tool", "whatsapp", "p")])
    # Falls through to phantom skill (no implementation), NOT connect/channel.
    assert reqs["whatsapp"].disposition != Disposition.CONNECT
    assert reqs["whatsapp"].kind != "channel"
