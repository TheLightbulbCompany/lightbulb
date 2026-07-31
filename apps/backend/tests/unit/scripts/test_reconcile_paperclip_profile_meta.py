"""Unit tests for the Paperclip profile-meta drift reconciler script.


The reconciler's source of truth is openclaw.json (decoded to a
``PaperclipMeta``). ``diff_profile_meta`` produces the minimal
``update_agent`` PATCH kwargs that bring a Paperclip agent row in line
with openclaw.json. Key contract (mirrors the live dual-write path in
``routers/websocket_chat.py:_intercept_agents_update``):

  * ``role`` is always normalized to a valid role (blank/unknown →
    ``"general"``) on BOTH sides before comparing.
  * ``title`` / ``capabilities`` / ``icon`` that openclaw has cleared
    (``None`` on the ``PaperclipMeta``) reach Paperclip as ``""`` to
    WIPE — never ``None`` (which would mean "leave alone").
  * Only changed fields appear in the diff; an already-in-sync row →
    ``None``.
  * Idempotent: applying the diff then re-diffing → ``None``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services.paperclip_admin_client import PaperclipApiError
from core.services.paperclip_meta import PaperclipMeta
from scripts.reconcile_paperclip_profile_meta import (
    _reconcile_company,
    diff_profile_meta,
)


def _apply_diff_to_paperclip(paperclip_agent: dict, diff: dict) -> dict:
    """Simulate applying the ``update_agent`` PATCH kwargs to the
    Paperclip row, so we can re-diff and assert idempotency. Mirrors
    Paperclip's PATCH semantics: ``None`` = leave alone, ``""`` = wipe
    (stored as empty/absent), any string = set.
    """
    updated = dict(paperclip_agent)
    for key, value in diff.items():
        if key == "role":
            updated["role"] = value
        else:
            # "" wipes; non-empty sets. Store "" verbatim so the next
            # read normalizes it back to absent — matches Paperclip.
            updated[key] = value
    return updated


# ---------------------------------------------------------------------------
# diff_profile_meta — pure core
# ---------------------------------------------------------------------------


def test_diff_returns_none_when_already_in_sync():
    """Identical role + fields → no PATCH needed."""
    openclaw_meta = PaperclipMeta(
        role="engineer",
        title="Backend specialist",
        capabilities="Writes Python services",
        icon="wrench",
    )
    paperclip_agent = {
        "role": "engineer",
        "title": "Backend specialist",
        "capabilities": "Writes Python services",
        "icon": "wrench",
    }
    assert diff_profile_meta(openclaw_meta, paperclip_agent) is None


def test_diff_returns_none_when_both_sides_empty():
    """openclaw role defaults to 'general', nullable fields absent on
    both sides → no diff."""
    openclaw_meta = PaperclipMeta(role="general")
    paperclip_agent = {"role": "general"}
    assert diff_profile_meta(openclaw_meta, paperclip_agent) is None


def test_diff_detects_role_drift_only():
    openclaw_meta = PaperclipMeta(role="cto", title="Lead", capabilities="x", icon="i")
    paperclip_agent = {"role": "engineer", "title": "Lead", "capabilities": "x", "icon": "i"}
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {"role": "cto"}


def test_diff_detects_title_drift_only():
    openclaw_meta = PaperclipMeta(role="general", title="New title")
    paperclip_agent = {"role": "general", "title": "Old title"}
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {"title": "New title"}


def test_diff_detects_capabilities_drift_only():
    openclaw_meta = PaperclipMeta(role="general", capabilities="Does new things")
    paperclip_agent = {"role": "general", "capabilities": "Does old things"}
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {"capabilities": "Does new things"}


def test_diff_detects_icon_drift_only():
    openclaw_meta = PaperclipMeta(role="general", icon="rocket")
    paperclip_agent = {"role": "general", "icon": "wrench"}
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {"icon": "rocket"}


def test_diff_cleared_field_wipes_with_empty_string():
    """openclaw cleared the title (None on the meta) but Paperclip still
    has a value → PATCH must send '' to WIPE, not None (leave alone)."""
    openclaw_meta = PaperclipMeta(role="general", title=None)
    paperclip_agent = {"role": "general", "title": "Stale title"}
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {"title": ""}


def test_diff_cleared_multiple_fields_all_wiped():
    openclaw_meta = PaperclipMeta(role="general", title=None, capabilities=None, icon=None)
    paperclip_agent = {
        "role": "general",
        "title": "t",
        "capabilities": "c",
        "icon": "i",
    }
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {"title": "", "capabilities": "", "icon": ""}


def test_diff_paperclip_blank_title_treated_as_empty():
    """A Paperclip title that is whitespace-only normalizes to empty, so
    when openclaw also has it cleared there's no drift."""
    openclaw_meta = PaperclipMeta(role="general", title=None)
    paperclip_agent = {"role": "general", "title": "   "}
    assert diff_profile_meta(openclaw_meta, paperclip_agent) is None


def test_diff_paperclip_missing_keys_treated_as_empty():
    """Paperclip row that omits title/capabilities/icon entirely is
    empty on those fields — no drift when openclaw also has them cleared.
    """
    openclaw_meta = PaperclipMeta(role="general")
    paperclip_agent = {"role": "general"}
    assert diff_profile_meta(openclaw_meta, paperclip_agent) is None


def test_diff_paperclip_invalid_role_normalized_to_general():
    """Paperclip row carrying a bogus role normalizes to 'general'; if
    openclaw is also 'general' there's no drift."""
    openclaw_meta = PaperclipMeta(role="general")
    paperclip_agent = {"role": "not-a-real-role"}
    assert diff_profile_meta(openclaw_meta, paperclip_agent) is None


def test_diff_paperclip_blank_role_normalized_to_general():
    openclaw_meta = PaperclipMeta(role="general")
    paperclip_agent = {"role": ""}
    assert diff_profile_meta(openclaw_meta, paperclip_agent) is None


def test_diff_paperclip_invalid_role_corrected_to_openclaw_role():
    """When openclaw has a real role but Paperclip's is bogus, the diff
    must set the openclaw role."""
    openclaw_meta = PaperclipMeta(role="designer")
    paperclip_agent = {"role": "garbage"}
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {"role": "designer"}


def test_diff_multiple_fields_drift_minimal_patch():
    openclaw_meta = PaperclipMeta(
        role="pm",
        title="Product manager",
        capabilities="Plans roadmaps",
        icon="clipboard",
    )
    paperclip_agent = {
        "role": "general",
        "title": "Product manager",  # in sync
        "capabilities": "Old caps",  # drift
        "icon": None,  # drift (set from cleared)
    }
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff == {
        "role": "pm",
        "capabilities": "Plans roadmaps",
        "icon": "clipboard",
    }


def test_diff_is_idempotent_after_apply():
    """Apply the diff to the Paperclip row, then re-diff → None."""
    openclaw_meta = PaperclipMeta(
        role="cmo",
        title="Marketing chief",
        capabilities=None,  # cleared on openclaw
        icon="megaphone",
    )
    paperclip_agent = {
        "role": "general",
        "title": "Old marketing",
        "capabilities": "Stale caps",
        "icon": "old-icon",
    }
    diff = diff_profile_meta(openclaw_meta, paperclip_agent)
    assert diff is not None
    reconciled = _apply_diff_to_paperclip(paperclip_agent, diff)
    assert diff_profile_meta(openclaw_meta, reconciled) is None


def test_diff_idempotent_for_already_synced_wipe():
    """If openclaw cleared a field and Paperclip already shows it empty
    (''), no wipe is re-issued."""
    openclaw_meta = PaperclipMeta(role="general", title=None)
    paperclip_agent = {"role": "general", "title": ""}
    assert diff_profile_meta(openclaw_meta, paperclip_agent) is None


# ---------------------------------------------------------------------------
# _reconcile_company — orchestration with stubbed admin client + EFS read
# ---------------------------------------------------------------------------


def _make_admin_client(agents: list) -> MagicMock:
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value=agents)
    admin.update_agent = AsyncMock()
    return admin


def _stub_efs_config(agent_entries: list) -> dict:
    """Build a parsed openclaw.json with an agents.list[]."""
    return {"agents": {"list": agent_entries}}


async def test_reconcile_dry_run_makes_no_update_calls(monkeypatch):
    """Dry-run logs drift but never PATCHes Paperclip."""
    admin = _make_admin_client([{"id": "a1", "companyId": "co_1", "role": "general", "title": "Stale"}])
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(
            return_value=_stub_efs_config(
                [{"id": "a1", "params": {"paperclip": {"role": "engineer", "title": "Fresh"}}}]
            )
        ),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=False,
        agent_id_filter=None,
    )

    assert matched == 1
    assert reconciled == 0
    admin.update_agent.assert_not_awaited()


async def test_reconcile_apply_patches_drifted_agent(monkeypatch):
    admin = _make_admin_client([{"id": "a1", "companyId": "co_1", "role": "general", "title": "Stale"}])
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(
            return_value=_stub_efs_config(
                [{"id": "a1", "params": {"paperclip": {"role": "engineer", "title": "Fresh"}}}]
            )
        ),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=True,
        agent_id_filter=None,
    )

    assert matched == 1
    assert reconciled == 1
    admin.update_agent.assert_awaited_once()
    kwargs = admin.update_agent.await_args.kwargs
    assert kwargs["agent_id"] == "a1"
    assert kwargs["role"] == "engineer"
    assert kwargs["title"] == "Fresh"


async def test_reconcile_skips_agents_in_sync(monkeypatch):
    """An agent whose Paperclip row matches openclaw.json is not
    matched and never PATCHed even on --apply."""
    admin = _make_admin_client([{"id": "a1", "companyId": "co_1", "role": "engineer", "title": "Same"}])
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(
            return_value=_stub_efs_config(
                [{"id": "a1", "params": {"paperclip": {"role": "engineer", "title": "Same"}}}]
            )
        ),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=True,
        agent_id_filter=None,
    )

    assert matched == 0
    assert reconciled == 0
    admin.update_agent.assert_not_awaited()


async def test_reconcile_intersects_by_id(monkeypatch):
    """Only agents present in BOTH Paperclip and openclaw.json are
    compared — orphans on either side are ignored."""
    admin = _make_admin_client(
        [
            {"id": "a1", "companyId": "co_1", "role": "general", "title": "Stale"},
            {"id": "paperclip_only", "companyId": "co_1", "role": "general"},
        ]
    )
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(
            return_value=_stub_efs_config(
                [
                    {"id": "a1", "params": {"paperclip": {"role": "engineer", "title": "Fresh"}}},
                    {"id": "efs_only", "params": {"paperclip": {"role": "cto"}}},
                ]
            )
        ),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=True,
        agent_id_filter=None,
    )

    assert matched == 1
    assert reconciled == 1
    # Only a1 patched — paperclip_only / efs_only are not in the intersection.
    admin.update_agent.assert_awaited_once()
    assert admin.update_agent.await_args.kwargs["agent_id"] == "a1"


async def test_reconcile_agent_id_filter(monkeypatch):
    """--agent-id scopes the reconcile to a single agent."""
    admin = _make_admin_client(
        [
            {"id": "a1", "companyId": "co_1", "role": "general", "title": "Stale1"},
            {"id": "a2", "companyId": "co_1", "role": "general", "title": "Stale2"},
        ]
    )
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(
            return_value=_stub_efs_config(
                [
                    {"id": "a1", "params": {"paperclip": {"role": "engineer", "title": "Fresh1"}}},
                    {"id": "a2", "params": {"paperclip": {"role": "designer", "title": "Fresh2"}}},
                ]
            )
        ),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=True,
        agent_id_filter="a2",
    )

    assert matched == 1
    assert reconciled == 1
    admin.update_agent.assert_awaited_once()
    assert admin.update_agent.await_args.kwargs["agent_id"] == "a2"


async def test_reconcile_non_list_list_agents_handled(monkeypatch):
    """list_agents is typed dict but returns a JSON array; a non-list
    response is logged and skipped, never raises."""
    admin = MagicMock()
    admin.list_agents = AsyncMock(return_value={"error": "unexpected"})
    admin.update_agent = AsyncMock()
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(return_value=_stub_efs_config([])),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=True,
        agent_id_filter=None,
    )

    assert matched == 0
    assert reconciled == 0
    admin.update_agent.assert_not_awaited()


async def test_reconcile_missing_efs_config_skips(monkeypatch):
    """No openclaw.json on EFS (returns None) → nothing to reconcile."""
    admin = _make_admin_client([{"id": "a1", "companyId": "co_1", "role": "general", "title": "Stale"}])
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(return_value=None),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=True,
        agent_id_filter=None,
    )

    assert matched == 0
    assert reconciled == 0
    admin.update_agent.assert_not_awaited()


async def test_reconcile_one_agent_failure_does_not_abort_loop(monkeypatch):
    """A PaperclipApiError on one agent's PATCH is logged and the loop
    continues to the next agent."""
    admin = MagicMock()
    admin.list_agents = AsyncMock(
        return_value=[
            {"id": "a1", "companyId": "co_1", "role": "general", "title": "Stale1"},
            {"id": "a2", "companyId": "co_1", "role": "general", "title": "Stale2"},
        ]
    )

    async def _update(*, session_cookie, agent_id, **kwargs):
        if agent_id == "a1":
            raise PaperclipApiError("boom", status_code=500, body=None)
        return {}

    admin.update_agent = AsyncMock(side_effect=_update)
    monkeypatch.setattr(
        "scripts.reconcile_paperclip_profile_meta.read_openclaw_config_from_efs",
        AsyncMock(
            return_value=_stub_efs_config(
                [
                    {"id": "a1", "params": {"paperclip": {"role": "engineer"}}},
                    {"id": "a2", "params": {"paperclip": {"role": "designer"}}},
                ]
            )
        ),
    )

    matched, reconciled = await _reconcile_company(
        admin_client=admin,
        admin_cookie="cookie",
        company_id="co_1",
        owner_id="owner_1",
        apply=True,
        agent_id_filter=None,
    )

    assert matched == 2
    # a1 failed, a2 succeeded — loop did not abort.
    assert reconciled == 1
    assert admin.update_agent.await_count == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
