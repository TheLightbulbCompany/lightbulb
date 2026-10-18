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
    resolve_organizations,
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


def test_paying_at_list_excludes_invoices_at_or_after_end():
    start, end = monday_window(datetime(2026, 10, 14, tzinfo=timezone.utc))
    invoices = [
        {
            "workspace_id": "org_before_end",
            "amount_paid": 100,
            "billing_reason": "subscription_cycle",
            "paid_at": "2026-10-11T23:59:59+00:00",
        },
        {
            "workspace_id": "org_at_end",
            "amount_paid": 100,
            "billing_reason": "subscription_cycle",
            "paid_at": end.isoformat(),
        },
        {
            "workspace_id": "org_after_end",
            "amount_paid": 100,
            "billing_reason": "subscription_cycle",
            "paid_at": "2026-10-12T00:00:01+00:00",
        },
    ]

    result = summarize([], invoices, {}, {}, set(), start, end)

    assert result["paying_at_list"]["all_time"]["workspace_ids"] == ["org_before_end"]
    assert result["paying_at_list"]["new_this_week"]["workspace_ids"] == ["org_before_end"]


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
    query = build_posthog_query(start, end, {"org_internal", "org_test"}, {"user_staff"})

    assert "properties.environment = 'prod'" in query
    assert "toString(properties.workspace_id) NOT IN ('org_internal', 'org_test')" in query
    assert "toString(distinct_id) NOT IN ('user_staff')" in query
    assert "nullIf(toString(distinct_id), '') AS actor_id" in query
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
    assert "ORDER BY timestamp, event_id" in query
    assert "LIMIT" not in query


def test_resolve_organizations_uses_exact_internal_slugs_each_run():
    class FakeClerk:
        def organizations(self):
            return [
                {"id": f"org_{index}", "name": slug, "slug": slug}
                for index, slug in enumerate(monday_readout.INTERNAL_WORKSPACE_SLUGS)
            ] + [{"id": "org_customer", "name": "Lightbulb Customer", "slug": "lightbulb-customer"}]

    organizations, excluded = resolve_organizations(FakeClerk(), {"user_staff"})

    assert excluded == {f"org_{index}" for index, _ in enumerate(monday_readout.INTERNAL_WORKSPACE_SLUGS)}
    assert "org_customer" not in excluded
    assert organizations["org_customer"] == "Lightbulb Customer"


def test_staff_authors_do_not_count_as_second_person_handoff_or_week4_activity():
    start, end = monday_window(datetime(2026, 10, 14, tzinfo=timezone.utc))
    events = [
        {"event": "workspace_created", "timestamp": "2026-10-04T00:00:00+00:00", "workspace_id": "org_team", "actor_id": "user_customer"},
        {"event": "team_formed", "timestamp": "2026-10-04T01:00:00+00:00", "workspace_id": "org_team", "actor_id": "user_staff"},
        {"event": "workspace_created", "timestamp": "2026-09-30T00:00:00+00:00", "workspace_id": "org_handoff", "actor_id": "user_customer"},
        {"event": "handoff_completed", "timestamp": "2026-10-01T00:00:00+00:00", "workspace_id": "org_handoff", "actor_id": "user_staff"},
        {"event": "team_formed", "timestamp": "2026-09-07T00:00:00+00:00", "workspace_id": "org_week4", "actor_id": "user_customer"},
        {"event": "room_message_sent", "timestamp": "2026-10-02T00:00:00+00:00", "workspace_id": "org_week4", "actor_id": "user_staff"},
    ]

    result = summarize(events, [], {}, {"org_team": 1}, set(), start, end, staff_ids={"user_staff"})

    assert result["teams_formed"]["count"] == 0
    assert result["first_handoff"]["count"] == 0
    assert result["week4_active"]["count"] == 0


def test_fetch_events_pages_until_posthog_returns_a_short_page(monkeypatch):
    queries = []
    pages = [
        {"columns": ["event_id"], "results": [["1"], ["2"]]},
        {"columns": ["event_id"], "results": [["3"]]},
    ]

    def fake_request(method, url, *, headers=None, body=None):
        queries.append(body["query"]["query"])
        return pages.pop(0)

    monkeypatch.setattr(monday_readout, "_request", fake_request)

    assert monday_readout.fetch_events("key", "SELECT event_id FROM events", page_size=2) == [
        {"event_id": "1"},
        {"event_id": "2"},
        {"event_id": "3"},
    ]
    assert queries == [
        "SELECT event_id FROM events\nLIMIT 2 OFFSET 0",
        "SELECT event_id FROM events\nLIMIT 2 OFFSET 2",
    ]


def test_fetch_events_refuses_to_publish_when_bounded_pagination_is_exhausted(monkeypatch):
    def fake_request(method, url, *, headers=None, body=None):
        return {"columns": ["event_id"], "results": [["1"], ["2"]]}

    monkeypatch.setattr(monday_readout, "_request", fake_request)

    try:
        monday_readout.fetch_events("key", "SELECT event_id FROM events", page_size=2, max_pages=2)
    except RuntimeError as error:
        assert "pagination limit" in str(error)
    else:
        raise AssertionError("expected a full bounded result set to fail closed")


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
