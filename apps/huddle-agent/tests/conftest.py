"""`huddle_agent.live` (Task 1, the GPT-Live session) is built in its own lane. Until it lands, a stub with its
interface stands in so the surfaces that import it load; tests replace `live.start` with their own fake."""

import importlib.util
import sys
import types

if importlib.util.find_spec("huddle_agent.live") is None:
    stub = types.ModuleType("huddle_agent.live")

    async def start(ctx, *, prompt, surface, post, events, room_options=None):  # noqa: ARG001
        raise NotImplementedError("tests patch live.start")

    def delegation_policy(backend_tools: list[str]) -> str:
        return "Delegation policy:\nBackend tools:\n" + "\n".join(
            f"- {t}" for t in backend_tools
        )

    stub.start, stub.delegation_policy = start, delegation_policy
    sys.modules["huddle_agent.live"] = stub
    import huddle_agent

    huddle_agent.live = stub
