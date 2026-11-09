"""One immutable usage receipt per owner room/job, including repeated delivery."""

from unittest.mock import AsyncMock, patch

from core.billing import voice_billing
from core.repositories import messaging_repo
from core.services import voice_calls


async def test_retry_freezes_original_seconds_and_start_but_new_job_is_separate(repos):
    with (
        patch.object(voice_billing, "report_minutes", AsyncMock()) as ingest,
        patch.object(voice_billing.billing_repo, "get_by_owner_id", AsyncMock(return_value={})),
        patch.object(voice_calls, "_bill", AsyncMock()),
    ):
        await voice_billing.report_owner_minutes("org_1", "owner:r1:job_1", "2026-11-08T16:00:00Z", 20)
        await voice_billing.report_owner_minutes("org_1", "owner:r1:job_1", "2026-11-08T16:01:00Z", 200)
        await voice_billing.report_owner_minutes("org_1", "owner:r1:job_2", "2026-11-08T16:02:00Z", 5)
    assert [c.args for c in ingest.await_args_list] == [
        ("org_1", "owner:r1:job_1", "2026-11-08T16:00:00Z", 20, "owner"),
        ("org_1", "owner:r1:job_1", "2026-11-08T16:00:00Z", 20, "owner"),
        ("org_1", "owner:r1:job_2", "2026-11-08T16:02:00Z", 5, "owner"),
    ]


async def test_owner_retry_keeps_the_first_usage_source_and_work_pointer(repos):
    with (
        patch.object(voice_billing.billing_repo, "get_by_owner_id", AsyncMock(return_value={})),
        patch.object(voice_calls, "_bill", AsyncMock()),
    ):
        await voice_billing.report_owner_minutes("org_1", "owner:r1:job_1", "2026-11-08T16:00:00Z", 20)
        await voice_billing.report_owner_minutes("org_1", "owner:r1:job_1", "2026-11-08T16:01:00Z", 200)

    key = {"pk": messaging_repo.owner_pk("org_1"), "sk": "VOICEUSAGE#owner:r1:job_1"}
    row = await messaging_repo._get(key["pk"], key["sk"])
    assert row["startedAt"] == "2026-11-08T16:00:00Z" and row["seconds"] == 20
    assert row["voiceUsageEvent"]["timestamp"] == "2026-11-08T16:00:00Z"
    assert row["voiceUsageEvent"]["seconds"] == 20
    pointer = await messaging_repo._get(key["pk"], "WORK#VOICEUSAGE#owner:r1:job_1")
    assert pointer["payload"] == {"kind": "voice_usage", "ownerId": "org_1", "transactionId": "owner:r1:job_1"}
