"""The one deployed LiveKit worker (`lightbulb-huddle`): routes each job to phone, Meet or owner Live."""

from __future__ import annotations

import json
import logging
import os

from livekit.agents import AgentServer, JobContext, JobRequest, cli

from huddle_agent import meet, owner, phone

logger = logging.getLogger("huddle")
AGENT_IDENTITY = "agent-ceo"
WORKER_DISPLAY_NAME = "Lightbulb"  # clients render tiles from attributes, not this name
# LiveKit's default (10 s) would kill a meeting's final transcript retry.
SHUTDOWN_S = 60
# One warm job process and a long init budget, like phone.py: LiveKit Cloud runs this on 1 vCPU, and the default
# (three processes, 10 s each to import the openai/google/noise-cancellation plugins) timed out at start, so the
# worker drained itself and the deployment sat in CrashLoop (7 Nov) with nothing in the agent log.
server = AgentServer(shutdown_process_timeout=SHUTDOWN_S, num_idle_processes=1, initialize_process_timeout=60)


async def _on_request(req: JobRequest) -> None:
    await req.accept(identity=AGENT_IDENTITY, name=WORKER_DISPLAY_NAME)


# In livekit-agents 1.8.11 this kwarg wins over LIVEKIT_AGENT_NAME (worker.py rtc_session); the Dockerfile sets
# LIVEKIT_AGENT_NAME=lightbulb-huddle too, so both agree. HUDDLE_RIG_AGENT_NAME: a laptop run of
# this worker under its own dispatch name, so no real call reaches it.
@server.rtc_session(agent_name=os.environ.get("HUDDLE_RIG_AGENT_NAME", "lightbulb-huddle"), on_request=_on_request)
async def entrypoint(ctx: JobContext) -> None:
    # A phone call: the plan allows one deployed agent, so the SIP dispatch rules target this one and name their
    # rooms `phone-{env}-_<caller>_<random>`. They send no job metadata.
    if ctx.job.room.name.startswith("phone-"):
        return await phone.entrypoint(ctx)
    meta = json.loads(ctx.job.metadata or "{}")
    if meta.get("source") == "meet":
        return await meet.run(ctx, meta)
    # Live in a member's DM with the assistant (owner.py).
    if meta.get("mode") == "owner":
        return await owner.run(ctx, meta)
    logger.warning("no handler for room %s", ctx.job.room.name)


if __name__ == "__main__":
    cli.run_app(server)
