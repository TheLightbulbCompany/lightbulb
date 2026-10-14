from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from scripts import monday_readout
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
    assert result["funnel"]["flickmeon.ai"]["landing_viewed"]["visitor_ids"] == ["visitor-a"]
    assert result["vercel_visitors"] == 123


def test_render_names_every_metric_and_suppresses_small_base_percentages():
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
    )
    text = render_readout(result)

    assert "Acme, Beta" in text
    assert "Formed Co" in text
    assert "Handoff Co" in text
    assert "Week Four Co" in text
    assert "New Paid, Old Paid" in text
    assert "flickmeon.ai" in text
    assert "%" not in text


def test_hogql_keeps_anonymous_acquisition_visitors_and_filters_workspace_metrics():
    start, end = monday_window(datetime(2026, 10, 14, tzinfo=timezone.utc))
    query = build_posthog_query(start, end, {"org_internal", "org_test"})

    assert "properties.environment = 'prod'" in query
    assert "toString(properties.workspace_id) NOT IN ('org_internal', 'org_test')" in query
    assert "nullIf(toString(distinct_id), '') AS visitor_id" in query
    assert "properties.first_touch_utm_source" in query
    assert "properties.first_touch_referring_domain" in query
    assert "properties.first_touch_source" not in query
    assert "properties.$host IN ('thelightbulb.company', 'www.thelightbulb.company')" in query
    assert (
        "event IN ('landing_viewed', 'setup_cta_clicked', 'signup_cta_clicked', 'signup_started', 'setup_requested')"
        in query
    )
    assert "OR (event NOT IN ('landing_viewed', 'setup_cta_clicked', 'signup_cta_clicked', 'signup_started')" in query
    assert "AND notEmpty(toString(properties.workspace_id))" in query


def test_anonymous_funnel_events_do_not_enter_workspace_metrics():
    start, end = monday_window(datetime(2026, 10, 14, tzinfo=timezone.utc))
    result = summarize(
        [
            {
                "event": "landing_viewed",
                "timestamp": "2026-10-06T08:00:00+00:00",
                "workspace_id": "",
                "visitor_id": "anonymous-visitor",
                "first_touch_utm_source": "newsletter",
            }
        ],
        [],
        {},
        {},
        set(),
        start,
        end,
    )

    assert result["funnel"]["newsletter"]["landing_viewed"]["visitor_ids"] == ["anonymous-visitor"]
    assert result["setup_requests"]["total"]["count"] == 0


def test_post_readout_sends_create_message_request(monkeypatch):
    request = {}

    def fake_request(method, url, *, headers=None, body=None):
        request.update(method=method, url=url, headers=headers, body=body)

    monkeypatch.setenv("MONDAY_READOUT_ROOM_ID", "room-1")
    monkeypatch.setenv("MONDAY_READOUT_BEARER_TOKEN", "token")
    monkeypatch.setattr(monday_readout, "_request", fake_request)

    monday_readout.post_readout("Readout")

    assert request["body"]["text"] == "Readout"
    assert request["body"]["reply_to_message_id"] is None
    assert request["body"]["thread_root_message_id"] is None
    UUID(request["body"]["client_message_id"])
