"""One immutable usage receipt per owner room/job, including repeated delivery."""

from unittest.mock import AsyncMock, patch

from core.billing import voice_billing
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
