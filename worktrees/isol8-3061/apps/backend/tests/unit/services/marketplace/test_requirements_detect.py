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


def test_detect_scans_root_lobster_and_skills(tmp_path):
    """Real agents (e.g. Tally) keep .lobster pipelines at the workspace ROOT and
    skills under skills/ — both must be scanned, not just pipelines/."""
    from pathlib import Path

    (tmp_path / "daily-feed.lobster").write_text("steps:\n  - command: gog.gmail.search\n", encoding="utf-8")
    skill = tmp_path / "skills" / "fetcher"
    skill.mkdir(parents=True)
    (skill / "run.sh").write_text("curl https://api.perplexity.ai/x $PERPLEXITY_API_KEY\n", encoding="utf-8")
    hits = detect_in_tree(Path(tmp_path))
    tools = {h.value for h in hits if h.kind == "tool"}
    assert "gog" in tools  # root-level .lobster
    assert any(h.kind == "host" and h.value == "api.perplexity.ai" for h in hits)  # skills/


def test_scan_files_scopes_to_shipped(tmp_path):
    """With an explicit shipped-file list, a file NOT in it (e.g. one the export
    filter dropped) is never scanned — no phantom deps from undelivered files."""
    from pathlib import Path

    (tmp_path / "ships.lobster").write_text("steps:\n  - command: inbox list\n", encoding="utf-8")
    (tmp_path / "dropped.lobster").write_text("steps:\n  - command: gog.gmail.search\n", encoding="utf-8")
    hits = detect_in_tree(Path(tmp_path), scan_files=["./ships.lobster"])
    tools = {h.value for h in hits if h.kind == "tool"}
    assert "inbox" in tools
    assert "gog" not in tools  # dropped.lobster not in the shipped list → not scanned


def test_inline_node_script_body_yields_no_keyword_tools(tmp_path):
    """An inline `node -e '…'` script body is a single quoted arg, not a chain of
    shell commands — its `;`-separated JS statements must NOT surface as tools."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "script.lobster"
    p.parent.mkdir(parents=True)
    p.write_text(
        "steps:\n  - command: >-\n      node -e 'const q=t;const b=c;process.stdout.write(x)'\n",
        encoding="utf-8",
    )
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    # None of the JS keywords / var names inside the quoted body leak as tools.
    assert not (tools & {"const", "b", "process", "x"})
    # `node` is plumbing, never a tool dep.
    assert "node" not in tools


def test_inline_script_keeps_legit_dotted_head(tmp_path):
    """A direct-CLI dotted head (gog.gmail.search) still resolves to its plugin."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "mail.lobster"
    p.parent.mkdir(parents=True)
    p.write_text("steps:\n  - command: gog.gmail.search --query foo\n", encoding="utf-8")
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    assert "gog" in tools


def test_pipeline_state_dsl_verb_not_a_tool(tmp_path):
    """Lobster state.get/state.set DSL verbs are engine plumbing, not tool deps."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "queue.lobster"
    p.parent.mkdir(parents=True)
    p.write_text(
        "steps:\n  - pipeline: state.get queue-size\n  - command: state.set queue-size 0\n",
        encoding="utf-8",
    )
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    assert "state" not in tools


def test_inline_script_keeps_chained_bare_head(tmp_path):
    """A bare command head after a `cd x && ` prefix still resolves."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "chain.lobster"
    p.parent.mkdir(parents=True)
    p.write_text("steps:\n  - command: cd /app && inbox list\n", encoding="utf-8")
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    assert "inbox" in tools
    assert "cd" not in tools


def test_quote_aware_split_ignores_separators_inside_quotes(tmp_path):
    """A `;` inside a quoted arg is not a shell separator, so the token after it
    (`b`) must not become a tool head; the real pipe to `jq` still splits."""
    from pathlib import Path

    p = tmp_path / "pipelines" / "quoted.lobster"
    p.parent.mkdir(parents=True)
    p.write_text("steps:\n  - command: echo 'a;b' | jq\n", encoding="utf-8")
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    assert "b" not in tools
    assert "jq" not in tools  # jq is plumbing
    assert "echo" not in tools


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


def test_js_helper_not_scraped_for_cli_but_host_kept(tmp_path):
    """Tool/cli scraping is scoped to pipeline files. A .js helper's internal
    `npx tsc`/`npx vitest` are implementation, not buyer deps — but a real
    integration host referenced in it still surfaces."""
    from pathlib import Path

    p = tmp_path / "scripts" / "build.js"
    p.parent.mkdir(parents=True)
    p.write_text(
        "execSync('npx tsc'); execSync('npx vitest run');\nfetch('https://api.stripe.com/v1/charges');\n",
        encoding="utf-8",
    )
    hits = detect_in_tree(Path(tmp_path))
    assert not any(h.kind == "cli_pkg" for h in hits)
    assert any(h.kind == "host" and "stripe.com" in h.value for h in hits)


def test_pipeline_npx_still_detected_after_scoping(tmp_path):
    """An npx package declared in a .lobster pipeline step still rides along."""
    from pathlib import Path

    p = tmp_path / "workflows" / "x.lobster"
    p.parent.mkdir(parents=True)
    p.write_text("steps:\n  - command: npx -y @notionhq/notion-mcp-server\n", encoding="utf-8")
    hits = detect_in_tree(Path(tmp_path))
    assert any(h.kind == "cli_pkg" and h.value == "@notionhq/notion-mcp-server" for h in hits)


def test_shell_builtins_are_not_tools(tmp_path):
    """Coreutils / shell builtins (printf, grep, sort, …) are plumbing, never
    buyer-facing deps, even when they head a pipeline segment."""
    from pathlib import Path

    p = tmp_path / "workflows" / "x.lobster"
    p.parent.mkdir(parents=True)
    p.write_text(
        "steps:\n  - command: printf '%s' x | node scripts/s.js\n  - command: grep foo bar.txt ; sort | uniq\n",
        encoding="utf-8",
    )
    tools = {h.value for h in detect_in_tree(Path(tmp_path)) if h.kind == "tool"}
    assert not ({"printf", "grep", "sort", "uniq"} & tools)
