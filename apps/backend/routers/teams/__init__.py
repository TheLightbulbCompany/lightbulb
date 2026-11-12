"""Teams BFF: the roster (``/teams/agents``, ``/teams/members``)."""

from fastapi import APIRouter

from . import agents as _agents

router = APIRouter(prefix="/teams", tags=["teams"])
router.include_router(_agents.router)
