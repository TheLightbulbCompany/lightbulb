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


GOG_PIPELINE = """
steps:
  - id: fetch-mail
    command: >
      gog.gmail.search --query 'newer_than:1d'
  - id: triage
    command: inbox categorize --json
  - id: wrapped
    command: >
      openclaw.invoke --tool message --action send
"""


def test_detect_direct_cli_tool_head():
    """A direct CLI invocation (gog.gmail.search, inbox list) has no --tool flag;
    the structural YAML walk must still surface its tool head."""
    p_dir = "pipelines"
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / p_dir / "mail.lobster"
        p.parent.mkdir(parents=True)
        p.write_text(GOG_PIPELINE, encoding="utf-8")
        hits = detect_in_tree(Path(d))
    tools = {h.value for h in hits if h.kind == "tool"}
    assert "gog" in tools  # dotted head → leading plugin segment
    assert "inbox" in tools  # bare command head
    assert "message" in tools  # --tool flag still caught
    # openclaw.invoke wrapper itself is plumbing, not a tool dep.
    assert "openclaw" not in tools


def test_detect_skips_plumbing_heads():
    import tempfile
    from pathlib import Path

    # The structural walk skips exec/curl command heads but still catches the
    # stripe host referenced inside the shell command.
    text = "steps:\n  - command: exec --json --shell 'curl https://api.stripe.com/v1'\n"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "pipelines" / "p.lobster"
        p.parent.mkdir(parents=True)
        p.write_text(text, encoding="utf-8")
        tree_hits = detect_in_tree(Path(d))
    tools = {h.value for h in tree_hits if h.kind == "tool"}
    assert "exec" not in tools and "curl" not in tools
    assert any(h.kind == "host" and h.value == "api.stripe.com" for h in tree_hits)


def test_detect_shell_chained_tool_head(tmp_path):
    """A tool after a `cd x && ` or `a; b` prefix must still be detected."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "chain.lobster"
    p.parent.mkdir(parents=True)
    p.write_text(
        "steps:\n  - command: cd /app && gog.gmail.search --query x\n  - command: 'echo hi; inbox list'\n",
        encoding="utf-8",
    )
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    assert "gog" in tools
    assert "inbox" in tools
    assert "cd" not in tools and "echo" not in tools


def test_detect_list_valued_command(tmp_path):
    """A command expressed as a YAML sequence of command lines is scanned."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "seq.lobster"
    p.parent.mkdir(parents=True)
    p.write_text(
        "steps:\n  - command:\n      - gog.gmail.search --query x\n      - inbox list\n",
        encoding="utf-8",
    )
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    assert "gog" in tools
    assert "inbox" in tools


def test_detect_npx_uvx_package():
    """npx/uvx invocations capture the runtime-resolved package (scoped + version
    stripped), tagged cli_pkg — not as the plumbing head `npx`."""
    hits = detect_in_text("npx -y @stripe/mcp@1.2 --tools=all\nuvx some-tool\nnpx opencli", "s")
    pkgs = {h.value for h in hits if h.kind == "cli_pkg"}
    assert pkgs == {"@stripe/mcp", "some-tool", "opencli"}
    # `npx`/`uvx` themselves are not emitted as tools.
    assert not any(h.kind == "tool" and h.value in ("npx", "uvx") for h in hits)


def test_detect_npx_in_mcp_command(tmp_path):
    """A pipeline declaring an MCP via `npx -y <pkg>` surfaces the package."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "mcp.lobster"
    p.parent.mkdir(parents=True)
    p.write_text("steps:\n  - command: npx -y @notionhq/notion-mcp-server\n", encoding="utf-8")
    hits = detect_in_tree(Path(tmp_path))
    assert any(h.kind == "cli_pkg" and h.value == "@notionhq/notion-mcp-server" for h in hits)


def test_detect_in_tree_recurses_nested_scripts(tmp_path):
    """Tool/env/host references in nested script files must be detected."""
    from pathlib import Path

    nested = tmp_path / "scripts" / "lib" / "fetch.sh"
    nested.parent.mkdir(parents=True)
    nested.write_text(
        "curl https://production.plaid.com/x -d $PLAID_SECRET\n",
        encoding="utf-8",
    )
    hits = detect_in_tree(Path(tmp_path))
    assert any(h.kind == "host" and h.value == "production.plaid.com" for h in hits)
    assert any(h.kind == "env" and h.value == "PLAID_SECRET" for h in hits)


def test_identify_service_host_and_env():
    assert identify_service(Hit("host", "production.plaid.com", "s")) == ("plaid", "Plaid")
    assert identify_service(Hit("host", "api.perplexity.ai", "s")) == ("perplexity", "Perplexity")
    assert identify_service(Hit("host", "api.stripe.com", "s")) == ("stripe", "Stripe")
    assert identify_service(Hit("env", "PLAID_SECRET", "s")) == ("plaid", "Plaid")
    assert identify_service(Hit("env", "PERPLEXITY_API_KEY", "s")) == ("perplexity", "Perplexity")
    assert identify_service(Hit("host", "example.invalid", "s")) is None


def test_quickbooks_marketing_host_is_not_a_requirement():
    """A bare intuit.com suffix matched marketing/docs links as phantom QBO
    deps; only the API subdomains should resolve."""
    assert identify_service(Hit("host", "quickbooks.api.intuit.com", "s")) == ("quickbooks", "QuickBooks")
    assert identify_service(Hit("host", "turbotax.intuit.com", "s")) is None
    assert identify_service(Hit("host", "quickbooks.intuit.com", "s")) is None
