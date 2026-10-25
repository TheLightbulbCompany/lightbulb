"""Teams BFF — Paperclip budgets + costs.

Paperclip is the budget source of truth. Reads are open to any workspace member;
setting a budget (the PATCHes) is org-admin only. Amounts are monthly cents
(``budgetMonthlyCents``), passed through in Paperclip's camelCase.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from core.auth import AuthContext, get_current_user, require_org_admin

from . import agents as _agents
from .agents import CompanyAgentId
from .deps import TeamsContext

router = APIRouter()
_ctx = _agents._ctx


class SetBudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budgetMonthlyCents: int = Field(ge=0, le=10_000_000_00)


@router.get("/budgets/overview")
async def budgets_overview(ctx: TeamsContext = Depends(_ctx)):
    return await _agents._admin().get_budgets_overview(company_id=ctx.company_id, session_cookie=ctx.session_cookie)


@router.get("/costs/summary")
async def costs_summary(request: Request, ctx: TeamsContext = Depends(_ctx)):
    # Only the date range is forwarded; anything else is dropped.
    params = {k: v for k, v in request.query_params.items() if k in ("from", "to")}
    return await _agents._admin().get_costs_summary(
        company_id=ctx.company_id, session_cookie=ctx.session_cookie, params=params
    )


@router.patch("/budgets")
async def set_company_budget(
    body: SetBudgetBody,
    ctx: TeamsContext = Depends(_ctx),
    auth: AuthContext = Depends(get_current_user),
):
    require_org_admin(auth)
    return await _agents._admin().update_company_budget(
        company_id=ctx.company_id, budget_monthly_cents=body.budgetMonthlyCents, session_cookie=ctx.session_cookie
    )


@router.patch("/agents/{agent_id}/budget")
async def set_agent_budget(
    agent_id: CompanyAgentId,
    body: SetBudgetBody,
    ctx: TeamsContext = Depends(_ctx),
    auth: AuthContext = Depends(get_current_user),
):
    require_org_admin(auth)
    return await _agents._admin().update_agent_budget(
        agent_id=agent_id, budget_monthly_cents=body.budgetMonthlyCents, session_cookie=ctx.session_cookie
    )
