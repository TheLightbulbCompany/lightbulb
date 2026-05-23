"""Teams BFF — native UI for Paperclip."""

from fastapi import APIRouter

from . import agents as _agents
from . import approvals as _approvals
from . import feed as _feed
from . import inbox as _inbox
from . import issues as _issues
from . import members as _members
from . import runs as _runs
from . import settings as _settings_r
from . import skills as _skills
from . import work as _work

router = APIRouter(prefix="/teams", tags=["teams"])
router.include_router(_agents.router)
router.include_router(_inbox.router)
router.include_router(_approvals.router)
router.include_router(_issues.router)
router.include_router(_runs.router)
router.include_router(_work.router)
router.include_router(_feed.router)
router.include_router(_skills.router)
router.include_router(_members.router)
router.include_router(_settings_r.router)
