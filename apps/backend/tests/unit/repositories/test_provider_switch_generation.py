import pytest

from core.repositories import container_repo


@pytest.mark.asyncio
async def test_marking_a_switch_stamps_a_new_generation(monkeypatch):
    seen = []

    class Table:
        def update_item(self, **kwargs):
            seen.append(kwargs["ExpressionAttributeValues"][":gen"])
            return {"Attributes": {"needs_provision": False, "provider_switch_generation": "old"}}

    async def run(func, **kwargs):
        return func(**kwargs)

    monkeypatch.setattr(container_repo, "_get_table", lambda: Table())
    monkeypatch.setattr(container_repo, "run_in_thread", run)
    await container_repo.mark_needs_provision("org_a")
    assert seen[0]
