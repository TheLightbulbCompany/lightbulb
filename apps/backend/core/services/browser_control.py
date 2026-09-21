"""One workspace browser. DynamoDB fences controller changes and saved-state publication."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services import browser_sessions, container_rpc

LEASE_SECONDS = 300
TRANSITION_SECONDS = 300
SESSION_SECONDS = 3600


class BrowserError(Exception):
    """A safe, user-facing conflict; never wrap a credential-bearing SDK exception."""


async def _row(owner: str) -> dict:
    row = await container_repo.get_by_owner_id(owner, consistent=True)
    if not row or not row.get("browser_profile_id"):
        raise BrowserError("Browser is unavailable for this workspace.")
    state = await container_repo.get_browser_state(owner)
    if state and state.get("mode") == "transition" and state.get("operation_at", 0) + TRANSITION_SECONDS < time.time():
        recovered = {
            **state,
            "mode": "error",
            "error_code": "transition_expired",
            "error": "Browser operation expired. Restart restores the last completed save.",
        }
        if not await container_repo.compare_browser_state(owner, state, recovered):
            raise BrowserError("Browser control changed. Try again.")
        state = recovered
    return {**row, "browser_state": state}


async def _publish(owner: str, old: dict | None, new: dict) -> dict:
    if not await container_repo.compare_browser_state(owner, old, new):
        raise BrowserError("Browser control changed. Refresh and try again.")
    return new


async def _begin(owner: str, state: dict | None) -> dict:
    if state and state.get("mode") == "transition":
        raise BrowserError("Browser is changing control. Try again shortly.")
    return await _publish(
        owner,
        state,
        {
            **(state or {}),
            "generation": secrets.token_hex(32),
            "mode": "transition",
            "operation_at": int(time.time()),
            "error": None,
            "error_code": None,
        },
    )


async def _owned(owner: str, row: dict, state: dict) -> dict | None:
    sid = state.get("session_id")
    if not sid:
        return None
    return await browser_sessions.owns_session(owner, sid, profile_id=row["browser_profile_id"], strict=True)


async def _stream(session_id: str, enabled: bool, token: str) -> None:
    await run_in_thread(
        browser_sessions._data_client().update_browser_stream,
        browserIdentifier=browser_sessions.BUILTIN_BROWSER_ID,
        sessionId=session_id,
        streamUpdate={"automationStreamUpdate": {"streamStatus": "ENABLED" if enabled else "DISABLED"}},
        clientToken=token,
    )


async def _cdp(session_id: str) -> str:
    return await run_in_thread(browser_sessions._presign_stream, session_id, "automation")


def _state_url(key: str, action: str) -> str:
    params = {"Bucket": settings.OWNER_STATE_BUCKET, "Key": key}
    if action == "put_object":
        params["ContentType"] = "application/json"
    return boto3.client(
        "s3", region_name=settings.AWS_REGION, config=Config(signature_version="s3v4")
    ).generate_presigned_url(
        action,
        Params=params,
        ExpiresIn=180,
    )


async def _checkpoint(owner: str, state: dict) -> dict:
    # The runtime cannot replace a committed object: each operation writes its own key.
    key = f"browser-state/{owner}/{state['generation']}.json"
    params = {
        "action": "save",
        "cdpUrl": await _cdp(state["session_id"]),
        "stateUrl": await run_in_thread(_state_url, key, "put_object"),
    }
    previous = state.get("saved_key")
    if previous:
        if not previous.startswith(f"browser-state/{owner}/"):
            raise BrowserError("Saved browser state is unavailable.")
        params["previousStateUrl"] = await run_in_thread(_state_url, previous, "get_object")
    result = await container_rpc.call(owner, "browser.state", params, timeout_s=180)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BrowserError("Could not save browser state. Try Done again.")
    return {"saved_key": key, "saved_at": datetime.now(timezone.utc).isoformat()}


async def _delete_snapshot(key: str) -> None:
    try:
        await run_in_thread(
            boto3.client("s3", region_name=settings.AWS_REGION).delete_object,
            Bucket=settings.OWNER_STATE_BUCKET,
            Key=key,
        )
    except Exception:
        pass  # Wipe also sweeps this backend-only prefix after stopping the runtime.


async def _discard_uncommitted(owner: str, state: dict) -> None:
    key = f"browser-state/{owner}/{state['generation']}.json"
    current = await container_repo.get_browser_state(owner) or {}
    if current.get("saved_key") != key:
        await _delete_snapshot(key)


async def _commit_checkpoint(owner: str, state: dict, saved: dict) -> dict:
    next_state = await _publish(owner, state, {**state, **saved})
    previous = state.get("saved_key")
    if previous and previous != saved["saved_key"] and previous.startswith(f"browser-state/{owner}/"):
        await _delete_snapshot(previous)
    return next_state


async def _restore(owner: str, state: dict) -> None:
    key = state.get("saved_key")
    if not key:
        return
    if not key.startswith(f"browser-state/{owner}/"):
        raise BrowserError("Saved browser state is unavailable.")
    result = await container_rpc.call(
        owner,
        "browser.state",
        {
            "action": "load",
            "cdpUrl": await _cdp(state["session_id"]),
            "stateUrl": await run_in_thread(_state_url, key, "get_object"),
        },
        timeout_s=180,
    )
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise BrowserError("Could not restore saved browser state. Try again.")


async def _fail(owner: str, state: dict, message: str) -> None:
    await container_repo.compare_browser_state(owner, state, {**state, "mode": "error", "error": message})


async def _retire(session_id: str) -> None:
    await run_in_thread(
        browser_sessions._data_client().stop_browser_session,
        browserIdentifier=browser_sessions.BUILTIN_BROWSER_ID,
        sessionId=session_id,
    )


async def _claim(
    owner: str, controller: str, human: bool, *, generation: str | None = None, start_only: bool = False
) -> dict:
    row = await _row(owner)
    old = row.get("browser_state")
    if not old:
        for sid in await run_in_thread(browser_sessions._ready_session_ids):
            if await browser_sessions.owns_session(owner, sid, profile_id=row["browser_profile_id"], strict=True):
                raise BrowserError(
                    "The previous browser session is still running. Wait for it to finish before starting a shared browser."
                )
    if old and old.get("mode") == "error" and not human and old.get("error_code") != "transition_expired":
        raise BrowserError("The browser needs human recovery. Open the Browser panel to restart it.")
    if old and old.get("mode") == "error" and human and not start_only:
        raise BrowserError("Open the Browser panel and restart to recover the last saved state.")
    if start_only and old and old.get("mode") != "error" and await _owned(owner, row, old):
        if old.get("mode") != "human" or old.get("controller_id") != controller:
            raise BrowserError("A browser is already running. Refresh and use Take control.")
    if generation is not None and (not old or old.get("generation") != generation):
        raise BrowserError("Browser control changed. Refresh and try again.")
    if old and old.get("mode") in ("human", "agent"):
        same = old.get("controller_id") == controller and old["mode"] == ("human" if human else "agent")
        if not same and (not human or old["mode"] == "human") and old.get("lease_until", 0) > time.time():
            raise BrowserError("The browser is controlled by another workspace member or agent.")
        if same and await _owned(owner, row, old):
            return await _publish(owner, old, {**old, "lease_until": int(time.time()) + LEASE_SECONDS})
    state = await _begin(owner, old)
    try:
        got = await _owned(owner, row, state)
        # Failed/abandoned operations may still hold old AWS capabilities. Recover into a
        # different session; an old stream update can then never mutate the replacement.
        if got and old and old.get("mode") == "error":
            await _retire(state["session_id"])
            got = None
        if not got:
            start_token = state.get("start_token") or secrets.token_hex(32)
            state = await _publish(owner, state, {**state, "start_token": start_token})
            result = await run_in_thread(
                browser_sessions._data_client().start_browser_session,
                browserIdentifier=browser_sessions.BUILTIN_BROWSER_ID,
                profileConfiguration={"profileIdentifier": row["browser_profile_id"]},
                name=f"workspace-{start_token[:16]}",
                clientToken=start_token,
                sessionTimeoutSeconds=SESSION_SECONDS,
                viewPort={"width": browser_sessions.REMOTE_WIDTH, "height": browser_sessions.REMOTE_HEIGHT},
            )
            state = await _publish(owner, state, {**state, "session_id": result["sessionId"], "start_token": None})
            if not await _owned(owner, row, state):
                raise BrowserError("Browser is still starting. Try again shortly.")
            await _restore(owner, state)
        await _stream(state["session_id"], not human, state["generation"])
        return await _publish(
            owner,
            state,
            {
                **state,
                "mode": "human" if human else "agent",
                "controller_id": controller,
                "lease_until": int(time.time()) + LEASE_SECONDS,
            },
        )
    except BrowserError:
        await _fail(owner, state, "Browser could not change control. Start again to recover the last saved state.")
        raise
    except Exception:
        await _fail(owner, state, "Browser could not change control. Start again to recover the last saved state.")
        raise BrowserError("Browser could not change control. Try again.") from None


async def start(owner: str, user: str) -> dict:
    # Old images retain v1 until the explicit image rollout. Never start a browser whose
    # runtime cannot checkpoint it, even if backend deployment happened first.
    health = await container_rpc.call(owner, "health")
    if not isinstance(health, dict) or health.get("browserProtocol") != 2:
        raise BrowserError("This workspace needs the browser update before it can start a shared session.")
    await _claim(owner, user, True, start_only=True)
    return await view(owner, user)


async def take(owner: str, user: str, generation: str) -> dict:
    await _claim(owner, user, True, generation=generation)
    return await view(owner, user)


async def navigate(owner: str, user: str, generation: str, url: str) -> dict:
    row = await _row(owner)
    old = row.get("browser_state") or {}
    if old.get("mode") != "human" or old.get("controller_id") != user or old.get("generation") != generation:
        raise BrowserError("Browser control changed. Refresh and try again.")
    if not await _owned(owner, row, old):
        raise BrowserError("The browser session expired. Start restores the last completed save.")
    state = await _begin(owner, old)
    error = None
    try:
        await _stream(state["session_id"], True, state["generation"])
        result = await container_rpc.call(
            owner,
            "browser.navigate",
            {
                "cdpUrl": await _cdp(state["session_id"]),
                "url": url,
            },
            timeout_s=180,
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            error = "Could not open that website. Try again."
    except Exception:
        error = "Could not open that website. Try again."
    if await container_repo.get_browser_state(owner) != state:
        raise BrowserError("Browser control changed. Refresh and try again.")
    try:
        await _stream(state["session_id"], False, secrets.token_hex(32))
    except Exception:
        await _fail(owner, state, "Browser control recovery is required.")
        raise BrowserError("Could not restore human browser control. Try restarting from the panel.") from None
    await _publish(
        owner,
        state,
        {
            **state,
            "mode": "human",
            "error": error,
            "lease_until": int(time.time()) + LEASE_SECONDS,
        },
    )
    if error:
        raise BrowserError(error)
    return await view(owner, user)


async def acquire_agent(owner: str, run_id: str) -> dict:
    state = await _claim(owner, run_id, False)
    return {"generation": state["generation"], "cdpUrl": await _cdp(state["session_id"])}


async def _release(owner: str, controller: str, generation: str, *, human: bool, stop: bool = False) -> None:
    row = await _row(owner)
    old = row.get("browser_state") or {}
    if (
        old.get("generation") != generation
        or old.get("controller_id") != controller
        or old.get("mode") != ("human" if human else "agent")
    ):
        raise BrowserError("Browser control changed. Refresh and try again.")
    if not await _owned(owner, row, old):
        raise BrowserError("The browser session expired. Its last completed save is available on restart.")
    state = await _begin(owner, old)
    try:
        await _stream(state["session_id"], True, state["generation"])
        saved = await _checkpoint(owner, state)
        # Commit the save before stop. A stale RPC can upload its own object, but cannot
        # move this pointer or stop the session now owned by another controller.
        state = await _commit_checkpoint(owner, state, saved)
        if stop:
            await _retire(state["session_id"])
        await _publish(
            owner,
            state,
            {
                **state,
                "mode": "idle",
                "controller_id": None,
                "lease_until": 0,
                "session_id": None if stop else state["session_id"],
            },
        )
    except Exception:
        # Preserve the controller and previous checkpoint for an explicit retry. Disable
        # automation again for a human; do not report Done or stop on a failed save.
        await _discard_uncommitted(owner, state)
        current = (await _row(owner)).get("browser_state")
        if current != state:
            raise BrowserError("Browser control changed. Refresh and try again.") from None
        if human:
            try:
                await _stream(state["session_id"], False, secrets.token_hex(32))
            except Exception:
                await _fail(owner, state, "Could not save browser state or restore human control. Try Start again.")
                raise BrowserError("Could not save browser state. Control recovery is required.") from None
        await container_repo.compare_browser_state(
            owner,
            state,
            {
                **state,
                "mode": "human" if human else "agent",
                "error": "Could not save browser state. Try again.",
            },
        )
        raise BrowserError("Could not save browser state. Try again.") from None


async def release_human(owner: str, user: str, generation: str) -> dict:
    await _release(owner, user, generation, human=True)
    return await view(owner, user)


async def stop(owner: str, user: str, generation: str) -> dict:
    await _release(owner, user, generation, human=True, stop=True)
    return await view(owner, user)


async def release_agent(owner: str, run_id: str, generation: str) -> dict:
    await _release(owner, run_id, generation, human=False)
    return {"ok": True}


async def view(owner: str, user: str) -> dict:
    row = await _row(owner)
    state = row.get("browser_state")
    if not state:
        # Preserve existing v1 discovery during staged image rollout.
        return {
            **await browser_sessions.live_view(owner),
            "mode": "idle",
            "isController": False,
            "canControl": False,
            "generation": None,
        }
    got = await _owned(owner, row, state)
    is_controller = state.get("mode") == "human" and state.get("controller_id") == user
    if is_controller and state.get("lease_until", 0) < time.time() + 60:
        renewed = {**state, "lease_until": int(time.time()) + LEASE_SECONDS}
        if await container_repo.compare_browser_state(owner, state, renewed):
            state = renewed
    out = {
        **browser_sessions._INACTIVE,
        "sessionId": state.get("session_id"),
        "generation": state["generation"],
        "mode": state.get("mode", "idle"),
        "controllerId": state.get("controller_id"),
        "isController": bool(got and is_controller),
        "canControl": state.get("mode") in ("idle", "agent", "error")
        or is_controller
        or state.get("lease_until", 0) < time.time(),
        "savedAt": state.get("saved_at"),
        "error": state.get("error"),
    }
    if got:
        out.update(await browser_sessions.session_view(state["session_id"], got))
    elif state.get("session_id"):
        out["error"] = "Browser session expired. Start restores the last completed save."
    return out


async def teardown(owner: str, row: dict, *, delete_state: bool = False) -> None:
    """Stop the billed browser at runtime teardown, retaining the last completed save."""
    old = await container_repo.get_browser_state(owner)
    if not old:
        return
    state = {**old, "generation": secrets.token_hex(32), "mode": "transition", "operation_at": int(time.time())}
    if not await container_repo.compare_browser_state(owner, old, state):
        raise BrowserError("Browser changed during teardown.")
    try:
        if row.get("browser_profile_id") and await _owned(owner, row, state):
            if not delete_state:
                # Revoke any in-flight automation connection before the checkpoint client
                # attaches: AgentCore admits only one automation connection per session.
                await _stream(state["session_id"], False, secrets.token_hex(32))
                await _stream(state["session_id"], True, state["generation"])
                saved = await _checkpoint(owner, state)
                state = await _commit_checkpoint(owner, state, saved)
            await _retire(state["session_id"])
        await _publish(
            owner, state, {**state, "mode": "idle", "session_id": None, "controller_id": None, "lease_until": 0}
        )
    except Exception:
        await _discard_uncommitted(owner, state)
        if (await container_repo.get_browser_state(owner)) == state:
            if old.get("mode") == "human" and state.get("session_id"):
                try:
                    await _stream(state["session_id"], False, secrets.token_hex(32))
                except Exception:
                    await _fail(owner, state, "Browser control recovery is required.")
                    raise BrowserError("Could not save browser before stopping it.") from None
            await container_repo.compare_browser_state(
                owner,
                state,
                {
                    **state,
                    "mode": old.get("mode", "idle"),
                    "error": "Could not save browser before stopping it.",
                },
            )
        raise BrowserError("Could not save browser before stopping it.") from None
