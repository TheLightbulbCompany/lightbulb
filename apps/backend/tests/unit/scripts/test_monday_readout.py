from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.monday_readout import (
    build_posthog_query,
    monday_window,
    render_readout,
    summarize,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "monday_readout.json"


def _fixture():
    return json.loads(FIXTURE.read_text())


def test_monday_window_is_previous_complete_utc_week():
    start, end = monday_window(datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
    assert start.isoformat() == "2026-10-05T00:00:00+00:00"
    assert end.isoformat() == "2026-10-12T00:00:00+00:00"


def test_fixture_metrics_exclude_internal_and_cross_check_memberships():
    data = _fixture()
    start, end = monday_window(datetime.fromisoformat(data["now"]))
    result = summarize(
        data["events"],
        data["invoices"],
        data["organizations"],
        data["membership_counts"],
        set(data["excluded"]),
        start,
        end,
        vercel_visitors=123,
    )

    assert result["setup_requests"]["total"]["workspace_ids"] == ["org_setup_a", "org_setup_b"]
    assert result["teams_formed"]["workspace_ids"] == ["org_formed"]
    assert result["teams_formed"]["membership_mismatches"] == ["org_solo"]
    assert result["first_handoff"]["workspace_ids"] == ["org_handoff"]
    assert result["week4_active"]["workspace_ids"] == ["org_week4"]
    assert result["paying_at_list"]["all_time"]["workspace_ids"] == ["org_paid_new", "org_paid_old"]
    assert result["paying_at_list"]["new_this_week"]["workspace_ids"] == ["org_paid_new"]
    assert result["funnel"]["flickmeon.ai"]["landing_viewed"]["count"] == 1
    assert result["vercel_visitors"] == 123


def test_render_names_every_metric_and_suppresses_small_base_percentages():
    data = _fixture()
    start, end = monday_window(datetime.fromisoformat(data["now"]))
    result = summarize(
        data["events"], data["invoices"], data["organizations"], data["membership_counts"], set(data["excluded"]), start, end
    )
    text = render_readout(result)

    assert "Acme, Beta" in text
    assert "Formed Co" in text
    assert "Handoff Co" in text
    assert "Week Four Co" in text
    assert "New Paid, Old Paid" in text
    assert "flickmeon.ai" in text
    assert "%" not in text


def test_hogql_enforces_prod_exclusions_workspace_ids_and_client_hosts():
    start, end = monday_window(datetime(2026, 10, 14, tzinfo=timezone.utc))
    query = build_posthog_query(start, end, {"org_internal", "org_test"})

    assert "properties.environment = 'prod'" in query
    assert "toString(properties.workspace_id) NOT IN ('org_internal', 'org_test')" in query
    assert "properties.$host IN ('thelightbulb.company', 'www.thelightbulb.company')" in query
    assert "distinct_id" not in query
