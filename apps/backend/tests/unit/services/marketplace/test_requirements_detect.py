from core.services.marketplace.requirements_detect import (
    Hit,
    detect_in_text,
    detect_in_tree,
    identify_service,
)

DAILY = r"""
  - id: stripe-feed
    command: >
      openclaw.invoke --tool stripe-api --action list-charges
  - id: bank-feed
    command: >
      exec --json --shell 'curl -s -X POST "https://production.plaid.com/transactions/get" -d "{\"secret\":\"$PLAID_SECRET\"}"'
"""


def test_detect_tool_invocations():
    hits = detect_in_text(DAILY, "pipeline:daily-feed")
    tools = {h.value for h in hits if h.kind == "tool"}
    assert "stripe-api" in tools


def test_detect_env_and_host():
    hits = detect_in_text(DAILY, "pipeline:daily-feed")
    assert any(h.kind == "env" and h.value == "PLAID_SECRET" for h in hits)
    assert any(h.kind == "host" and h.value == "production.plaid.com" for h in hits)


def test_ignores_lowercase_and_short_env():
    hits = detect_in_text("echo $x $AB $GOOD_KEY", "s")
    envs = {h.value for h in hits if h.kind == "env"}
    assert envs == {"GOOD_KEY"}


def test_detect_in_tree_reads_pipelines(tmp_path):
    p = tmp_path / "pipelines" / "daily-feed.lobster"
    p.parent.mkdir(parents=True)
    p.write_text(DAILY, encoding="utf-8")
    hits = detect_in_tree(tmp_path)
    assert any(h.kind == "tool" and h.value == "stripe-api" for h in hits)
    assert all(h.source == "daily-feed.lobster" for h in hits)


def test_identify_service_host_and_env():
    assert identify_service(Hit("host", "production.plaid.com", "s")) == ("plaid", "Plaid")
    assert identify_service(Hit("host", "api.perplexity.ai", "s")) == ("perplexity", "Perplexity")
    assert identify_service(Hit("host", "api.stripe.com", "s")) == ("stripe", "Stripe")
    assert identify_service(Hit("env", "PLAID_SECRET", "s")) == ("plaid", "Plaid")
    assert identify_service(Hit("env", "PERPLEXITY_API_KEY", "s")) == ("perplexity", "Perplexity")
    assert identify_service(Hit("host", "example.invalid", "s")) is None
