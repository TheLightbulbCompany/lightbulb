from unittest.mock import AsyncMock, patch

from scripts import list_super_agent_optouts


async def test_lists_exactly_the_owners_whose_row_says_false(capsys):
    rows = [
        {"owner_id": "org_off", "super_agent": False},
        {"owner_id": "org_default"},
        {"owner_id": "org_on", "super_agent": True},
        {"owner_id": "org_string", "super_agent": "false"},  # only a real False opts out
    ]
    with patch("core.repositories.container_repo.scan_all", AsyncMock(return_value=rows)):
        assert await list_super_agent_optouts.main([]) == 0
    assert capsys.readouterr().out == "org_off\n"
