"""Webhook triggers of super-agent automations (lane A, Task 8; contracts-v2 §11, spec 06 §3.5–3.6).

A trigger is a `HOOK#{publicId}` row linked from its automation's `SCHED#` row, plus its secret in Secrets
Manager under `hook-{publicId}` (named, never read back here except to verify a delivery). The provider side is
registered with the same helpers the Paperclip seed uses (`inbound_triggers`), deliveries arrive at the same
public URL and `routers/hooks.py` verifies them. No routine, no maintenance clause, no renewal trigger, no state
file: a Google watch is kept alive by `GoogleWatchKeeperWorkflow` (`renew` / `mark_broken` below).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError, RPCStatusCode
from ulid import ULID

from core.config import settings
from core.repositories import automations_repo as repo
from core.repositories import integrations_repo
from core.repositories.tasks_repo import IllegalTransition, VersionConflict
from core.services import google_watch
from core.services.credentials import integration_secret_store
from core.services.trigger_fire_url import trigger_fire_url
from core.temporal.client import get_client

logger = logging.getLogger(__name__)

# Gmail push: Pub/Sub delivers with a per-delivery Google OIDC JWT, not a static secret (routers/hooks.py).
GMAIL_PUSH_MODE = "google_oidc"
# Shapes where we mint the secret (the provider side is armed with it, or the owner pastes it provider-side).
_MINTED = frozenset({"auto_github", "auto_google_watch", "owner_setup", "auto_meta"})
_GOOGLE = frozenset({"auto_google_watch", "owner_setup"})


def keeper_id(ws: str, public_id: str) -> str:
    return f"gwatch:{ws}:{public_id}"


def expires_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


async def seed(
    owner_id: str,
    provider_key: str,
    event_ids: list[str],
    *,
    resource: str | None,
    topic: str | None,
    specialist: str | None,
    automation_id: str | None,
    title: str | None,
    description: str | None,
    actor_user_id: str | None,
) -> dict[str, Any]:
    """The super-agent half of `inbound_triggers.seed_trigger_routine`: same inputs, same answer shape
    (`routine_id` = the automation id), no Paperclip. `automation_id` attaches the trigger to an existing
    automation; otherwise a draft automation is created for it (activation is A6's route)."""
    from core.services import inbound_triggers as it  # lazy: that module branches into this one

    reg = await it._resolve_registration(owner_id, provider_key, event_ids, resource=resource, topic=topic)
    if reg.registration == "poll":
        raise ValueError(
            "use_gmail_push: scheduled inbox checks are not available here; add 'New email' (gmail.message), "
            "which pushes to your Pub/Sub topic"
        )
    provider, inbound, events = reg.provider, reg.inbound, reg.events
    label_text = it._join_human([e.label for e in events])
    id_text = it._join_human([e.id for e in events])

    if automation_id:
        sched = await repo.get_schedule(owner_id, automation_id)
        if sched is None:
            raise ValueError(f"automation {automation_id!r} is not in this workspace")
        created_row = False
    else:
        name = (title or "").strip() or inbound.template.title.format(event_label=label_text)
        prompt = events[0].starter_prompt or inbound.template.starter_prompt
        policy = (description or "").strip() or prompt.format(event_label=label_text, account=resource or "")
        sched, _ = await repo.create_schedule(
            owner_id,
            {
                "scheduleId": str(ULID()),
                "name": name[:200],
                "ownerUserId": actor_user_id or owner_id,
                "cron": [],  # event-only; a cron can be added on the automation later
                "tz": "UTC",
                "policyText": policy,
                "specialist": specialist,
                "budgetPerRunUsd": 1.0,
                "createdBy": "user",
            },
        )
        created_row = True
    sid = sched["scheduleId"]

    public_id = secrets.token_hex(12)  # 24 hex, the shape the proxy route already guards
    mode = GMAIL_PUSH_MODE if reg.registration == "owner_setup" else inbound.signing_mode
    bearer_guided = reg.registration == "guided_paste" and inbound.signing_mode == "bearer"
    secret = secrets.token_urlsafe(32) if reg.registration in _MINTED or bearer_guided else None
    hook: dict[str, Any] = {
        "publicId": public_id,
        "scheduleId": sid,
        "provider": provider_key,
        "events": [e.id for e in events],
        "signingMode": mode,
        "signatureHeader": inbound.signature_header,
        "secretRef": f"hook-{public_id}",
        "armed": False,
        "resource": reg.meta_asset["id"] if reg.meta_asset else resource,
    }
    proxy_url = trigger_fire_url(owner_id, public_id)
    hook, _ = await repo.put_hook(owner_id, hook)
    await repo.update_schedule(
        owner_id,
        sid,
        {"hookPublicIds": [*sched.get("hookPublicIds", []), public_id]},
        expected_version=sched["version"],
    )
    if secret:
        await integration_secret_store.put(owner_id, hook["secretRef"], {"secret": secret, "mode": mode})
    try:
        fields = await _register(owner_id, reg, public_id=public_id, proxy_url=proxy_url, secret=secret, topic=topic)
        if fields:
            hook = await repo.update_hook(owner_id, public_id, fields, expected_version=hook["version"])
    except Exception:
        await _unwind(owner_id, public_id, sid, created_row)
        raise
    if hook.get("expiresAt"):
        await start_keeper(owner_id, public_id, expires_ms(hook["expiresAt"]))
    logger.info(
        "automation_hooks: seeded %s automation=%s hook=%s owner=%s events=%s",
        reg.registration,
        sid,
        public_id,
        owner_id,
        id_text,
    )

    guided = reg.registration == "guided_paste"
    instructions: str | None = None
    if guided:
        instructions = it._guided_instructions(
            provider,
            inbound,
            proxy_url=proxy_url,
            event_label_text=label_text,
            event_id_text=id_text,
            count=len(events),
        )
    elif reg.registration == "owner_setup":
        instructions = it._gmail_setup_instructions(topic or "", proxy_url)
    return {
        "routine_id": sid,
        "automation_id": sid,
        "trigger_public_id": public_id,
        "proxy_url": proxy_url,
        "guided": guided,
        "instructions": instructions,
        "secret_hint": inbound.secret_hint if guided else None,
        "resource": reg.meta_asset["label"] if reg.meta_asset else resource,
        "webhook_secret": secret if bearer_guided else None,
    }


async def _register(owner_id: str, reg, *, public_id: str, proxy_url: str, secret: str | None, topic) -> dict:
    """Register the provider side; returns the `HOOK#` fields to store (armed, provider ids, Google channel)."""
    from core.services import inbound_triggers as it

    events = reg.events
    if reg.registration == "guided_paste":
        return {"armed": True} if secret else {}
    if reg.registration == "auto_github":
        hook_id = await it._create_github_hook(
            token=reg.gh_token,
            resource=reg.resource,
            proxy_url=proxy_url,
            events=it._provider_event_names(events),
            secret=secret,
        )
        return {"armed": True, "providerHookId": hook_id}
    if reg.registration == "auto_vercel":
        webhook_id, vercel_secret = await it._create_vercel_webhook(
            token=reg.vercel_token, resource=reg.resource, proxy_url=proxy_url, events=it._provider_event_names(events)
        )
        await integration_secret_store.put(
            owner_id, f"hook-{public_id}", {"secret": vercel_secret, "mode": reg.inbound.signing_mode}
        )
        return {"armed": True, "providerHookId": webhook_id}
    if reg.registration == "auto_meta":
        asset, fields = reg.meta_asset, it._provider_event_names(events)
        await it._subscribe_meta_asset(asset, fields)
        try:  # the app-level ingress routes a delivery by this binding (routers/meta_webhook.py)
            bindings = await integrations_repo.get_meta_webhook_bindings(owner_id, reg.provider.key)
            bindings.append(
                {
                    "asset_id": asset["id"],
                    "asset_name": asset["label"],
                    "object_type": asset["object_type"],
                    "event_id": events[0].id,
                    "fields": fields,
                    "trigger_public_id": public_id,
                    "trigger_secret": secret,
                }
            )
            await integrations_repo.set_meta_webhook_bindings(owner_id, reg.provider.key, bindings)
        except Exception:
            await it._unsubscribe_meta_asset(asset)
            raise
        return {"armed": True, "providerHookId": asset["id"]}
    account = reg.resource
    if reg.registration == "auto_google_watch":
        channel = await google_watch.start_watch(
            owner_id=owner_id,
            account_email=account,
            provider_event=events[0].id,
            proxy_url=proxy_url,
            channel_token=secret,
        )
        return {
            "armed": True,
            "account": account,
            "channelId": channel["channel_id"],
            "resourceId": channel["resource_id"],
            "expiresAt": _iso(channel["expiration_ms"]),
            "pageToken": channel.get("page_token"),
        }
    if reg.registration == "owner_setup":
        watch = await google_watch.start_gmail_watch(owner_id=owner_id, account_email=account, topic=topic)
        return {"armed": True, "account": account, "topic": topic, "expiresAt": _iso(watch["expiration_ms"])}
    raise ValueError(f"unsupported registration {reg.registration!r}")


async def _unwind(owner_id: str, public_id: str, sid: str, created_row: bool) -> None:
    """Undo this call's rows after the provider refused: never masks the caller's error."""
    try:
        await integration_secret_store.delete(owner_id, f"hook-{public_id}")
        await repo.delete_hook(owner_id, public_id)
        if created_row:
            await repo.delete_schedule(owner_id, sid)
        else:
            await _unlink(owner_id, sid, public_id)
    except Exception:
        logger.exception("automation_hooks: unwind of hook=%s owner=%s failed", public_id, owner_id)


async def _unlink(owner_id: str, sid: str, public_id: str) -> None:
    sched = await repo.get_schedule(owner_id, sid)
    if sched and public_id in (sched.get("hookPublicIds") or []):
        kept = [p for p in sched["hookPublicIds"] if p != public_id]
        await repo.update_schedule(owner_id, sid, {"hookPublicIds": kept}, expected_version=sched["version"])


async def arm(owner_id: str, public_id: str, secret: str) -> bool:
    """The paste half of a guided hook: the provider's secret into Secrets Manager, the hook armed.
    False when there is no such `HOOK#` (the caller falls back to today's path)."""
    hook = await repo.get_hook(owner_id, public_id)
    if hook is None:
        return False
    await integration_secret_store.put(owner_id, hook["secretRef"], {"secret": secret, "mode": hook["signingMode"]})
    await repo.update_hook(owner_id, public_id, {"armed": True}, expected_version=hook["version"])
    return True


async def delete(owner_id: str, public_id: str) -> None:
    """Unregister the provider side (best effort), stop its keeper, drop the secret and the row, unlink it
    from its automation. Already gone is done."""
    hook = await repo.get_hook(owner_id, public_id)
    if hook is None:
        return
    await _unregister(owner_id, hook)
    await cancel_keeper(owner_id, public_id)
    await integration_secret_store.delete(owner_id, hook["secretRef"])
    await repo.delete_hook(owner_id, public_id)
    await _unlink(owner_id, hook["scheduleId"], public_id)


async def _unregister(owner_id: str, hook: dict) -> None:
    from core.services import inbound_triggers as it

    provider, hook_id, resource = hook["provider"], hook.get("providerHookId"), hook.get("resource")
    try:
        if provider == "github" and hook_id and resource:
            token = await it._github_token(owner_id)
            if token:
                await it._delete_github_hook(token=token, resource=resource, hook_id=hook_id)
        elif provider == "vercel_cli" and hook_id:
            token = await it._vercel_token(owner_id)
            if token:
                await it._delete_vercel_webhook(token=token, webhook_id=hook_id)
        elif provider in it._META_ASSET_LISTERS and resource:
            assets = await it._META_ASSET_LISTERS[provider](owner_id)
            asset = next((a for a in assets if a["id"] == resource), None)
            if asset:
                await it._unsubscribe_meta_asset(asset)
            bindings = await integrations_repo.get_meta_webhook_bindings(owner_id, provider)
            kept = [b for b in bindings if b.get("trigger_public_id") != hook["publicId"]]
            if len(kept) != len(bindings):
                await integrations_repo.set_meta_webhook_bindings(owner_id, provider, kept)
        elif hook.get("account"):
            if hook["signingMode"] == GMAIL_PUSH_MODE:
                await google_watch.stop_gmail_watch(owner_id=owner_id, account_email=hook["account"])
            elif hook.get("channelId") and hook.get("resourceId"):
                await google_watch.stop_watch(
                    owner_id=owner_id,
                    account_email=hook["account"],
                    provider_event=hook["events"][0],
                    channel_id=hook["channelId"],
                    resource_id=hook["resourceId"],
                )
    except Exception:
        logger.exception("automation_hooks: provider unregister failed hook=%s owner=%s", hook["publicId"], owner_id)


# --- Google watch keeper (the Activities' bodies) ---------------------------------------------------


async def start_keeper(ws: str, public_id: str, expires_ms_: int, *, task_queue: str | None = None) -> None:
    """One keeper per Google hook, id `gwatch:{ws}:{publicId}`; a running one is kept."""
    await get_client().start_workflow(
        "GoogleWatchKeeperWorkflow",
        args=[ws, public_id, expires_ms_],
        id=keeper_id(ws, public_id),
        task_queue=task_queue or settings.TEMPORAL_TASK_QUEUE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def cancel_keeper(ws: str, public_id: str) -> None:
    try:
        await get_client().get_workflow_handle(keeper_id(ws, public_id)).cancel()
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise


async def renew(ws: str, public_id: str) -> int | None:
    """Open a fresh watch for the hook, retire the old channel, store the new ids. The new expiry in ms, or
    None when the hook is gone (its keeper ends)."""
    hook = await repo.get_hook(ws, public_id)
    if hook is None:
        return None
    account = hook["account"]
    if hook["signingMode"] == GMAIL_PUSH_MODE:
        watch = await google_watch.start_gmail_watch(owner_id=ws, account_email=account, topic=hook["topic"])
        ms, fields = watch["expiration_ms"], {"armed": True}
    else:
        secret = await integration_secret_store.get(ws, hook["secretRef"])
        channel = await google_watch.start_watch(
            owner_id=ws,
            account_email=account,
            provider_event=hook["events"][0],
            proxy_url=trigger_fire_url(ws, public_id),
            channel_token=(secret or {}).get("secret") or "",
        )
        if hook.get("channelId") and hook.get("resourceId"):
            await google_watch.stop_watch(
                owner_id=ws,
                account_email=account,
                provider_event=hook["events"][0],
                channel_id=hook["channelId"],
                resource_id=hook["resourceId"],
            )
        ms = channel["expiration_ms"]
        fields = {"channelId": channel["channel_id"], "resourceId": channel["resource_id"], "armed": True}
        if channel.get("page_token") and not hook.get("pageToken"):  # never clobber S7's sync token
            fields["pageToken"] = channel["page_token"]
    await _update_hook(ws, public_id, {**fields, "expiresAt": _iso(ms)})
    return ms


async def _update_hook(ws: str, public_id: str, fields: dict) -> None:
    """Version-checked; one re-read when another writer (S7's sync token) moved the row meanwhile."""
    for _ in range(2):
        hook = await repo.get_hook(ws, public_id)
        if hook is None:
            raise KeyError(public_id)
        try:
            await repo.update_hook(ws, public_id, fields, expected_version=hook["version"])
            return
        except VersionConflict:
            continue
    raise VersionConflict(f"hook {public_id}: kept changing under the keeper")


async def mark_broken(ws: str, public_id: str) -> None:
    """Three failed renewals or a passed expiry: the automation pauses (`google_watch_broken`) until the
    owner reconnects Google. Already paused or gone: nothing to do."""
    from core.services import automation_schedule

    hook = await repo.get_hook(ws, public_id)
    if hook is None:
        return
    if hook.get("armed"):  # disarmed = the one to re-arm on resume; deliveries on the dead channel 401
        await _update_hook(ws, public_id, {"armed": False})
    try:
        row = await repo.set_status(
            ws,
            hook["scheduleId"],
            "paused_system",
            reason="google_watch_broken",
            allowed_from=frozenset({"draft", "active", "paused"}),
        )
    except IllegalTransition:
        return
    await automation_schedule.upsert(row)  # pauses its Temporal Schedule when it has one
