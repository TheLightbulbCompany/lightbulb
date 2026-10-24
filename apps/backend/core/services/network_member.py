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
import json
import logging
import re
import time

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
    if apps is None:
        apps = _app_entries(await _read_sessions(owner_id) or {})
    return await _write(owner_id, apps)


async def _write(owner_id: str, apps: list[dict]) -> dict:
    from core.services import clerk_admin
    from core.services.company_identity import slugify
    from core.services.hermes_profiles import put_owner_object
    from core.services.messaging import load_agent_roster

    roster, org = await asyncio.gather(load_agent_roster(owner_id), clerk_admin.get_organization(owner_id))
    if len(roster) + len(apps) > MAX_SESSIONS:
        raise ApiProblem("INVALID_REQUEST", f"This workspace has reached {MAX_SESSIONS} network addresses.")

    # ponytail: names are claimed in order (apps, then agents by id), so a new duplicate-named
    # agent with a lower id, or an app named like an agent, can move an existing agent's address,
    # A taken name's suffix grows (id[:4], id[:8], the full id) until unique.
    # Stable addresses need a stored name per agent.
    used = {s.get("name") for s in apps}
    sessions = []
    for agent in roster:
        base = slugify(agent["name"]) or agent["id"][:8]
        name = base
        for n in (4, 8, None):
            if name not in used:
                break
            name = f"{base}-{agent['id'][:n]}"
        used.add(name)
        sessions.append({"name": name, "agent": "hermes", "title": agent["name"], "agentId": agent["id"]})

    doc = {"workspace": (org or {}).get("name") or owner_id, "sessions": sessions + apps}
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
        await _write(owner_id, _app_entries(prior))
        # Only a warm sidecar is pinged: a parked one reads sessions.json at its next start,
        # and an invoke here would cold-start the workspace (~55 s) from an agent edit.
        if await _session_is_warm(owner_id):
            await container_rpc.call(owner_id, "network.sessions", {})
    except Exception:  # noqa: BLE001
        logger.warning("network sessions publish failed for owner=%s", owner_id, exc_info=True)


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
    from core.services.agentcore_runtime import drop_network_key
    from core.services.hermes_provisioning import _delete_prefixes, stop_runtime_session

    await drop_network_key(owner_id)
    arn = await _runtime_arn(owner_id)
    if arn:
        await stop_runtime_session(owner_id, arn)
    await run_in_thread(_delete_prefixes, [network_prefix(owner_id)])


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
    # An address that only looks app-shaped (an agent named "x-deadbeef") asks everyone.
    mine = [h for h in humans if app and app_person(h) == app[1]]
    return mine or humans


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

    created = utc_now_iso()
    expires = int(time.time()) + HOLD_TTL_S
    asked = await _asked(owner_id, notice["target"])
    row = {
        **key,
        "holdId": hold_id,
        "label": notice["label"],
        "target": notice["target"],
        "state": "open",
        "askedUserIds": asked,
        "createdAt": created,
        "expiresAt": expires,
    }
    try:
        await run_in_thread(messaging_repo._table().put_item, Item=row, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # a repeated notice: already filed
        raise
    sk = messaging_repo.activity_sk(created, hold_item_id(hold_id))
    await asyncio.gather(
        *(
            messaging_repo.put_activity_row(
                {
                    "pk": messaging_repo.activity_pk(owner_id, user_id),
                    "sk": sk,
                    "kind": "network_hold",
                    "holdId": hold_id,
                    "label": notice["label"],
                    "target": notice["target"],
                    "createdAt": created,
                    "expiresAt": expires,
                }
            )
            for user_id in asked
        ),
        return_exceptions=True,
    )
    await asyncio.gather(*(_invalidate_activity(owner_id, u) for u in asked))


async def decide_hold(auth, hold_id: str, action_id: str) -> None:
    """Answer the hold through the runtime, then resolve the item for everyone asked. The caller
    (``activity``) has checked the viewer was asked and the hold is open."""
    from core.auth import resolve_owner_id

    owner_id = resolve_owner_id(auth)
    await container_rpc.call(
        owner_id, "network.hold", {"hold": hold_id, **_VERDICTS[action_id], "decided_by": auth.user_id}
    )
    await hold_notice(owner_id, {"holdId": hold_id, "state": "closed"})
