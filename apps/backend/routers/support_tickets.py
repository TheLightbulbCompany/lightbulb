"""Support ticket capture -- POST /api/v1/support/tickets."""

import logging

from fastapi import APIRouter, Depends

from core.auth import AuthContext, get_current_user
from core.services import support_ticket
from schemas.support import SupportTicketRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/tickets", summary="Submit a support ticket", operation_id="submit_support_ticket")
async def submit_support_ticket(
    body: SupportTicketRequest,
    auth: AuthContext = Depends(get_current_user),
) -> dict:
    """Enrich + deliver the ticket to the support inbox. Returns sent/received
    regardless of downstream email success (best-effort delivery)."""
    email_id = await support_ticket.submit_ticket(auth, body)
    if email_id is None:
        logger.warning("support ticket from %s not delivered (email disabled/failed)", auth.user_id)
        return {"status": "received"}
    return {"status": "sent", "id": email_id}
