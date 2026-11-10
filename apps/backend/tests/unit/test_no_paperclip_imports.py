"""Phase 1 task 8: Paperclip code leaves ``core/`` and ``routers/``.

Every file there that still says "paperclip" (any case) is listed below, so the list can only
shrink: a new mention fails, and so does a listed file that no longer has one (delete its line).

``KEPT`` and ``IMPORTERS`` are empty since task 9: no Paperclip module is left. ``MENTIONS`` only carry a
data name or a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

#: Deleted in this task; nothing may import them again.
DELETED = (
    "core.services.company_deploy_service",
    "core.services.company_agent_materializer",
    "core.services.paperclip_credential_rule",
    "core.services.paperclip_agent_lifecycle",
    "core.services.paperclip_cost",
    "core.services.paperclip_instance_flags",
    "core.services.paperclip_github_token",
    "core.services.company_stripe_approval",
    "core.services.work_products",
    "routers.paperclip_proxy",
    "routers.paperclip_cost",
    "routers.company_board",
    "core.services.paperclip_api_key",
    "core.services.paperclip_provisioning",
    "core.services.paperclip_user_delete",
    "core.services.paperclip_sql",
    "core.services.board_cards",
    "core.services.paperclip_session_cookie",
    "core.services.paperclip_user_session",
    "core.services.paperclip_admin_client",
    "core.services.paperclip_admin_session",
    "core.services.paperclip_adapter_config",
    "core.services.paperclip_meta",
    "core.services.paperclip_openclaw_pairing",
    "core.services.paperclip_owner_email",
    "core.repositories.paperclip_repo",
)

KEPT: dict[str, str] = {}

IMPORTERS: set[str] = set()

#: Data names (the ``paperclip-companies`` table, ``PAPERCLIP_*`` env names, the ``params.paperclip`` wire
#: key, the ``paperclipUserId`` field, the ``paperclip.event`` live-event name, wipe step names) and comments.
MENTIONS = {
    "core/config.py",
    "core/repositories/integrations_repo.py",
    "core/repositories/update_repo.py",
    "core/repositories/user_repo.py",
    "core/repositories/workspace_repo.py",
    "core/services/account_wipe.py",
    "core/services/activity.py",
    "core/services/admin_service.py",
    "core/services/agent_mail_thread.py",
    "core/services/agent_meta.py",
    "core/services/agent_provenance.py",
    "core/services/automation_hooks.py",
    "core/services/clerk_admin.py",
    "core/services/company_package.py",
    "core/services/connection_service.py",
    "core/services/credentials/providers.yaml",
    "core/services/credentials/registry.py",
    "core/services/daily_digest.py",
    "core/services/entitlement_guards.py",
    "core/services/google_watch.py",
    "core/services/heartbeat_checklist.py",
    "core/services/hermes_profiles.py",
    "core/services/hermes_provisioning.py",
    "core/services/hook_signatures.py",
    "core/services/inbound_triggers.py",
    "core/services/local_sessions.py",
    "core/services/messaging.py",
    "core/services/needs_you_email.py",
    "core/services/node_broker.py",
    "core/services/notification_copy.py",
    "core/services/onboarding_brief.py",
    "core/services/readiness_state.py",
    "core/services/runtime_rpc.py",
    "core/services/service_token.py",
    "core/services/session_keys.py",
    "core/services/stripe_webhook_handlers.py",
    "core/services/trigger_fire_url.py",
    "core/services/update_service.py",
    "core/services/workspace_wipe.py",
    "core/tasks_schema.py",
    "core/temporal/activities.py",
    "routers/automations.py",
    "routers/container.py",
    "routers/debug.py",
    "routers/gateway_relay.py",
    "routers/memory.py",
    "routers/orgs.py",
    "routers/sessions.py",
    "routers/teams/agents.py",
    "routers/webhooks.py",
    "routers/websocket_chat.py",
    "routers/workspace_files.py",
}


def _mentions() -> set[str]:
    found = set()
    for root in ("core", "routers"):
        for path in (BACKEND / root).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                if "paperclip" in path.read_text(errors="ignore").lower():
                    found.add(path.relative_to(BACKEND).as_posix())
    return found


def test_only_listed_files_mention_paperclip():
    allowed = set(KEPT) | IMPORTERS | MENTIONS
    found = _mentions()
    assert sorted(found - allowed) == [], "new Paperclip code in core/ or routers/"
    assert sorted(allowed - found) == [], "no longer mentions Paperclip: drop it from the list"


def test_deleted_modules_stay_deleted():
    for mod in DELETED:
        assert not (BACKEND / (mod.replace(".", "/") + ".py")).exists(), mod
    pattern = re.compile(r"^\s*(from|import)\s+(" + "|".join(re.escape(m) for m in DELETED) + r")\b", re.M)
    hits = [
        p.relative_to(BACKEND).as_posix()
        for root in ("core", "routers", "scripts", "tests")
        for p in (BACKEND / root).rglob("*.py")
        if pattern.search(p.read_text())
    ]
    assert hits == []
