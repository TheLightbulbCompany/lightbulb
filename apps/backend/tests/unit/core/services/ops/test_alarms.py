from unittest.mock import MagicMock, patch

import pytest

from core.services.ops import alarms


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("isol8-dev-P9-alb-5xx-rate", "critical"),
        ("isol8-prod-P12-backend-liveness", "critical"),
        ("isol8-dev-W28-alb-unhealthy-hosts", "warning"),
        ("isol8-prod-W5-gateway-connection-drop", "warning"),
        ("isol8-dev-composite-rollup", "unknown"),  # no P/W token
        ("isol8-dev-Perf1-not-a-priority", "unknown"),  # P must be P<digits>-
        ("TargetTracking-service/foo", "unknown"),  # non-isol8 alarm
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_severity_parses_alarm_name_convention(name, expected):
    assert alarms._severity(name) == expected


def test_console_url_is_region_aware_and_encodes_name():
    with patch.object(alarms.settings, "AWS_REGION", "us-east-1"):
        url = alarms._console_url("isol8-dev-P9-alb 5xx/rate")
    assert url == (
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home"
        "?region=us-east-1#alarmsV2:alarm/isol8-dev-P9-alb%205xx%2Frate"
    )
    assert alarms._console_url(None) is None


@pytest.mark.asyncio
async def test_alarms_in_alarm_maps_fields():
    fake = MagicMock()
    fake.describe_alarms.return_value = {
        "MetricAlarms": [
            {
                "AlarmName": "isol8-dev-P9-alb-5xx-rate",
                "MetricName": "HTTPCode_Target_5XX_Count",
                "AlarmDescription": "ALB 5xx rate above threshold",
                "StateReason": "Threshold Crossed: 3 datapoints",
            },
            {
                # Empty description must surface as None (not "") so the UI
                # falls back to the raw name.
                "AlarmName": "isol8-dev-W28-alb-unhealthy-hosts",
                "MetricName": "UnHealthyHostCount",
                "AlarmDescription": "",
            },
        ],
        "CompositeAlarms": [
            {
                "AlarmName": "isol8-dev-platform-rollup",
                "AlarmDescription": "Any platform alarm firing",
                "StateReason": "child in ALARM",
            }
        ],
    }
    with patch.object(alarms, "_cw", return_value=fake), patch.object(alarms.settings, "AWS_REGION", "us-east-1"):
        rows = await alarms.alarms_in_alarm()

    by_name = {r["name"]: r for r in rows}
    p9 = by_name["isol8-dev-P9-alb-5xx-rate"]
    assert p9["metric"] == "HTTPCode_Target_5XX_Count"
    assert p9["description"] == "ALB 5xx rate above threshold"
    assert p9["state_reason"] == "Threshold Crossed: 3 datapoints"
    assert p9["severity"] == "critical"
    assert p9["console_url"].endswith("#alarmsV2:alarm/isol8-dev-P9-alb-5xx-rate")

    w28 = by_name["isol8-dev-W28-alb-unhealthy-hosts"]
    assert w28["description"] is None
    assert w28["state_reason"] is None
    assert w28["severity"] == "warning"

    rollup = by_name["isol8-dev-platform-rollup"]
    assert rollup["metric"] is None  # composite alarms have no single metric
    assert rollup["severity"] == "unknown"
    assert rollup["console_url"].endswith("isol8-dev-platform-rollup")
