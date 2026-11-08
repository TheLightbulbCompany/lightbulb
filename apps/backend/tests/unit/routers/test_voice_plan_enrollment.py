from unittest.mock import AsyncMock, patch

from core.billing import voice_plan

TERMS = {
    "planVersion": "voice-agents-v1",
    "acceptMonthlyUsd": 250,
    "acceptIncludedMinutes": 1500,
    "acceptOverageUsdPerMinute": 0.35,
}


async def test_member_cannot_enroll_workspace_in_paid_plan(async_client_org_member):
    with patch.object(voice_plan, "enroll", AsyncMock()) as enroll:
        response = await async_client_org_member.post("/api/v1/billing/voice-plan", json=TERMS)
    assert response.status_code == 403
    enroll.assert_not_awaited()


async def test_admin_must_accept_exact_plan_terms(async_client_org_admin):
    with patch.object(voice_plan, "enroll", AsyncMock()) as enroll:
        response = await async_client_org_admin.post(
            "/api/v1/billing/voice-plan", json={**TERMS, "acceptMonthlyUsd": 0}
        )
    assert response.status_code == 422
    enroll.assert_not_awaited()


async def test_disabled_plan_returns_unavailable_without_activation(async_client_org_admin):
    with patch.object(voice_plan, "enroll", AsyncMock(side_effect=voice_plan.VoicePlanError("voice_plan_unavailable"))):
        response = await async_client_org_admin.post("/api/v1/billing/voice-plan", json=TERMS)
    assert response.status_code == 503 and response.json()["detail"]["code"] == "voice_plan_unavailable"


async def test_admin_acceptance_enrolls_only_authenticated_workspace(async_client_org_admin, mock_org_admin_context):
    result = {"status": "active", "planVersion": "voice-agents-v1"}
    with patch.object(voice_plan, "enroll", AsyncMock(return_value=result)) as enroll:
        response = await async_client_org_admin.post("/api/v1/billing/voice-plan", json=TERMS)
    assert response.status_code == 200 and response.json() == result
    enroll.assert_awaited_once_with(mock_org_admin_context.org_id)


async def test_member_cannot_cancel_paid_workspace_plan(async_client_org_member):
    with patch.object(voice_plan, "cancel", AsyncMock()) as cancel:
        response = await async_client_org_member.delete("/api/v1/billing/voice-plan")
    assert response.status_code == 403
    cancel.assert_not_awaited()
