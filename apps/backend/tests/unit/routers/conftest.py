from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _no_live_openrouter_allowance(monkeypatch):
    """GET /billing/account reads a capped owner's allowance from OpenRouter; keep router tests offline."""
    monkeypatch.setattr(
        "routers.billing.openrouter_keys", SimpleNamespace(owner_allowance=AsyncMock(return_value=None))
    )
