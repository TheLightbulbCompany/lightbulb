"""Every per-agent Teams route resolves the agent through the workspace fence (404 outside the workspace)."""

from routers.teams import agents as agents_mod


def test_every_per_agent_route_is_fenced():
    """Each ``/agents/{agent_id}...`` route depends on ``_workspace_agent`` or is gone (410)."""
    fenced = {
        r.endpoint.__name__
        for r in agents_mod.router.routes
        if "{agent_id}" in getattr(r, "path", "")
        and any(d.call is agents_mod._workspace_agent for d in r.dependant.dependencies)
    }
    per_agent = {r.endpoint.__name__ for r in agents_mod.router.routes if "{agent_id}" in getattr(r, "path", "")}
    assert per_agent - fenced == {"agent_heartbeat_gone"}
