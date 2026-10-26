"""The workspace as one computer on the free ``lightbulb`` peer-to-peer network.

The network engine runs as a sidecar in the workspace's AgentCore microVM. The backend owns two
inputs it reads: the member key (in the boot secret, written only by
``agentcore_runtime.write_boot_secret``) and ``<owner>/network/sessions.json``, the addresses
this workspace answers on -- one per agent, plus any app entries the sidecar added -- and
``members``, the workspace's people: the sidecar answers only a computer whose sign-in proof
(``network_proof``, shown in its hello) names one of them.

A workspace never invites: it joins one network when an admin runs ``lightbulb invite
--agents`` (``join_network``). The once link it joins with is a credential: never logged here.
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


async def _write(owner_id: str, apps: list[dict], prior: dict | None = None) -> dict:
    from core.services import clerk_admin
    from core.services.company_identity import slugify
    from core.services.hermes_profiles import put_owner_object
    from core.services.messaging import load_agent_roster
    from core.services import network_login
    from core.services.website_logins import _humans_to_ask

    roster, org, members = await asyncio.gather(
        load_agent_roster(owner_id), clerk_admin.get_organization(owner_id), _humans_to_ask(owner_id, None)
    )
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
        # `agent` is what a teammate's screen calls the sender ("From The Lightbulb Company's Friday").
        sessions.append({"name": name, "agent": agent["name"] or name, "title": agent["name"], "agentId": agent["id"]})

    # The wake token and the joined mark survive every writer: with no prior in hand, read the file.
    prior = prior if prior is not None else (await _read_sessions(owner_id) or {})
    wake = await _wake(owner_id, prior)
    doc = {
        "workspace": (org or {}).get("name") or owner_id,
        "sessions": sessions + apps,
        "wake": wake,
        # The sidecar's gate: only computers whose sign-in proof names one of these people, and
        # never a computer its person signed out (``network_login.revoke``).
        "members": members,
        "revoked_keys": await network_login.revoked_keys(members),
    }
    if prior.get("joined"):
        doc["joined"] = True  # on a network an admin added it to (``join_network``)
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


async def app_title(user_id: str, client: str) -> str:
    """What the network calls a person's app session: ``"<client> via <first name>"``."""
    from core.services.messaging import human_display_name

    resolved = (await human_display_name(user_id)).strip()
    # The lookup answers the user id when Clerk cannot say: never publish an id on the network.
    first = resolved.split()[0] if resolved and resolved != user_id else "a teammate"
    return f"{client} via {first}"[:256]


def rpc_error(exc: RuntimeError) -> dict:
    """The ``{"code", "message", ...}`` a ``container_rpc`` RuntimeError carries; ``{}`` if none."""
    try:
        error = json.loads(str(exc))
    except ValueError:
        return {}
    return error if isinstance(error, dict) else {}


async def _session_is_warm(owner_id: str) -> bool:
    """The ``__session__#<owner>`` estimate ``runtime_transport`` keeps, for THIS runtime.
    False with no runtime yet (a fresh provision writes profiles before CreateAgentRuntime)."""
    from core.repositories import container_repo

    arn, row = await asyncio.gather(_runtime_arn(owner_id), run_in_thread(container_repo.get_session_sync, owner_id))
    return bool(arn) and int(row.get("warm_until") or 0) > int(time.time()) and row.get("arn") == arn


async def _runtime_arn(owner_id: str) -> str | None:
    from core.repositories import container_repo

    return ((await container_repo.get_by_owner_id(owner_id)) or {}).get("agentcore_runtime_arn")


async def join_network(owner_id: str, ticket: str) -> dict:
    """Join the network ``ticket`` (an admin computer's once ask link) is for. A workspace that is
    not a member yet gets its key and ``sessions.json`` first, and a restart if the key is new.
    The bridge answers at once (``{state: "joining"}``); the admin's computer then asks Allow."""
    from core.services.hermes_provisioning import prepare_hermes_workspace, stop_runtime_session

    if not await sessions_exist(owner_id):
        from core.services.hermes_provisioning import _delete_prefixes

        # A stale state.db under a new key is a KEY_MISMATCH: clear it first.
        await run_in_thread(_delete_prefixes, [f"{network_prefix(owner_id)}state.db"])
        if await prepare_hermes_workspace(owner_id):
            arn = await _runtime_arn(owner_id)
            if arn:
                await stop_runtime_session(owner_id, arn)
        await write_sessions(owner_id)
    try:
        answer = await container_rpc.call(owner_id, "network.join", {"ticket": ticket})
        await _mark_joined(owner_id, True)
        return answer
    except RuntimeError as exc:
        error = rpc_error(exc)
        # The bridge answers UNAVAILABLE while its network sidecar is off or starting (a fresh
        # microVM): a retryable 503 the confirm page waits on, not a bare 500.
        if error.get("code") in ("UNAVAILABLE", "RUNTIME_UNAVAILABLE"):
            raise container_rpc._unavailable() from exc
        if error.get("code") == "already_in_team":
            raise ApiProblem(
                "NETWORK_ALREADY_JOINED", "This workspace's agents are already on another network."
            ) from exc
        raise


async def _mark_joined(owner_id: str, joined: bool) -> None:
    """``sessions.json``'s ``joined``: the hosted MCP routes a member's apps through this
    workspace only while it is on a network an admin added it to."""
    from core.services.hermes_profiles import put_owner_object

    doc = await _read_sessions(owner_id)
    if doc is None or bool(doc.get("joined")) == joined:
        return
    doc = {k: v for k, v in doc.items() if k != "joined"} | ({"joined": True} if joined else {})
    await run_in_thread(put_owner_object, sessions_key(owner_id), json.dumps(doc).encode())


async def leave_network(owner_id: str) -> dict:
    """An admin takes the workspace off its network (``team.leave``), so ``invite --agents``
    can add it to another. A workspace that is not a member is NETWORK_NOT_CONNECTED."""
    if not await sessions_exist(owner_id):
        raise ApiProblem("NETWORK_NOT_CONNECTED", "This workspace is not on a network.")
    try:
        answer = await container_rpc.call(owner_id, "network.leave", {})
    except RuntimeError as exc:
        if rpc_error(exc).get("code") in ("UNAVAILABLE", "RUNTIME_UNAVAILABLE"):
            raise container_rpc._unavailable() from exc
        raise
    await _mark_joined(owner_id, False)
    return answer


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


async def _invalidate_activity(owner_id: str, user_id: str) -> None:
    from core.services.live_events import get_live_event_publisher
    from core.services.messaging import make_live_event

    try:
        await get_live_event_publisher().publish_user(
            owner_id, user_id, make_live_event("runtime.resource.invalidated", {"resource": "activity"})
        )
    except Exception:  # noqa: BLE001 -- the badge catches up on the next focus
        logger.warning("network hold activity live event failed owner=%s", owner_id)


async def hold_notice(owner_id: str, notice: dict) -> None:
    """The bridge's content-free notice ``{holdId, state, label, target}``: ``open`` files one
    Needs-you item per person asked (once per hold), ``closed`` resolves it for all of them."""
    from botocore.exceptions import ClientError

    from core.repositories import messaging_repo

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
    # The token rides an Authorization header, never the path: paths land in access logs.
    return {"url": f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/network/wake", "token": token}


def _refused() -> bool:
    """A flood of bad rings shows as a metric; nothing about the caller is logged."""
    from core.observability.metrics import put_metric

    put_metric("network.wake.refused", 1)
    return False


async def ring(token: str) -> bool:
    """False when the token is not a workspace's doorbell (the route answers 404, nothing written).
    True otherwise; at most one wake per workspace per ``WAKE_WINDOW_S`` reaches the work queue.
    The token is a credential: never logged."""
    from core.repositories import messaging_repo

    owner_id, dot, _ = token.partition(".")
    # 128 covers org_<64> + "." + a 43-char urlsafe secret; anything longer is refused before any read.
    if len(token) > 128 or not dot or not _WAKE_OWNER_RE.match(owner_id):
        return _refused()
    key = _netwake_key(owner_id)
    row = await messaging_repo._get(key["pk"], key["sk"])
    digest = _token_hash(token)
    if not row or not hmac.compare_digest(str(row.get("tokenHash") or ""), digest):
        return _refused()
    await _queue_wake(owner_id, digest)
    return True


async def _queue_wake(owner_id: str, digest: str) -> None:
    """At most one ``network_wake`` per workspace per ``WAKE_WINDOW_S`` reaches the work queue."""
    from botocore.exceptions import ClientError

    from core.repositories import messaging_repo

    key = _netwake_key(owner_id)
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
                        # lastWakeAt is each backend task's own clock, so skew can admit a second wake
                        # inside the window; that costs one extra no-op network.wake RPC.
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
        return
    logger.info("network wake queued owner=%s", owner_id)
