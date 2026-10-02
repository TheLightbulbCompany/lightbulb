"""Teams BFF — native UI for Paperclip."""

from fastapi import APIRouter

from . import agents as _agents
from . import approvals as _approvals
from . import attention as _attention
from . import cases as _cases
from . import decisions as _decisions
from . import feedback as _feedback
from . import feed as _feed
from . import inbox as _inbox
from . import interactions as _interactions
from . import issues as _issues
from . import runs as _runs
from . import search as _search
from . import settings as _settings_r
from . import session_work as _session_work
from . import tree_control as _tree_control
from . import work as _work

router = APIRouter(prefix="/teams", tags=["teams"])
router.include_router(_agents.router)
router.include_router(_inbox.router)
router.include_router(_approvals.router)
router.include_router(_attention.router)
router.include_router(_decisions.router)
router.include_router(_issues.router)
router.include_router(_tree_control.router)
router.include_router(_interactions.router)
router.include_router(_runs.router)
router.include_router(_search.router)
router.include_router(_work.router)
router.include_router(_feed.router)
router.include_router(_feedback.router)
router.include_router(_cases.router)
router.include_router(_settings_r.router)
router.include_router(_session_work.router)
