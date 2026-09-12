"""Authenticated HTTP replacement for the retired browser WebSocket RPC branch."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from core.auth import AuthContext, get_current_user
from core.services import runtime_rpc as service
from core.services.messaging import (
    ResolveApprovalRequest,
    ResolveQuestionRequest,
    ResolveRuntimeResponse,
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


@router.post("/approvals/{approval_id}/resolve", response_model=ResolveRuntimeResponse)
async def resolve_approval(
    approval_id: str,
    request: ResolveApprovalRequest,
    auth: AuthContext = Depends(get_current_user),
):
    method = "plugin.approval.resolve" if request.kind == "plugin" else "exec.approval.resolve"
    await service.execute_control(
        auth,
        method=method,
        params={"id": approval_id, "decision": request.decision},
    )
    return ResolveRuntimeResponse(id=approval_id)


@router.post("/questions/{question_id}/resolve", response_model=ResolveRuntimeResponse)
async def resolve_question(
    question_id: str,
    request: ResolveQuestionRequest,
    auth: AuthContext = Depends(get_current_user),
):
    params = request.model_dump(by_alias=True, exclude_none=True)
    params["id"] = question_id
    await service.execute_control(auth, method="question.resolve", params=params)
    return ResolveRuntimeResponse(id=question_id)
