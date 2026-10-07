"""curator.get_curator_user_id resolves the marketplace curator's Clerk id."""

from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.constants import ISOL8_CURATOR_EMAIL
from core.services import curator


@pytest.fixture(autouse=True)
def _clear_cache():
    curator._curator_user_id = None
    yield
    curator._curator_user_id = None


def test_candidates_collapse_to_one_address_on_the_current_domain(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.isol8.co")
    assert curator._candidate_emails() == (ISOL8_CURATOR_EMAIL,)


def test_candidates_lead_with_the_current_apex_after_a_domain_move(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.example.ai")
    assert curator._candidate_emails() == ("admin@example.ai", ISOL8_CURATOR_EMAIL)


@pytest.mark.asyncio
async def test_falls_back_to_the_historical_address(monkeypatch):
    """The Clerk account's email lags the domain move; the gallery must not go
    empty in the gap."""
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://example.ai")
    lookup = AsyncMock(side_effect=[None, {"id": "user_curator"}])
    with patch.object(curator.clerk_admin, "find_user_by_email", new=lookup):
        assert await curator.get_curator_user_id() == "user_curator"
    assert [c.args[0] for c in lookup.await_args_list] == ["admin@example.ai", ISOL8_CURATOR_EMAIL]


@pytest.mark.asyncio
async def test_miss_is_not_cached_and_hit_is(monkeypatch):
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://isol8.co")
    lookup = AsyncMock(side_effect=[None, {"id": "user_curator"}])
    with patch.object(curator.clerk_admin, "find_user_by_email", new=lookup):
        assert await curator.get_curator_user_id() is None
        assert await curator.get_curator_user_id() == "user_curator"
        assert await curator.get_curator_user_id() == "user_curator"
    assert lookup.await_count == 2  # third call served from cache
