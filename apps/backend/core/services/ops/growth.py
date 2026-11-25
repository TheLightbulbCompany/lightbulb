"""Date and HogQL helpers shared with the Spark metrics (core.services.ops.spark_metrics)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def _hogql_filter(environment: str) -> str:
    """The shared filter (09-analytics §6): this backend's own environment only (dev and prod
    share one PostHog project), and on prod only the product hosts -- never the launch site
    or a dev host. Server events carry no ``$host`` and pass the host test."""
    name = "prod" if environment.strip().lower() in ("prod", "production") else (environment.strip() or "local")
    clause = f"properties.environment = '{name}'"
    if name == "prod":
        clause += (
            " and (coalesce(properties.$host, '') = ''"
            " or properties.$host in ('thelightbulb.company', 'www.thelightbulb.company'))"
        )
    return clause


def _monday(d: date) -> date:
    """The Monday opening the ISO week `d` falls in."""
    return d - timedelta(days=d.weekday())


def _weeks(now: datetime, n: int) -> list[str]:
    """The last n Monday buckets, oldest first, ending with the current (partial) week."""
    monday = (now - timedelta(days=now.weekday())).date()
    return [(monday - timedelta(weeks=i)).isoformat() for i in range(n - 1, -1, -1)]


def _parse_ts(v) -> datetime | None:
    """Parse a DynamoDB ISO string (Z / +00:00) into an aware UTC datetime, so
    comparisons never mix naive and aware values. None in -> None out."""
    if v is None:
        return None
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00").replace(" ", "T"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _utc_date(dt: datetime) -> date:
    """The UTC calendar date — the definitions count *UTC* days."""
    return dt.astimezone(timezone.utc).date()
