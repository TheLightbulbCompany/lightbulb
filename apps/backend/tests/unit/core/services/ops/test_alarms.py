from unittest.mock import MagicMock, patch

import pytest

from core.services.ops import alarms


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("lightbulb-dev-P9-alb-5xx-rate", "critical"),
        ("lightbulb-prod-P12-backend-liveness", "critical"),
        ("lightbulb-dev-W28-alb-unhealthy-hosts", "warning"),
        ("lightbulb-prod-W5-gateway-connection-drop", "warning"),
        ("lightbulb-dev-composite-rollup", "unknown"),  # no P/W token
        ("lightbulb-dev-Perf1-not-a-priority", "unknown"),  # P must be P<digits>-
        ("TargetTracking-service/foo", "unknown"),  # not one of ours
        # The retired prefix is NOT the convention any more: every alarm is
        # named by resourcePrefix() = `lightbulb-{env}-`. Pinned so a revert
        # to the isol8-era regex fails here.
        ("isol8-dev-P9-alb-5xx-rate", "unknown"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_severity_parses_alarm_name_convention(name, expected):
    assert alarms._severity(name) == expected


def test_console_url_is_region_aware_and_encodes_name():
    with patch.object(alarms.settings, "AWS_REGION", "us-east-1"):
        url = alarms._console_url("lightbulb-dev-P9-alb 5xx/rate")
    assert url == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
        "?region=us-east-1#alarmsV2:alarm/lightbulb-dev-P9-alb%205xx%2Frate"
    )
    assert alarms._console_url(None) is None


@pytest.mark.asyncio
async def test_alarms_in_alarm_maps_fields():
    fake = MagicMock()
    fake.describe_alarms.return_value = {
        "MetricAlarms": [
            {
                "AlarmName": "lightbulb-dev-P9-alb-5xx-rate",
                "MetricName": "HTTPCode_Target_5XX_Count",
                "AlarmDescription": "ALB 5xx rate above threshold",
                "StateReason": "Threshold Crossed: 3 datapoints",
            },
            {
                # Empty description must surface as None (not "") so the UI
                # falls back to the raw name.
                "AlarmName": "lightbulb-dev-W28-alb-unhealthy-hosts",
                "MetricName": "UnHealthyHostCount",
                "AlarmDescription": "",
            },
        ],
        "CompositeAlarms": [
            {
                "AlarmName": "lightbulb-dev-platform-rollup",
                "AlarmDescription": "Any platform alarm firing",
                "StateReason": "child in ALARM",
            }
        ],
    }
    with patch.object(alarms, "_cw", return_value=fake), patch.object(alarms.settings, "AWS_REGION", "us-east-1"):
        rows = await alarms.alarms_in_alarm()

    by_name = {r["name"]: r for r in rows}
    p9 = by_name["lightbulb-dev-P9-alb-5xx-rate"]
    assert p9["metric"] == "HTTPCode_Target_5XX_Count"
    assert p9["description"] == "ALB 5xx rate above threshold"
    assert p9["state_reason"] == "Threshold Crossed: 3 datapoints"
    assert p9["severity"] == "critical"
    assert p9["console_url"].endswith("#alarmsV2:alarm/lightbulb-dev-P9-alb-5xx-rate")

    w28 = by_name["lightbulb-dev-W28-alb-unhealthy-hosts"]
    assert w28["description"] is None
    assert w28["state_reason"] is None
    assert w28["severity"] == "warning"

    rollup = by_name["lightbulb-dev-platform-rollup"]
    assert rollup["metric"] is None  # composite alarms have no single metric
    assert rollup["severity"] == "unknown"
    assert rollup["console_url"].endswith("lightbulb-dev-platform-rollup")
