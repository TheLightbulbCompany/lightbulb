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
            {"Id": "c_trial_expired", "Values": [4.0, 3.0]},
            {"Id": "c_oauth_required", "Values": []},
            {"Id": "c_subscription_required", "Values": [1.0]},
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
    assert q["MetricStat"]["Stat"] == "Sum" and q["MetricStat"]["Period"] == 604800
