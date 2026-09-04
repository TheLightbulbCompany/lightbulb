import pytest


@pytest.fixture(autouse=True)
def _no_real_trial_first_response(monkeypatch):
    """Every chat.final the reader loop sees now schedules
    ``capture_trial_first_agent_response``, which reads the billing row. Keep
    it out of DynamoDB for every gateway test; the tests that care about it
    patch the same name themselves (innermost patch wins)."""
    from unittest.mock import AsyncMock

    mock = AsyncMock()
    monkeypatch.setattr("core.gateway.connection_pool.capture_trial_first_agent_response", mock)
    return mock
