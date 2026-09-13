"""Authenticated HTTP replacement for the retired browser WebSocket RPC branch."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from core.auth import AuthContext, get_current_user
from core.services import runtime_rpc as service
from core.services.messaging import (
    RuntimeOperation,
    RuntimeOperationAccepted,
    RuntimeRpcRequest,
    RuntimeRpcResponse,
)

router = APIRouter()


@router.post(
    "/rpc",
    response_model=RuntimeRpcResponse | RuntimeOperationAccepted,
    responses={202: {"model": RuntimeOperationAccepted}},
)
async def runtime_rpc(request: RuntimeRpcRequest, response: Response, auth: AuthContext = Depends(get_current_user)):
    status, payload = await service.execute(auth, request)
    response.status_code = status
    return payload


@router.get("/operations/{operation_id}", response_model=RuntimeOperation)
async def runtime_operation(operation_id: str, auth: AuthContext = Depends(get_current_user)):
    return await service.get_operation(auth, operation_id)
