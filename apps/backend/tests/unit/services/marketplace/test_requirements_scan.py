from core.services.marketplace.requirements import Disposition, requirements_scan


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_tally_shaped_pipeline_classifies_each_dependency(tmp_path):
    _write(
        tmp_path,
        "pipelines/daily-feed.lobster",
        r"""
      - command: openclaw.invoke --tool stripe-api --action list-charges
      - command: openclaw.invoke --tool gog --action sheets-write
      - command: openclaw.invoke --tool slack --action post
      - command: openclaw.invoke --tool llm-task --action json
      - command: exec --shell 'curl https://production.plaid.com/x -d "$PLAID_SECRET"'
      - command: exec --shell 'curl https://api.perplexity.ai/x -H "$PERPLEXITY_API_KEY"'
    """,
    )
    reqs, incomplete = requirements_scan({}, tmp_path)
    assert incomplete is False
    d = {r.key: r.disposition for r in reqs}
    assert "llm-task" not in d  # engine-internal pipeline verb — suppressed, not a buyer dep
    assert d["slack"] == Disposition.CONNECT
    # `gog` resolves to the google platform-app provider → CONNECT, keyed under "google".
    assert d["google"] == Disposition.CONNECT
    assert d["stripe-api"] == Disposition.UNAVAILABLE  # phantom skill
    assert d["plaid"] == Disposition.BYO
    assert d["perplexity"] == Disposition.BYO


def test_scan_orders_worst_first(tmp_path):
    _write(
        tmp_path,
        "pipelines/p.lobster",
        "--tool summarize ; --tool gog ; curl https://production.plaid.com $PLAID_SECRET",
    )
    reqs, _ = requirements_scan({}, tmp_path)
    order = [r.disposition.value for r in reqs]
    assert order == sorted(order, key=lambda d: {"unavailable": 0, "byo": 1, "connect": 2, "included": 3}[d])


def test_declared_skill_unbundled_surfaces_as_unavailable(tmp_path):
    """A skill on the agent's agents.list[].skills that no pipeline --tool's, and
    isn't bundled/present, must still surface (UNAVAILABLE), not silently ship."""
    reqs, _ = requirements_scan({"skills": {"entries": {"jira": {}}}}, tmp_path, declared_skills={"jira"})
    by_key = {r.key: r for r in reqs}
    assert "jira" in by_key
    assert by_key["jira"].disposition == Disposition.UNAVAILABLE


def test_declared_skill_bundled_is_included(tmp_path):
    reqs, _ = requirements_scan(
        {"skills": {"entries": {"jira": {}}}},
        tmp_path,
        declared_skills={"jira"},
        bundled_skills={"jira"},
    )
    assert {r.key: r.disposition for r in reqs}["jira"] == Disposition.INCLUDED


def test_scan_never_raises_on_garbage(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "x.lobster").write_bytes(b"\xff\xfe not utf8 \x00 --tool foo")
    reqs, incomplete = requirements_scan({}, tmp_path)
    assert isinstance(reqs, list)
    assert incomplete is False  # decode errors are replaced, not raised
