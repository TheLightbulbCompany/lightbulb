"""Teams router tests: stub the agent company-fence dependency by default.

Route-logic tests mock the admin client for the call under test only, so a live
fence (which fetches the issue/approval/agent first) would fail every one of
them. The fences are covered on their own: the dependency unit tests and
``test_teams_company_fences``'s structural check. A test that exercises a
fence end to end opts back in with ``@pytest.mark.real_fences``.
"""

import pytest

from main import app
from routers.teams import agents
from routers.teams.agents import SafeId


def _pass_agent(agent_id: SafeId) -> str:
    return agent_id


def pytest_configure(config):
    config.addinivalue_line("markers", "real_fences: keep the Teams company-fence dependencies live")


@pytest.fixture(autouse=True)
def _stub_company_fences(request):
    if request.node.get_closest_marker("real_fences"):
        yield
        return
    stubs = {agents._company_agent: _pass_agent}
    saved = {k: app.dependency_overrides.get(k) for k in stubs}
    app.dependency_overrides.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                app.dependency_overrides.pop(k, None)
            else:
                app.dependency_overrides[k] = v
