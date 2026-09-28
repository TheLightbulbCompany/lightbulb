"""Ticketed WebSocket door to a paired Mac's agent Chrome. Auth is the ticket (core/services/mac_cdp.py)."""

from fastapi import APIRouter, WebSocket

from core.services import mac_cdp

router = APIRouter()


@router.websocket("/{ticket}")
async def mac_cdp_door(websocket: WebSocket, ticket: str) -> None:
    await mac_cdp.serve(websocket, ticket)
