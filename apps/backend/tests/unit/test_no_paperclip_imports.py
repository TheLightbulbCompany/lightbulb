"""Phase 1 task 8: Paperclip code leaves ``core/`` and ``routers/``.

Every file there that still says "paperclip" (any case) is listed below, so the list can only
shrink: a new mention fails, and so does a listed file that no longer has one (delete its line).

``KEPT`` is the part of inventory §4e Group 1 still on the live path; each line says why. The rest
import a kept module or only carry a data name (the ``paperclip-companies`` table, a
``PAPERCLIP_*`` setting or secret, the ``paperclip.event`` live-event name) or a comment.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
MARK = "# ponytail: delete after the prod data move (task 3)"

#: Deleted in this task; nothing may import them again.
DELETED = (
    "core.services.paperclip_cost",
    "core.services.paperclip_instance_flags",
    "core.services.paperclip_github_token",
    "core.services.company_stripe_approval",
    "core.services.work_products",
    "routers.paperclip_proxy",
    "routers.paperclip_cost",
    "routers.company_board",
    "core.services.paperclip_api_key",
    "core.services.board_cards",
)

KEPT = {
    "core/repositories/paperclip_repo.py": "the workspace META store (resolve_owner_row, funnel, members) and the copy_workspace_meta translation",
    "core/services/paperclip_adapter_config.py": "hermes_gateway adapter block for agent create",
    "core/services/paperclip_admin_client.py": "workspace provisioning (its agent writes mirror into the registry)",
    "core/services/paperclip_admin_session.py": "admin cookie for the client above and for the prod scripts",
    "core/services/paperclip_agent_lifecycle.py": "marketplace deploy only (company_deploy_service, task 6)",
    "core/services/paperclip_credential_rule.py": "catalog agent materializer",
    "core/services/paperclip_meta.py": "agent create fields",
    "core/services/paperclip_openclaw_pairing.py": "resolve_company_id_for_owner, read by the client callers",
    "core/services/paperclip_owner_email.py": "agent mail sender identity",
    "core/services/paperclip_provisioning.py": "workspace provisioning seeds the company and starter agents",
    "core/services/paperclip_session_cookie.py": "/teams/* BFF session cookie and the 401 self-heal in main.py",
    "core/services/paperclip_sql.py": "RDS Data API for paperclip_user_delete",
    "core/services/paperclip_user_delete.py": "account and workspace wipe delete the Better Auth user",
    "core/services/paperclip_user_session.py": "/teams/* BFF per-user sign-in",
    "routers/teams/agents.py": "the shared /teams _ctx (session cookie) and /teams/members principal map; /agents routes are native",
    "routers/teams/deps.py": "TeamsContext for agents.py and settings.py",
    "routers/teams/schemas.py": "request bodies for agents.py and settings.py",
}

#: Import a ``KEPT`` module (a Paperclip call on a live path).
IMPORTERS = {
    "core/repositories/container_repo.py",
    "core/services/account_wipe.py",
    "core/services/activity.py",
    "core/services/admin_service.py",
    "core/services/agent_lifecycle.py",
    "core/services/agent_mail_owner_send.py",
    "core/services/agent_mail_send.py",
    "core/services/agent_registry.py",
    "core/services/company_agent_materializer.py",
    "core/services/company_deploy_service.py",
    "core/services/company_package.py",
    "core/services/daily_digest.py",
    "core/services/desktop_execution.py",
    "core/services/hermes_provisioning.py",
    "core/services/ideas.py",
    "core/services/inbound_triggers.py",
    "core/services/link_wallet.py",
    "core/services/mac_browser.py",
    "core/services/messaging.py",
    "core/services/onboarding_brief.py",
    "core/services/openclaw_work.py",
    "core/services/owner_facts.py",
    "core/services/readiness_state.py",
    "core/services/run_analytics.py",
    "core/services/runtime_rpc.py",
    "core/services/session_chat.py",
    "core/services/text_lines.py",
    "core/services/update_service.py",
    "core/services/user_provision.py",
    "core/services/voice_calls.py",
    "core/services/voice_persona.py",
    "core/services/website_logins.py",
    "core/services/week4_active.py",
    "core/services/workspace_name.py",  # the META store's class is still named PaperclipRepo (task 9 renames it)
    "core/services/workspace_wipe.py",
    "routers/admin.py",
    "routers/billing.py",
    "routers/company_context.py",
    "routers/debug.py",
    "routers/messaging.py",
    "routers/ses_inbound.py",
    "routers/teams/settings.py",  # the META store's class is still named PaperclipRepo (task 9 renames it)
    "routers/webhooks.py",
}

#: Data names and comments only.
MENTIONS = {
    "core/config.py",
    "core/repositories/integrations_repo.py",
    "core/repositories/update_repo.py",
    "core/repositories/user_repo.py",
    "core/services/agent_mail_thread.py",
    "core/services/agent_provenance.py",
    "core/services/automation_hooks.py",
    "core/services/clerk_admin.py",
    "core/services/connection_service.py",
    "core/services/credentials/providers.yaml",
    "core/services/credentials/registry.py",
    "core/services/entitlement_guards.py",
    "core/services/google_watch.py",
    "core/services/heartbeat_checklist.py",
    "core/services/hermes_profiles.py",
    "core/services/hook_signatures.py",
    "core/services/local_session_grants.py",
    "core/services/local_sessions.py",
    "core/services/mac_cdp.py",
    "core/services/meeting_notes.py",
    "core/services/needs_you_email.py",
    "core/services/node_broker.py",
    "core/services/notification_copy.py",
    "core/services/owner_secret_sweep.py",
    "core/services/service_token.py",
    "core/services/session_keys.py",
    "core/services/stripe_webhook_handlers.py",
    "core/services/task_conversation.py",
    "core/services/trial_recap.py",
    "core/services/trigger_fire_url.py",
    "core/tasks_schema.py",
    "core/temporal/activities.py",
    "routers/automations.py",
    "routers/container.py",
    "routers/gateway_relay.py",
    "routers/lifecycle_operations.py",
    "routers/memory.py",
    "routers/meta_webhook.py",
    "routers/orgs.py",
    "routers/sessions.py",
    "routers/websocket_chat.py",
    "routers/workspace_files.py",
}

#: The prod data-move scripts and the code only they need.
FOR_SCRIPTS = (
    "scripts/copy_workspace_meta.py",
    "scripts/copy_workspace_names.py",
    "scripts/backfill_agent_registry.py",
    "scripts/cancel_paperclip_approvals.py",
    "scripts/list_paperclip_webhook_triggers.py",
    "core/services/agent_registry.py",
    "core/repositories/paperclip_repo.py",
    "core/services/paperclip_admin_client.py",
)


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


def test_kept_for_the_prod_scripts_is_marked():
    assert [f for f in FOR_SCRIPTS if MARK not in (BACKEND / f).read_text()] == []
