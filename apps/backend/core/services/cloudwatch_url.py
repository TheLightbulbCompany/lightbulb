"""CloudWatch Logs Insights deep-link builder.

Pure string assembly — no AWS SDK call. Used by /admin/users/{user_id}/cloudwatch-url
to give admins a "Open in CloudWatch" button that lands on the AWS Console
with a pre-filled query filtering to the specified user_id.

Useful when the inline log viewer (cloudwatch_logs.filter_user_logs) hits
its 1 MB / 10k event response cap and the admin needs cross-user or
longer-range search power that the inline viewer can't provide.
"""

import urllib.parse

from core.config import settings


def _backend_log_group() -> str:
    env = settings.ENVIRONMENT or "dev"
    return f"/ecs/lightbulb-{env}"


def _encode_insights_param(value: str) -> str:
    """CloudWatch Insights URLs use a custom encoding — `~` for tildes,
    `*` for percent, then URL-encode the rest. The query parameter values
    look like `~(end~'2026-04-21T00:00:00Z*20...)`.

    For our purposes, we URL-encode each interpolated value (timestamps,
    user_id) so reserved chars don't break the query string."""
    return urllib.parse.quote(value, safe="")


def build_insights_url(*, user_id: str, start: str, end: str, level: str = "ERROR") -> str:
    """Construct an AWS Console CWL Insights URL pre-filtered to user_id.

    `start` / `end` are ISO-8601 timestamps; `level` is the structured-log
    level filter ("ERROR" by default, can be "WARN", "INFO", etc.).

    The Insights query:
        fields @timestamp, @message
        | filter user_id = "{user_id}" and level = "{level}"
        | sort @timestamp desc
        | limit 100
    """
    region = settings.AWS_REGION
    log_group = _backend_log_group()

    query = (
        f'fields @timestamp, @message | filter user_id = "{user_id}" '
        f'and level = "{level}" | sort @timestamp desc | limit 100'
    )

    # Insights URL params use a custom delimiter scheme. Encode each piece
    # then assemble. The format mirrors what AWS Console produces when you
    # share a query.
    detail = (
        f"~(end~'{_encode_insights_param(end)}"
        f"~start~'{_encode_insights_param(start)}"
        f"~timeType~'ABSOLUTE~tz~'UTC"
        f"~editorString~'{_encode_insights_param(query)}"
        f"~source~(~'{_encode_insights_param(log_group)}))"
    )

    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home"
        f"?region={region}#logsV2:logs-insights?queryDetail={detail}"
    )
