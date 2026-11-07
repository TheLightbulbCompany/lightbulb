"""Teams BFF: the roster (``/teams/agents``, ``/teams/members``) and company settings."""

from fastapi import APIRouter

from . import agents as _agents
from . import settings as _settings_r

router = APIRouter(prefix="/teams", tags=["teams"])
router.include_router(_agents.router)
router.include_router(_settings_r.router)
