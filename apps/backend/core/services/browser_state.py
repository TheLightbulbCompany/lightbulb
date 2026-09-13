"""Best-effort browser session persistence across container rolls.

Chrome's ``--user-data-dir`` profile natively persists cookies / localStorage,
but ``container-stack.ts`` symlinks ``~/.openclaw/browser`` -> ``/tmp`` (to dodge
the SQLite-on-EFS/NFS "database is locked" corruption hazard), so the profile is
wiped on every container roll. We persist just the *state* (cookies + the active
tab's localStorage) to EFS and restore it, sidestepping NFS.

Entirely isol8-side, ZERO OpenClaw-fork changes: OpenClaw already exposes the
read/apply primitives as browser HTTP routes, reachable from the backend via the
``operator.admin`` ``browser.request`` gateway RPC (the persistent pool
connection already signs the operator-device handshake, so it carries that
scope — same door the pairing RPCs use):

  * ``GET  /cookies``           -> ``{cookies: [...]}`` (whole browser context)
  * ``GET  /storage/local``     -> ``{values: {k: v}}`` (ACTIVE tab's origin)
  * ``POST /cookies/set``       -> apply ONE cookie   (``{cookie}``)
  * ``POST /storage/local/set`` -> apply ONE kv pair  (``{key, value}``)

The JSON is backend-only: the backend does every get/set over the gateway RPC
and stores the snapshot on EFS purely as its own persistence. The container
never reads this file, so only the backend-side EFS path matters
(``/mnt/efs/users/{owner}/browser-state/state.json`` via ``workspace.py``).

Two event hooks (no long-running worker — feedback: prefer event-based):
  * ``snapshot_browser_state`` — ecs_manager roll paths, BEFORE the old task is
    stopped (``resize_user_container`` real-roll + ``force_redeploy_user_service``).
  * ``restore_browser_state`` — the ``/container/status`` readiness poll, once the
    new task is ``ready``. Applies then DELETES the file (consume-once).

Both are strictly best-effort: any failure is swallowed and NEVER blocks a roll
or a poll.

Session-cookie correctness is free: the routes are Playwright end to end.
``context().cookies()`` returns session cookies as ``expires: -1`` and
``addCookies`` treats ``-1`` as a session cookie natively, so a straight
round-trip preserves them — no expires normalization needed on our side.

ponytail: cookies are the reliable, high-value half (context-wide login
sessions). localStorage is snapshotted/restored against the ACTIVE tab only;
with no origin tracking it lands only when a tab is already on the matching
origin, so on a blank boot tab it is a near no-op. Robust per-origin
localStorage (snapshot keyed by URL, navigate-then-set on restore) is a
follow-up, not v1.

ponytail: crash-robustness ceiling — snapshot fires on a GRACEFUL roll only. A
Spot reclaim / OOM kill loses session changes since the last graceful roll. A
periodic snapshot would close that gap; deliberately deferred (would need the
long-running worker v1 avoids). Covered rolls: resize / image-apply / Ultra
swap / force-redeploy. The one-time config-lock migration roll is not covered.
"""

import asyncio
import json
import logging

from core.containers import get_workspace
from core.containers.workspace import WorkspaceError
from core.observability.metrics import put_metric
from core.services import container_rpc

logger = logging.getLogger(__name__)

# Backend-relative EFS path under the owner's user root (workspace.py resolves
# it to /mnt/efs/users/{owner}/browser-state/state.json). Not under agents/ or
# workspace-{id}/, so it never leaks into a marketplace agent export.
STATE_PATH = "browser-state/state.json"
STATE_VERSION = 1

# Per-RPC timeout. Short so a snapshot on an unreachable container adds only a
# couple seconds to a (rare, already-heavy) roll rather than blocking it.
_RPC_TIMEOUT_S = 4.0

# Bound the restore fan-out — one gateway RPC per cookie/key, since the routes
# are singular. A real session is tens of cookies; the cap guards against a
# pathological store hammering the connection. ponytail: raise if a legit
# session ever exceeds it (or add a fork batch route).
_MAX_COOKIES = 500
_MAX_LOCAL_KEYS = 500


async def _browser_request(*, owner_id: str, method: str, path: str, body: dict | None = None):
    """One ``browser.request`` gateway RPC. Returns the route's response body
    (e.g. ``{ok, targetId, cookies}``); raises on transport/RPC error."""
    params: dict = {"method": method, "path": path}
    if body is not None:
        params["body"] = body
    return await container_rpc.call(owner_id, "browser.request", params, timeout_s=_RPC_TIMEOUT_S)


async def snapshot_browser_state(owner_id: str) -> bool:
    """Capture cookies + active-tab localStorage from the RUNNING container and
    persist to EFS. Best-effort: returns ``False`` (never raises) when there is
    no running container, the browser is unreachable, or nothing was captured.

    Call BEFORE the roll stops the old task — the old task must still be up.
    """
    try:
        await container_rpc.running_container(owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort; covers ApiProblem (cold/unreachable) and a raw resolve failure
        logger.info("browser-state snapshot: resolve failed for %s: %s", owner_id, exc)
        return False

    state: dict = {"v": STATE_VERSION}

    async def _get(path: str):
        return await _browser_request(owner_id=owner_id, method="GET", path=path)

    # Both GETs share the one persistent connection; run them concurrently to
    # halve the worst-case delay added before the roll.
    cookies_res, local_res = await asyncio.gather(_get("/cookies"), _get("/storage/local"), return_exceptions=True)

    if isinstance(cookies_res, dict) and isinstance(cookies_res.get("cookies"), list) and cookies_res["cookies"]:
        state["cookies"] = cookies_res["cookies"]
    elif isinstance(cookies_res, Exception):
        logger.info("browser-state snapshot: cookies GET failed for %s: %s", owner_id, cookies_res)

    if isinstance(local_res, dict) and isinstance(local_res.get("values"), dict) and local_res["values"]:
        state["local"] = local_res["values"]
    elif isinstance(local_res, Exception):
        logger.info("browser-state snapshot: localStorage GET failed for %s: %s", owner_id, local_res)

    if "cookies" not in state and "local" not in state:
        # Captured nothing — do NOT clobber a prior good snapshot with an empty
        # one (a later roll before restore consumed the file would erase it).
        put_metric("browser_state.snapshot.empty")
        return False

    try:
        get_workspace().write_file(owner_id, STATE_PATH, json.dumps(state))
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception("browser-state snapshot: EFS write failed for %s", owner_id)
        return False

    put_metric("browser_state.snapshot.ok")
    logger.info(
        "browser-state snapshot saved for %s (cookies=%d local=%d)",
        owner_id,
        len(state.get("cookies", [])),
        len(state.get("local", {})),
    )
    return True


async def restore_browser_state(owner_id: str) -> bool:
    """Apply the EFS snapshot to the (freshly booted) container, then DELETE it
    so it restores at most once per snapshot. Best-effort: returns ``False`` and
    never raises. Fast no-op (one EFS stat) when no snapshot exists.

    Caller MUST only invoke this once the container is reachable
    (``runtime_phase == "ready"``), so the mutation POSTs land and consuming the
    file does not discard a snapshot the browser never received. Restoring
    cookies again is idempotent, so a duplicate concurrent call is harmless.
    """
    ws = get_workspace()
    try:
        raw = ws.read_file(owner_id, STATE_PATH)
    except WorkspaceError:
        return False  # steady state: nothing to restore
    except Exception:  # noqa: BLE001 — best-effort
        logger.exception("browser-state restore: EFS read failed for %s", owner_id)
        return False

    try:
        state = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("browser-state restore: corrupt JSON for %s — discarding", owner_id)
        _safe_delete(ws, owner_id)
        return False

    applied_cookies = 0
    applied_local = 0

    cookies = state.get("cookies") if isinstance(state, dict) else None
    if isinstance(cookies, list):
        for cookie in cookies[:_MAX_COOKIES]:
            if not isinstance(cookie, dict):
                continue
            try:
                await _browser_request(owner_id=owner_id, method="POST", path="/cookies/set", body={"cookie": cookie})
                applied_cookies += 1
            except Exception as exc:  # noqa: BLE001 — per-cookie best-effort
                logger.info("browser-state restore: cookie set failed for %s: %s", owner_id, exc)

    local = state.get("local") if isinstance(state, dict) else None
    if isinstance(local, dict):
        for key, value in list(local.items())[:_MAX_LOCAL_KEYS]:
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            try:
                await _browser_request(
                    owner_id=owner_id, method="POST", path="/storage/local/set", body={"key": key, "value": value}
                )
                applied_local += 1
            except Exception as exc:  # noqa: BLE001 — per-key best-effort
                logger.info("browser-state restore: storage set failed for %s: %s", owner_id, exc)

    # Consume-once: delete regardless of per-item outcome. The caller gates on
    # runtime_phase=="ready", so the browser was reachable — retrying the whole
    # apply every poll (~3s) would only hammer the gateway. A future roll
    # re-snapshots fresh.
    _safe_delete(ws, owner_id)

    put_metric("browser_state.restore.ok")
    logger.info(
        "browser-state restored for %s (cookies=%d local=%d)",
        owner_id,
        applied_cookies,
        applied_local,
    )
    return applied_cookies > 0 or applied_local > 0


def _safe_delete(ws, owner_id: str) -> None:
    try:
        ws.delete_file(owner_id, STATE_PATH)
    except Exception:  # noqa: BLE001 — best-effort
        logger.info("browser-state: delete failed for %s", owner_id)
