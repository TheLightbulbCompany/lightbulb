"""Shared board-failure mapping for the container-authed approval rails.

Both P0a's ``/company/stripe/u/{owner}/request`` and P0b's provisioning routes
file one Paperclip approval card on behalf of a container, and both need the
same split on the way out.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException

from core.services.paperclip_admin_client import PaperclipApiError

logger = logging.getLogger(__name__)


def _unavailable(owner_id: str, e: Exception) -> HTTPException:
    logger.warning("company_board: could not reach the board owner_id=%s: %s", owner_id, e)
    return HTTPException(
        status_code=503,
        detail={"code": "paperclip_unavailable", "message": "Could not reach the company board; try again later"},
    )


async def file_card(owner_id: str, coro) -> Any:
    """Await one card-filing coroutine, mapping board failures to the wire.

    503 = the board is down, retrying is the right move. 502 = the board
    REFUSED this request; ``retryable`` is 5xx/429, so a 4xx reported as an
    outage would tell the agent to retry forever on something that will never
    succeed. ``e.message`` and not ``str(e)`` — the latter appends the body.
    """
    try:
        return await coro
    except httpx.HTTPError as e:
        raise _unavailable(owner_id, e) from e
    except PaperclipApiError as e:
        if e.retryable:
            raise _unavailable(owner_id, e) from e
        logger.warning("company_board: board rejected the request owner_id=%s: %s", owner_id, e)
        raise HTTPException(status_code=502, detail={"code": "paperclip_rejected", "message": e.message}) from e
