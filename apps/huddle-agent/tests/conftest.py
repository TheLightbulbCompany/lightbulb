"""Until Task 1's live.py lands on this branch, a stand-in `huddle_agent.live` with its planned interface, so the
surfaces import. Tests that drive a session monkeypatch `live.start` themselves. Inert once live.py exists."""

import importlib.util
import sys
import types

if importlib.util.find_spec("huddle_agent.live") is None:  # ponytail: delete this file after Task 1 merges
    stub = types.ModuleType("huddle_agent.live")

    async def start(ctx, *, prompt, surface, post, events):
        raise NotImplementedError("huddle_agent.live is Task 1")

    def delegation_policy(backend_tools: list[str]) -> str:
        return "Delegation policy:\nBackend tools:\n" + "\n".join(f"- {t}" for t in backend_tools)

    stub.start, stub.delegation_policy = start, delegation_policy
    sys.modules["huddle_agent.live"] = stub
    import huddle_agent

    huddle_agent.live = stub
