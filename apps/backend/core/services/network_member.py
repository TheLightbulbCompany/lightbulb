"""The workspace as one computer on the free ``lightbulb`` peer-to-peer network.

The network engine runs as a sidecar in the workspace's AgentCore microVM. The backend owns two
inputs it reads: the member key (in the boot secret, written only by
``agentcore_runtime.write_boot_secret``) and ``<owner>/network/sessions.json``, the addresses
this workspace answers on -- one per agent, plus any app entries the sidecar added.

The invite line ``connect`` returns is a credential: it is never logged or stored here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from typing import Any

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.services import container_rpc
from core.services.messaging import ApiProblem

logger = logging.getLogger(__name__)

MAX_SESSIONS = 100


def sessions_key(owner_id: str) -> str:
    return f"{owner_id}/network/sessions.json"


def network_prefix(owner_id: str) -> str:
    return f"{owner_id}/network/"


def app_person(user_id: str) -> str:
    """A short, stable, non-reversible handle for a person on an app's address."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:8]


async def sessions_exist(owner_id: str) -> bool:
    from core.services.hermes_profiles import owner_object_size

    return await run_in_thread(owner_object_size, sessions_key(owner_id)) is not None


async def _read_sessions(owner_id: str) -> dict | None:
    from core.services.hermes_profiles import get_owner_object

    body = await run_in_thread(get_owner_object, sessions_key(owner_id))
    return json.loads(body) if body else None


def _app_entries(doc: dict) -> list[dict]:
    """An app entry is one with no ``agentId``."""
    return [s for s in doc.get("sessions", []) if isinstance(s, dict) and "agentId" not in s]


async def write_sessions(owner_id: str, *, apps: list[dict] | None = None) -> dict:
    """Write ``sessions.json``: every roster agent, then the app entries. ``apps=None`` keeps
    the earlier app entries."""
    prior = await _read_sessions(owner_id) or {}
    return await _write(owner_id, _app_entries(prior) if apps is None else apps, prior)


async def _write(owner_id: str, apps: list[dict], prior: dict) -> dict:
    from core.services import clerk_admin
    from core.services.company_identity import slugify
    from core.services.hermes_profiles import put_owner_object
    from core.services.messaging import load_agent_roster

    roster, org = await asyncio.gather(load_agent_roster(owner_id), clerk_admin.get_organization(owner_id))
    if len(roster) + len(apps) > MAX_SESSIONS:
        raise ApiProblem("INVALID_REQUEST", f"This workspace has reached {MAX_SESSIONS} network addresses.")

    # ponytail: names are claimed in order (apps, then agents by id), so a new duplicate-named
    # agent with a lower id, or an app named like an agent, can move an existing agent's address.
    # A taken name's suffix grows (-<id[:4]>, -id<id[:8]>, -id<id>) until unique, and an agent
    # name never takes the app shape ``{app}-{8 hex}``. Stable addresses need a stored name per agent.
    used = {s.get("name") for s in apps}
    sessions = []
    for agent in roster:
        base = slugify(agent["name"]) or agent["id"][:8]
        name = base
        for suffix in (agent["id"][:4], f"id{agent['id'][:8]}", f"id{agent['id']}"):
            if name not in used and not app_target(name):
                break
            name = f"{base}-{suffix}"
        used.add(name)
        sessions.append({"name": name, "agent": "hermes", "title": agent["name"], "agentId": agent["id"]})

    wake = await _wake(owner_id, prior)
    doc = {"workspace": (org or {}).get("name") or owner_id, "sessions": sessions + apps, "wake": wake}
    await run_in_thread(put_owner_object, sessions_key(owner_id), json.dumps(doc).encode())
    return doc


async def publish_sessions(owner_id: str) -> None:
    """When this workspace is a member, rewrite ``sessions.json`` and tell the sidecar.
    Best effort: never raises."""
    if not settings.OWNER_STATE_BUCKET:
        return
    try:
        prior = await _read_sessions(owner_id)
        if prior is None:
            return
        await _write_and_ping(owner_id, _app_entries(prior))
    except Exception:  # noqa: BLE001
        logger.warning("network sessions publish failed for owner=%s", owner_id, exc_info=True)


async def _write_and_ping(owner_id: str, apps: list[dict]) -> None:
    await _write(owner_id, apps)
    # Only a warm sidecar is pinged: a parked one reads sessions.json at its next start,
    # and an invoke here would cold-start the workspace (~55 s) from an agent edit.
    if await _session_is_warm(owner_id):
        await container_rpc.call(owner_id, "network.sessions", {})


async def _declare(owner_id: str, user_id: str, slug: str, client: str, doc: dict) -> None:
    """Write (or rewrite, with a fresh title) this person's app entry, then tell a warm sidecar."""
    from core.services.messaging import human_display_name

    person = app_person(user_id)
    name = f"{slug}-{person}"
    resolved = (await human_display_name(user_id)).strip()
    # The lookup answers the user id when Clerk cannot say: never publish an id on the network.
    first = resolved.split()[0] if resolved and resolved != user_id else "a teammate"
    entry = {"name": name, "agent": slug, "title": f"{client} via {first}"[:256], "app": slug, "person": person}
    apps = [s for s in _app_entries(doc) if s.get("name") != name]
    await _write_and_ping(owner_id, [*apps, entry])


def rpc_error(exc: RuntimeError) -> dict:
    """The ``{"code", "message", ...}`` a ``container_rpc`` RuntimeError carries; ``{}`` if none."""
    try:
        error = json.loads(str(exc))
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {}


async def app_call(auth, slug: str, client: str, op: str, **fields) -> Any:
    """One ``network.app`` call as ``<client> via <person>``: the person's app session is declared
    in ``sessions.json`` first (and published when new). The member holds the key, not us."""
    from core.auth import resolve_owner_id

    owner_id = resolve_owner_id(auth)
    person = app_person(auth.user_id)
    doc = await _read_sessions(owner_id)
    if doc is None:  # never write sessions.json for a workspace that is not a member
        raise ApiProblem("NETWORK_NOT_CONNECTED", "This workspace is not on a network yet. Connect it in Settings.")
    if not any(s.get("name") == f"{slug}-{person}" for s in _app_entries(doc)):
        await _declare(owner_id, auth.user_id, slug, client, doc)
    params = {"op": op, "app": slug, "person": person, **fields}
    try:
        return await container_rpc.call(owner_id, "network.app", params)
    except RuntimeError as exc:
        # The sidecar never learned the entry (a lost ping, a stale warm estimate): declare once more.
        if rpc_error(exc).get("reason") != "not_declared":
            raise
    await _declare(owner_id, auth.user_id, slug, client, await _read_sessions(owner_id) or doc)
    return await container_rpc.call(owner_id, "network.app", params)


async def _session_is_warm(owner_id: str) -> bool:
    """The ``__session__#<owner>`` estimate ``runtime_transport`` keeps, for THIS runtime.
    False with no runtime yet (a fresh provision writes profiles before CreateAgentRuntime)."""
    from core.repositories import container_repo

    arn, row = await asyncio.gather(_runtime_arn(owner_id), run_in_thread(container_repo.get_session_sync, owner_id))
    return bool(arn) and int(row.get("warm_until") or 0) > int(time.time()) and row.get("arn") == arn


async def _runtime_arn(owner_id: str) -> str | None:
    from core.repositories import container_repo

    return ((await container_repo.get_by_owner_id(owner_id)) or {}).get("agentcore_runtime_arn")


async def connect(owner_id: str, user_id: str, name: str) -> dict:
    """An invite line for one person. A workspace that is not a member yet (not rolled, or just
    reconnected) gets its key and ``sessions.json`` first, and a restart if the key is new."""
    from core.services.hermes_provisioning import prepare_hermes_workspace, stop_runtime_session

    if not await sessions_exist(owner_id):
        from core.services.hermes_provisioning import _delete_prefixes

        # A SIGTERM save from the session reconnect stopped can land after reconnect's delete;
        # a stale state.db under the new key is a KEY_MISMATCH, so clear it once more first.
        await run_in_thread(_delete_prefixes, [f"{network_prefix(owner_id)}state.db"])
        if await prepare_hermes_workspace(owner_id):
            arn = await _runtime_arn(owner_id)
            if arn:
                await stop_runtime_session(owner_id, arn)
    return await container_rpc.call(owner_id, "network.invite", {"for": user_id, "name": name})


async def reconnect(owner_id: str) -> None:
    """Leave the current team: drop the member key, stop the session, delete the network state.
    The next ``connect`` (or reconcile) mints a new key, which makes a new team.

    Order matters: the key goes first so a cold start in between cannot read it; the stop comes
    before the delete so a warm session cannot save network state back after it."""
    from core.repositories import messaging_repo
    from core.services.agentcore_runtime import drop_network_key
    from core.services.hermes_provisioning import _delete_prefixes, stop_runtime_session

    await drop_network_key(owner_id)
    arn = await _runtime_arn(owner_id)
    if arn:
        await stop_runtime_session(owner_id, arn)
    await run_in_thread(_delete_prefixes, [network_prefix(owner_id)])
    await run_in_thread(messaging_repo._table().delete_item, Key=_netwake_key(owner_id))  # a new token next time


# ---------------------------------------------------------------------------
# holds: a stranger's first message waits for a person's Allow (Activity Needs-you)
# ---------------------------------------------------------------------------

HOLD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_APP_PERSON_RE = re.compile(r"^(?P<app>.+)-(?P<person>[0-9a-f]{8})$")
HOLD_TTL_S = 7 * 24 * 3600  # the engine keeps a hold at most 7 days
_VERDICTS = {
    "send": {"verdict": "allow"},
    "decline": {"verdict": "decline"},
    "allow": {"verdict": "allow", "scope": "always"},
}


def hold_item_id(hold_id: str) -> str:
    return f"nethold:{hold_id}"


def _hold_key(owner_id: str, hold_id: str) -> dict:
    from core.repositories import messaging_repo

    return {"pk": messaging_repo.owner_pk(owner_id), "sk": f"NETHOLD#{hold_id}"}


def app_target(target: str) -> tuple[str, str] | None:
    """``(app, person)`` for an app session's address (``{app}-{app_person}``), else None."""
    m = _APP_PERSON_RE.match(target)
    return (m["app"], m["person"]) if m else None


async def get_hold(owner_id: str, hold_id: str) -> dict | None:
    from core.repositories import messaging_repo

    if not HOLD_ID_RE.match(hold_id or ""):
        return None
    key = _hold_key(owner_id, hold_id)
    return await messaging_repo._get(key["pk"], key["sk"])


async def _asked(owner_id: str, target: str) -> list[str]:
    from core.services.website_logins import _humans_to_ask

    humans = await _humans_to_ask(owner_id, None)
    app = app_target(target)
    if app is None:
        return humans
    # An app address belongs to one person; if they have left, nobody else may answer for it.
    return [h for h in humans if app_person(h) == app[1]]


async def hold_notice(owner_id: str, notice: dict) -> None:
    """The bridge's content-free notice ``{holdId, state, label, target}``: ``open`` files one
    Needs-you item per person asked (once per hold), ``closed`` resolves it for all of them."""
    from botocore.exceptions import ClientError

    from core.repositories import messaging_repo
    from core.services.webhook_agents import _invalidate_activity

    hold_id = notice["holdId"]
    key = _hold_key(owner_id, hold_id)
    if notice["state"] == "closed":
        try:
            response = await run_in_thread(
                messaging_repo._table().update_item,
                Key=key,
                UpdateExpression="SET #s = :closed, resolvedAt = if_not_exists(resolvedAt, :now)",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeNames={"#s": "state"},
                ExpressionAttributeValues={":closed": "closed", ":now": utc_now_iso()},
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return  # never opened here (or expired): nothing to resolve
            raise
        asked = response["Attributes"].get("askedUserIds") or []
        await asyncio.gather(*(_invalidate_activity(owner_id, u) for u in asked))
        return

    asked = await _asked(owner_id, notice["target"])
    row = {
        **key,
        "holdId": hold_id,
        "label": notice["label"],
        "target": notice["target"],
        "state": "open",
        "askedUserIds": asked,
        "createdAt": utc_now_iso(),
        "expiresAt": int(time.time()) + HOLD_TTL_S,
    }
    try:
        await run_in_thread(messaging_repo._table().put_item, Item=row, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # A repeated notice (the bridge resending after a failed answer): file any lost item
        # again under the stored createdAt. ``put_activity_row`` is idempotent on its key.
        row = await get_hold(owner_id, hold_id)
        if not row or row.get("state") != "open":
            return
    await _file_hold_items(owner_id, row)


async def _file_hold_items(owner_id: str, hold: dict) -> None:
    from core.repositories import messaging_repo
    from core.services.webhook_agents import _invalidate_activity

    asked = hold.get("askedUserIds") or []
    sk = messaging_repo.activity_sk(hold["createdAt"], hold_item_id(hold["holdId"]))
    results = await asyncio.gather(
        *(
            messaging_repo.put_activity_row(
                {
                    "pk": messaging_repo.activity_pk(owner_id, user_id),
                    "sk": sk,
                    "kind": "network_hold",
                    **{k: hold[k] for k in ("holdId", "label", "target", "createdAt", "expiresAt")},
                }
            )
            for user_id in asked
        ),
        return_exceptions=True,
    )
    failed = [u for u, r in zip(asked, results) if isinstance(r, BaseException)]
    for user_id in failed:
        logger.warning("network hold activity row failed owner=%s user=%s hold=%s", owner_id, user_id, hold["holdId"])
    await asyncio.gather(*(_invalidate_activity(owner_id, u) for u in asked if u not in failed))
    if failed:
        # A non-2xx makes the bridge resend; the repeat refiles the lost rows.
        raise RuntimeError("network hold activity rows failed")


_GONE_CODES = ("not_found", "unknown", "gone", "expired")


async def decide_hold(auth, hold_id: str, action_id: str) -> None:
    """Answer the hold through the runtime, then resolve the item for everyone asked. The caller
    (``activity``) has checked the viewer was asked and the hold is open."""
    from core.auth import resolve_owner_id

    owner_id = resolve_owner_id(auth)
    try:
        await container_rpc.call(
            owner_id, "network.hold", {"hold": hold_id, **_VERDICTS[action_id], "decided_by": auth.user_id}
        )
    except RuntimeError as exc:  # the engine answered with an error
        hold = await get_hold(owner_id, hold_id)
        gone = any(code in str(exc).lower() for code in _GONE_CODES)
        if hold and hold.get("state") == "open" and not gone:
            raise
        # Another tap answered it first, or the engine no longer has it (expired, or the close
        # notice was lost): the item reads answered for everyone.
        await hold_notice(owner_id, {"holdId": hold_id, "state": "closed"})
        raise ApiProblem("HOLD_GONE", "This request was already answered or has expired.") from exc
    await hold_notice(owner_id, {"holdId": hold_id, "state": "closed"})


# ---------------------------------------------------------------------------
# the doorbell: a teammate's Mac that cannot dial this parked workspace wakes it
# ---------------------------------------------------------------------------

WAKE_WINDOW_S = 10
_WAKE_OWNER_RE = re.compile(r"^org_[A-Za-z0-9_]{1,64}$")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _netwake_key(owner_id: str) -> dict:
    from core.repositories import messaging_repo

    return {"pk": messaging_repo.owner_pk(owner_id), "sk": messaging_repo.netwake_sk()}


async def _wake(owner_id: str, prior: dict) -> dict:
    """sessions.json's ``wake`` entry. The token is minted once and kept in sessions.json (only
    the backend and this workspace's runtime read it); the ``NETWAKE`` row holds only its hash."""
    from core.repositories import messaging_repo

    token = (prior.get("wake") or {}).get("token") or f"{owner_id}.{secrets.token_urlsafe(32)}"
    # ponytail: the hash and sessions.json are two writes, so two concurrent FIRST writes can leave
    # them on different tokens; every write re-sets the hash from the doc, so the next one heals it.
    await run_in_thread(
        messaging_repo._table().update_item,
        Key=_netwake_key(owner_id),
        UpdateExpression="SET tokenHash = :h",
        ExpressionAttributeValues={":h": _token_hash(token)},
    )
    return {"url": f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/network/wake/{token}", "token": token}


async def ring(token: str) -> bool:
    """False when the token is not a workspace's doorbell (the route answers 404, nothing written).
    True otherwise; at most one wake per workspace per ``WAKE_WINDOW_S`` reaches the work queue.
    The token is a credential: never logged."""
    from botocore.exceptions import ClientError

    from core.repositories import messaging_repo

    owner_id, dot, _ = token.partition(".")
    if not dot or not _WAKE_OWNER_RE.match(owner_id):
        return False
    key = _netwake_key(owner_id)
    row = await messaging_repo._get(key["pk"], key["sk"])
    digest = _token_hash(token)
    if not row or not hmac.compare_digest(str(row.get("tokenHash") or ""), digest):
        return False
    now = int(time.time())
    ulid = f"{now * 1000:012x}{uuid.uuid4().hex[:20]}"
    table_name = messaging_repo._table().name
    try:
        await messaging_repo._transact(
            [
                {
                    "Update": {
                        "TableName": table_name,
                        "Key": key,
                        "UpdateExpression": "SET lastWakeAt = :now",
                        # tokenHash too: a reconnect that deleted the row in between must not see it recreated.
                        "ConditionExpression": "tokenHash = :h AND (attribute_not_exists(lastWakeAt) OR lastWakeAt < :cutoff)",
                        "ExpressionAttributeValues": {":now": now, ":h": digest, ":cutoff": now - WAKE_WINDOW_S},
                    }
                },
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            "pk": key["pk"],
                            "sk": messaging_repo.work_netwake_sk(ulid),
                            "payload": {"kind": "network_wake", "ownerId": owner_id},
                            "messageGroupId": f"netwake#{owner_id}",
                            "messageDeduplicationId": ulid,
                            "expiresAt": now + messaging_repo.OUTBOX_TTL_S,
                        },
                    }
                },
            ]
        )
    except ClientError as exc:
        if not messaging_repo.is_cancelled(exc):
            raise
        logger.info("network wake skipped (within %ss) owner=%s", WAKE_WINDOW_S, owner_id)
        return True
    logger.info("network wake queued owner=%s", owner_id)
    return True
