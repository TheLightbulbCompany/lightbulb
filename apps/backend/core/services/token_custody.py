"""Backend token broker for ChatGPT (Codex) login under backend custody (spec 13 T2, §5.3).

With per-task VMs, several microVMs hold one workspace's ChatGPT grant at once. OpenAI's refresh
token is single-use, so a second VM refreshing it gets ``refresh_token_reused`` and the whole
family can die. Under ``custody: backend`` (the token row, oauth_service) the backend is the only
refresher and every VM is vended a short-lived access token:

- **Fast path, no lease:** the stored token has more than ``REFRESH_WINDOW_S`` left and nobody
  presented it as stale -> one consistent read, returned.
- **Refresh:** under ``owner_lifecycle_guard`` (30 s wait), re-read, decide again (a peer may have
  refreshed while we waited), POST the refresh once, write it with a conditional update on
  ``version`` (the CAS is the backstop if the lease were ever lost).
- **Forced refresh** (the VM got a 401): only when ``sha256(current access token) ==
  presented_sha256``; a VM presenting an older token gets the current one with no OpenAI call. N
  VMs hitting one 401 therefore cause exactly one refresh.

Logs and metrics carry ids and numbers only, never a token value.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services import hermes_profiles, hermes_provisioning, oauth_service, owner_lifecycle
from core.services.container_provision import resolve_provider_choice
from core.services.credentials.access_token import ReconnectRequired, _announce_reconnect

logger = logging.getLogger(__name__)

PROVIDER_KEY = "openai-codex"
REFRESH_WINDOW_S = 600
#: The first dev image tag (``h-<YYYY.M.D>-<sha7>``) whose bridge reads the ``auth.json`` marker
#: and asks the broker (F6 + V11). None until V16's roll records it: the cutover refuses every
#: image before then (spec 13 §6.3 -- an old image reads the marker as "no refresh token").
BROKER_MIN_IMAGE_TAG: str | None = None
#: How long the cutover holds the cold-wake lease: nothing may start a session mid-move.
WAKE_LEASE_S = 60
_TAG_RE = re.compile(r"^h-(\d{4})\.(\d{1,2})\.(\d{1,2})-([0-9a-f]{7})$")
_REFRESH_TIMEOUT_S = 20.0
_TERMINAL_CODES = frozenset({"invalid_grant", "invalid_token", "invalid_request", "refresh_token_reused"})


@dataclass(frozen=True)
class CodexVend:
    access_token: str
    id_token: str | None
    account_id: str | None
    expires_at: int  # epoch seconds; 0 = unknown
    refreshed: bool


class RefreshUnavailable(RuntimeError):
    """OpenAI could not refresh right now (429, 5xx, timeout): the grant is still good."""


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _metric(name: str, value: float = 1.0, unit: str = "Count", **dims: str) -> None:
    try:
        put_metric(f"token_broker.{name}", value, unit=unit, dimensions=dims or None)
    except Exception:  # noqa: BLE001 -- a metrics outage never fails a vend
        logger.debug("token_broker metric failed", exc_info=True)


def _expires_at_s(row: dict) -> int:
    """Epoch seconds the stored access token dies, from the row or the token's own ``exp``; 0 when
    neither says (unknown is NOT "refresh now": the VM's 401 drives a forced refresh instead)."""
    ms = int(row.get("expires_at") or 0) or oauth_service._expires_at_ms(row["tokens"].get("access_token") or "") or 0
    return ms // 1000


def _needs_refresh(row: dict, presented_sha256: str | None, force: bool) -> bool:
    """A forced refresh counts only with the hash of the CURRENT token (spec 13 §5.2): without
    one, a bridge bug could rotate the family on every call."""
    current = row["tokens"].get("access_token") or ""
    if force and presented_sha256 is not None and presented_sha256 == _sha(current):
        return True
    expires_at = _expires_at_s(row)
    return bool(expires_at) and expires_at - time.time() < REFRESH_WINDOW_S


def _vend(tokens: dict, row: dict, *, refreshed: bool) -> CodexVend:
    return CodexVend(
        access_token=tokens["access_token"],
        id_token=tokens.get("id_token"),
        account_id=tokens.get("account_id") or row.get("account_id") or None,
        expires_at=_expires_at_s({"tokens": tokens, "expires_at": tokens.get("expires_at")}),
        refreshed=refreshed,
    )


async def _post_refresh(refresh_token: str) -> dict:
    """The same request Hermes's ``refresh_codex_oauth_pure`` sends. Terminal refusals raise
    :class:`ReconnectRequired`-bound ``ValueError``; anything retryable :class:`RefreshUnavailable`."""
    try:
        async with httpx.AsyncClient(timeout=_REFRESH_TIMEOUT_S) as client:
            resp = await client.post(
                oauth_service.OAUTH_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": oauth_service.CLIENT_ID,
                },
            )
    except httpx.HTTPError as exc:
        raise RefreshUnavailable(type(exc).__name__) from exc
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            raise RefreshUnavailable("bad_json") from None
        if not isinstance(body, dict) or not body.get("access_token"):
            raise RefreshUnavailable("missing_access_token")
        return body
    code = ""
    try:
        err = resp.json().get("error")
        code = (err.get("code") or err.get("type") or "") if isinstance(err, dict) else str(err or "")
    except Exception:  # noqa: BLE001
        pass
    code = str(code)[:200]  # response text is unbounded; it reaches logs and exception messages
    if code in _TERMINAL_CODES or resp.status_code in (401, 403):
        raise ValueError(code or f"http_{resp.status_code}")
    raise RefreshUnavailable(f"http_{resp.status_code}")


async def codex_vend(owner_id: str, *, presented_sha256: str | None = None, force: bool = False) -> CodexVend | None:
    """The workspace's current ChatGPT access token, refreshing once if due.

    None when the workspace is not on ChatGPT login or its grant is not backend-custodied (the VM
    path applies). Raises ``ReconnectRequired`` (dead grant), ``OwnerLifecycleBusyError`` (lease
    held 30 s), :class:`RefreshUnavailable` (OpenAI down; the grant is fine)."""
    choice, _ = await resolve_provider_choice(owner_id)
    if choice != "chatgpt_oauth":
        return None
    user = await oauth_service.resolve_token_owner(owner_id=owner_id)
    row = await oauth_service.get_token_row(user, consistent=True, bound_to=owner_id)
    if row is None:
        if await oauth_service.token_row_needs_reconnect(user, bound_to=owner_id):
            raise ReconnectRequired(PROVIDER_KEY)
        return None
    if row.get("custody") != "backend":
        return None
    if not _needs_refresh(row, presented_sha256, force):
        _metric("vend", path="fast")
        if force:
            _metric("forced_noop")
        return _vend(row["tokens"], row, refreshed=False)

    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        row = await oauth_service.get_token_row(user, consistent=True, bound_to=owner_id)
        if row is None:
            # A peer's refresh was refused while we waited: say "reconnect" now, not next call.
            if await oauth_service.token_row_needs_reconnect(user, bound_to=owner_id):
                raise ReconnectRequired(PROVIDER_KEY)
            return None  # revoked while we waited
        if row.get("custody") != "backend":
            return None  # rolled back while we waited
        if not _needs_refresh(row, presented_sha256, force):  # a peer refreshed while we waited
            _metric("vend", path="fast")
            return _vend(row["tokens"], row, refreshed=False)
        started = time.monotonic()
        try:
            body = await _post_refresh(row["tokens"].get("refresh_token") or "")
        except ValueError as exc:
            _metric("reconnect")
            logger.warning("token_broker refresh refused owner=%s code=%s", owner_id, exc)
            await oauth_service.mark_reconnect_required(user, expected_version=row["version"])
            # No MCP wiring for ChatGPT login: the Needs-you item alone (activity renders it
            # from the token row, not an integrations row).
            await _announce_reconnect(owner_id, PROVIDER_KEY, reconcile=False)
            raise ReconnectRequired(PROVIDER_KEY) from exc
        _metric("refresh_ms", (time.monotonic() - started) * 1000, unit="Milliseconds")
        # Merge onto what we hold: a response without refresh_token / id_token / account_id keeps
        # ours (OpenAI rotates the refresh token only sometimes; dropping it would end the grant).
        fresh = {k: body.get(k) for k in ("access_token", "refresh_token", "id_token", "account_id") if body.get(k)}
        if body.get("expires_in"):
            fresh["expires_at"] = int(time.time() * 1000) + int(body["expires_in"]) * 1000
        tokens = {**row["tokens"], **fresh}
        if not await oauth_service.cas_update_tokens(user, expected_version=row["version"], tokens=tokens):
            _metric("cas_lost")
            raise RuntimeError("token_broker: conditional write lost (row revoked or lease taken over)")
        _metric("vend", path="refresh")
        return _vend(tokens, row, refreshed=True)


# --- cutover / rollback (spec 13 §6.1) -----------------------------------------------------------
#
# Two writers must be fenced. Backend writers (connect, disconnect, the broker's refresh) wait on
# ``owner_lifecycle_guard``. The VM writer (``save_root_auth``) is fenced by an idle wait, a stopped
# session and a held cold-wake lease, so no session can start and rotate the grant mid-move.


def _tag_date(tag: str) -> tuple[int, int, int] | None:
    m = _TAG_RE.fullmatch(tag or "")
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _image_has_broker(image_uri: str) -> bool:
    """Whether the configured Hermes image's tag is the broker-capable one or a later build."""
    if not BROKER_MIN_IMAGE_TAG:
        return False
    tag = (image_uri or "").rsplit(":", 1)[-1]
    if tag == BROKER_MIN_IMAGE_TAG:
        return True
    have, want = _tag_date(tag), _tag_date(BROKER_MIN_IMAGE_TAG)
    return bool(have and want and have > want)


@asynccontextmanager
async def fenced_session(owner_id: str) -> AsyncIterator[bool]:
    """Wait for the workspace session to go idle, stop it, and hold its cold-wake lease for the
    block. Yields False -- nothing touched -- when a turn is still running at the idle deadline
    (a mid-turn stop loses that turn's profile writes), the stop is unconfirmed, or the lease is
    held. Spec 13 §6.1 step 2.1-2.2 and 2.6."""
    row = await container_repo.get_by_owner_id(owner_id) or {}
    arn = row.get("agentcore_runtime_arn")
    if arn and not await hermes_provisioning._wait_for_idle_session(owner_id):
        yield False
        return
    if arn and not await hermes_provisioning.stop_runtime_session(owner_id, arn):
        yield False
        return
    token = uuid.uuid4().hex
    held = await run_in_thread(
        container_repo.try_take_wake_lease_sync,
        owner_id,
        arn=arn or "",
        token=token,
        now=int(time.time()),
        lease_s=WAKE_LEASE_S,
    )
    if not held:
        yield False
        return
    try:
        yield True
    finally:
        await run_in_thread(container_repo.finish_wake_sync, owner_id, token=token)


_GRANT_KEYS = ("access_token", "refresh_token", "id_token")


async def codex_cutover(owner_id: str, *, dry_run: bool = False) -> str:
    """Move the workspace's ChatGPT grant into backend custody.

    ``done`` | ``already`` | ``not_chatgpt`` | ``no_grant`` | ``stale_image`` | ``busy_session`` |
    ``raced`` | ``would_cutover`` (dry run: every precondition passed, nothing written)."""
    choice, _ = await resolve_provider_choice(owner_id)
    if choice != "chatgpt_oauth":
        return "not_chatgpt"
    user = await oauth_service.resolve_token_owner(owner_id=owner_id)
    row = await oauth_service.get_token_row(user, consistent=True, bound_to=owner_id)
    if row is None:
        return "no_grant"
    if row.get("custody") == "backend":
        return "already"
    if not _image_has_broker(settings.AGENTCORE_HERMES_IMAGE):
        return "stale_image"
    if dry_run:
        return "would_cutover"
    # The runtime itself must be on the broker-capable image: a roll brings it there, and a
    # session that is busy at the roll's deadline is still on the old image.
    if await hermes_provisioning.roll_runtime_image(owner_id) not in ("current", "updated"):
        return "stale_image"
    bucket = settings.OWNER_STATE_BUCKET
    key = f"{hermes_profiles.root_home_prefix(owner_id)}auth.json"
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        async with fenced_session(owner_id) as fenced:
            if not fenced:
                return "busy_session"
            existing, etag = await run_in_thread(hermes_profiles._s3_get_versioned, bucket, key)
            blob = json.loads(existing) if existing else {}
            staged = ((blob.get("providers") or {}).get(PROVIDER_KEY) or {}).get("tokens") or {}
            # The VM's copy is the newest pair whenever it holds a grant (it rotates in place);
            # a never-rotated grant is still the one DDB has from connect.
            source = staged if staged.get("refresh_token") else row["tokens"]
            pair = {k: v for k, v in source.items() if k in _GRANT_KEYS and v}
            if not pair.get("refresh_token"):
                return "no_grant"
            tokens = {**row["tokens"], **pair}
            if row.get("account_id"):
                tokens.setdefault("account_id", row["account_id"])
            if not await oauth_service.take_custody(user, tokens=tokens):
                return "raced"
            try:
                await run_in_thread(hermes_profiles.stage_codex_marker, owner_id, expected_etag=etag, existing=blob)
            except hermes_profiles.MarkerRaced:
                # A writer we failed to fence: undo the row so the old path (which still reads
                # S3's newest pair) stays the one path.
                await oauth_service.set_custody_actor(user, expected_version=1)
                _metric("cutover", result="raced")
                return "raced"
    if await codex_vend(owner_id) is None:
        raise RuntimeError("token_broker: cutover verify vend returned nothing")
    _metric("cutover", result="done")
    logger.info("token_broker cutover owner=%s result=done", owner_id)
    return "done"


async def codex_rollback(owner_id: str) -> str:
    """Back to VM custody: stage the row's newest pair as a plain grant (replacing the marker),
    then flip the row. ``done`` | ``already`` | ``busy_session`` | ``raced``."""
    user = await oauth_service.resolve_token_owner(owner_id=owner_id)
    row = await oauth_service.get_token_row(user, consistent=True, bound_to=owner_id)
    if row is None or row.get("custody") != "backend":
        return "already"
    async with owner_lifecycle.owner_lifecycle_guard(owner_id):
        async with fenced_session(owner_id) as fenced:
            if not fenced:
                return "busy_session"
            await run_in_thread(hermes_profiles.stage_codex_auth, owner_id, row["tokens"])
            if not await oauth_service.set_custody_actor(user, expected_version=row["version"]):
                return "raced"
    _metric("cutover", result="rollback")
    logger.info("token_broker rollback owner=%s result=done", owner_id)
    return "done"
