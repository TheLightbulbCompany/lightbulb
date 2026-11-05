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
import logging
import time
from dataclasses import dataclass

import httpx

from core.observability.metrics import put_metric
from core.services import oauth_service, owner_lifecycle
from core.services.container_provision import resolve_provider_choice
from core.services.credentials.access_token import ReconnectRequired, _announce_reconnect

logger = logging.getLogger(__name__)

PROVIDER_KEY = "openai-codex"
REFRESH_WINDOW_S = 600
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
    current = row["tokens"].get("access_token") or ""
    if force and (presented_sha256 is None or presented_sha256 == _sha(current)):
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
        body = resp.json()
        if not body.get("access_token"):
            raise RefreshUnavailable("missing_access_token")
        return body
    code = ""
    try:
        err = resp.json().get("error")
        code = (err.get("code") or err.get("type") or "") if isinstance(err, dict) else str(err or "")
    except Exception:  # noqa: BLE001
        pass
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
        if row is None or row.get("custody") != "backend":
            return None  # revoked or rolled back while we waited
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
            await _announce_reconnect(owner_id, PROVIDER_KEY)
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
