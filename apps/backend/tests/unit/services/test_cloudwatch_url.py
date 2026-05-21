"""Tests for the CloudWatch Insights deep-link builder.

Pure URL assembly — no SDK calls. Just verify the URL contains
the user_id, level, log group, and time range so the AWS Console
opens with the right query.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENVIRONMENT", "dev")
os.environ.setdefault("AWS_REGION", "us-east-1")


def test_url_targets_us_east_1_console():
    from core.services.cloudwatch_url import build_insights_url

    url = build_insights_url(
        user_id="user_test",
        start="2026-04-20T00:00:00Z",
        end="2026-04-21T00:00:00Z",
        level="ERROR",
    )

    assert url.startswith("https://us-east-1.console.aws.amazon.com/cloudwatch/home")
    assert "region=us-east-1" in url


def test_url_includes_user_id_in_query():
    from core.services.cloudwatch_url import build_insights_url

    url = build_insights_url(
        user_id="user_xyz_abc",
        start="2026-04-20T00:00:00Z",
        end="2026-04-21T00:00:00Z",
        level="ERROR",
    )
    assert "user_xyz_abc" in url


def test_url_includes_level_filter():
    from core.services.cloudwatch_url import build_insights_url

    url = build_insights_url(
        user_id="u1",
        start="2026-04-20T00:00:00Z",
        end="2026-04-21T00:00:00Z",
        level="WARN",
    )
    assert "WARN" in url


def test_url_points_at_correct_log_group():
    """Same Phase A fix — log group is /ecs/isol8-{env}."""
    from core.services.cloudwatch_url import build_insights_url

    url = build_insights_url(
        user_id="u1",
        start="2026-04-20T00:00:00Z",
        end="2026-04-21T00:00:00Z",
        level="ERROR",
    )
    # URL-encoded /ecs/isol8-dev → %2Fecs%2Fisol8-dev
    assert "%2Fecs%2Fisol8-dev" in url


def test_url_uses_logs_insights_path():
    from core.services.cloudwatch_url import build_insights_url

    url = build_insights_url(
        user_id="u1",
        start="2026-04-20T00:00:00Z",
        end="2026-04-21T00:00:00Z",
        level="ERROR",
    )
    assert "#logsV2:logs-insights" in url


def test_default_level_is_error():
    from core.services.cloudwatch_url import build_insights_url

    url_default = build_insights_url(
        user_id="u1",
        start="2026-04-20T00:00:00Z",
        end="2026-04-21T00:00:00Z",
    )
    url_explicit = build_insights_url(
        user_id="u1",
        start="2026-04-20T00:00:00Z",
        end="2026-04-21T00:00:00Z",
        level="ERROR",
    )
    assert url_default == url_explicit
