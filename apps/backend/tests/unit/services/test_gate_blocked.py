import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services.ops import gate_blocked  # noqa: E402


@pytest.mark.asyncio
async def test_last_7d_queries_each_code_with_full_dimension_set(monkeypatch):
    monkeypatch.setattr("core.config.settings.ENVIRONMENT", "prod")
    cw = MagicMock()
    cw.get_metric_data.return_value = {
        "MetricDataResults": [
            {"Id": "c_trial_expired", "Values": [4.0, 3.0], "StatusCode": "Complete"},
            {"Id": "c_oauth_required", "Values": [], "StatusCode": "Complete"},
            {"Id": "c_subscription_required", "Values": [1.0], "StatusCode": "Complete"},
        ]
    }
    with patch("core.services.ops.gate_blocked._client", return_value=cw):
        out = await gate_blocked.last_7d()

    assert out == [{"code": "trial_expired", "n": 7}, {"code": "subscription_required", "n": 1}]
    queries = cw.get_metric_data.call_args.kwargs["MetricDataQueries"]
    assert len(queries) == len(gate_blocked.GATE_CODES)
    q = next(q for q in queries if q["Id"] == "c_trial_expired")
    assert q["MetricStat"]["Metric"] == {
        "Namespace": "Isol8",
        "MetricName": "provision.gate.blocked",
        "Dimensions": [
            {"Name": "env", "Value": "prod"},
            {"Name": "service", "Value": "isol8-backend"},
            {"Name": "code", "Value": "trial_expired"},
        ],
    }
    assert q["MetricStat"]["Stat"] == "Sum" and q["MetricStat"]["Period"] == 86400


@pytest.mark.asyncio
async def test_last_7d_raises_when_a_result_status_is_not_complete():
    """Forbidden/PartialData must never read as a silent zero."""
    cw = MagicMock()
    cw.get_metric_data.return_value = {
        "MetricDataResults": [
            {"Id": "c_trial_expired", "StatusCode": "Forbidden", "Messages": [{"Code": "Forbidden"}], "Values": []},
        ]
    }
    with patch("core.services.ops.gate_blocked._client", return_value=cw):
        with pytest.raises(RuntimeError, match="Forbidden"):
            await gate_blocked.last_7d()
