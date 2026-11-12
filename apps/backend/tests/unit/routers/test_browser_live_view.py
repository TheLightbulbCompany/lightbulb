"""GET /api/v1/browser/live-view — the owner's watch-only AgentCore Browser panel."""

from unittest.mock import patch

import pytest

INACTIVE = {
    "active": False,
    "url": None,
    "expiresAt": None,
    "remoteWidth": 1920,
    "remoteHeight": 1080,
}


@pytest.mark.asyncio
async def test_internal_browser_refuses_wrong_workspace_hook(app):
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import AsyncMock

    with patch(
        "routers.messaging.container_repo.get_by_owner_id", AsyncMock(return_value={"hook_token": "owner-secret"})
    ):
        with patch("routers.container.browser_control.acquire_agent") as acquire:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/internal/browser/org_other/acquire",
                    headers={"Authorization": "Bearer wrong"},
                    json={"runId": "run"},
                )
    assert response.status_code == 401
    acquire.assert_not_called()


@pytest.mark.asyncio
async def test_input_uses_clerk_workspace_and_skips_the_provision_gate(async_client_org_admin):
    """A person's input in the live view is a signal, not a grant: owner from the claim only,
    and no billing gate on a request the panel sends every few seconds."""
    with (
        patch("routers.container._assert_provision_allowed") as gate,
        patch("routers.container.browser_control.record_input", return_value={"ok": True}) as record,
    ):
        response = await async_client_org_admin.post("/api/v1/browser/input", json={"owner_id": "org_other"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    record.assert_awaited_once_with("org_test_456")
    gate.assert_not_called()


@pytest.mark.asyncio
async def test_take_control_and_navigate_are_gone(async_client_org_admin):
    for path in ("/api/v1/browser/control", "/api/v1/browser/navigate"):
        response = await async_client_org_admin.post(path, json={"action": "take", "generation": "g1"})
        assert response.status_code in (404, 405)


@pytest.mark.asyncio
async def test_viewing_uses_clerk_workspace_and_the_start_gate(async_client_org_admin):
    with (
        patch("routers.container._assert_provision_allowed") as gate,
        patch("routers.container.browser_control.watch", return_value=INACTIVE) as watch,
    ):
        response = await async_client_org_admin.post("/api/v1/browser/viewing", json={"owner_id": "org_other"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    watch.assert_awaited_once_with("org_test_456", "user_test_123", frames=False)
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_viewing_without_the_live_view_asks_for_frames(async_client_org_admin):
    with (
        patch("routers.container._assert_provision_allowed"),
        patch("routers.container.browser_control.watch", return_value=INACTIVE) as watch,
    ):
        await async_client_org_admin.post("/api/v1/browser/viewing", json={"frames": True})
    watch.assert_awaited_once_with("org_test_456", "user_test_123", frames=True)


@pytest.mark.asyncio
async def test_frame_input_uses_clerk_workspace_and_is_bounded(async_client_org_member):
    with patch("routers.container.browser_control.send_input", return_value={"ok": True}) as send:
        ok = await async_client_org_member.post(
            "/api/v1/browser/input",
            json={"runId": "run-1", "type": "click", "x": 10, "y": 20, "owner_id": "org_other"},
        )
        bad = [
            await async_client_org_member.post("/api/v1/browser/input", json=body)
            for body in (
                {"type": "exec", "x": 1},
                {"type": "text", "text": "x" * 1001},
                {"type": "click", "x": -1},
                {"type": "click", "runId": "../x"},
            )
        ]
    assert ok.status_code == 200
    send.assert_awaited_once_with(
        "org_test_456", {"runId": "run-1", "type": "click", "x": 10.0, "y": 20.0, "button": "left", "deltaY": 0.0}
    )
    assert [r.status_code for r in bad] == [422, 422, 422, 422]


@pytest.mark.asyncio
async def test_frame_input_conflict_is_a_409(async_client_org_admin):
    from core.services import browser_control

    with patch(
        "routers.container.browser_control.send_input",
        side_effect=browser_control.BrowserError(browser_control.NOT_CASTING),
    ):
        response = await async_client_org_admin.post("/api/v1/browser/input", json={"type": "key", "key": "Enter"})
    assert response.status_code == 409 and "Take live control" in response.json()["detail"]


@pytest.mark.asyncio
async def test_agent_acquire_answers_202_while_the_browser_starts(app):
    from unittest.mock import AsyncMock

    from httpx import ASGITransport, AsyncClient

    from core.services import browser_control

    with (
        patch("routers.messaging._require_hook_token", AsyncMock()),
        patch("routers.container.browser_control.acquire_agent", AsyncMock(return_value=browser_control.STARTING)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/internal/browser/org_x/acquire", json={"runId": "run"})
    assert response.status_code == 202 and response.json() == {"status": "starting"}


@pytest.mark.asyncio
async def test_helper_routes_take_only_a_helper_token_of_a_live_session(async_client):
    from core.services.browser_extensions import helper_token

    assert (await async_client.get("/api/v1/browser/helper/logins?origin=https://a.example")).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (
        await async_client.post("/api/v1/browser/helper/extensions", json={"idOrUrl": "x"}, headers=bad)
    ).status_code == 401
    ok = {"Authorization": f"Bearer {helper_token('org_h', 'n')}"}
    logins = [
        {"id": "vault_aaaaaaaaaaaa", "origin": "https://a.example", "username": "me"},
        {"id": "vault_bbbbbbbbbbbb", "origin": "https://b.example", "username": "other"},
    ]
    live = {"mode": "idle", "session_id": "s1", "helper_nonce": "n"}
    with (
        patch("routers.container.container_repo.get_browser_state", return_value=live),
        patch("core.services.website_logins.list_logins", return_value=logins),
        patch("core.services.website_logins.resolve_login") as resolve,
    ):
        listed = await async_client.get("/api/v1/browser/helper/logins?origin=https://a.example/login", headers=ok)
        # The helper never resolves a password: there is no fill route.
        filled = await async_client.post(
            "/api/v1/browser/helper/logins/fill",
            json={"origin": "https://a.example", "id": "vault_aaaaaaaaaaaa"},
            headers=ok,
        )
    assert listed.json() == {"logins": [{"id": "vault_aaaaaaaaaaaa", "username": "me"}]}
    assert filled.status_code in (404, 405)
    resolve.assert_not_called()
    # The token lives inside the browser: once that session is gone it opens nothing.
    for ended in (None, {"mode": "idle", "session_id": None}):
        with patch("routers.container.container_repo.get_browser_state", return_value=ended):
            gone = await async_client.get("/api/v1/browser/helper/logins?origin=https://a.example", headers=ok)
        assert gone.status_code == 401


@pytest.mark.asyncio
async def test_helper_install_needs_a_persons_fresh_click_or_it_asks(async_client):
    """Anything inside the browser can press the helper's button (an agent, a page that
    prompt-injected it, whatever read the token), so only a click right after a person's own
    input in the panel installs; every other one files the Needs-you ask."""
    import time

    from core.services.browser_extensions import helper_token

    ok = {"Authorization": f"Bearer {helper_token('org_h', 'n')}"}
    agent = {
        "mode": "agent",
        "agent_id": "friday",
        "lease_until": int(time.time()) + 100,
        "session_id": "s1",
        "helper_nonce": "n",
    }
    idle = {"mode": "idle", "session_id": "s1", "helper_nonce": "n"}
    for state, touched, agent_id in (
        (agent, time.time(), "friday"),
        (idle, time.time() - 60, None),
        (idle, None, None),
    ):
        with (
            patch("routers.container.container_repo.get_browser_state", return_value=state),
            patch("routers.container.container_repo.get_browser_input_at", return_value=touched),
            patch("routers.container.browser_extensions.request_install", return_value={"status": "requested"}) as ask,
            patch("routers.container.browser_extensions.add_from_webstore") as add,
        ):
            resp = await async_client.post("/api/v1/browser/helper/extensions", json={"idOrUrl": "abc"}, headers=ok)
        assert resp.json() == {"status": "requested"}
        ask.assert_awaited_once_with("org_h", "abc", agent_id=agent_id)
        add.assert_not_called()

    with (
        patch("routers.container.container_repo.get_browser_state", return_value=idle),
        patch("routers.container.container_repo.get_browser_input_at", return_value=time.time() - 1),
        patch("routers.container.browser_extensions.add_from_webstore", return_value={"slug": "abc"}) as add,
        patch("routers.container.browser_control.restart_soon", return_value=True),
    ):
        resp = await async_client.post("/api/v1/browser/helper/extensions", json={"idOrUrl": "abc"}, headers=ok)
    assert resp.json() == {"slug": "abc", "restarting": True, "status": "installed"}
    add.assert_awaited_once_with("org_h", "abc", "browser")


@pytest.mark.asyncio
async def test_helper_fill_needs_a_persons_fresh_click_and_no_agent_and_answers_no_secret(async_client):
    import time

    from core.services.browser_extensions import helper_token

    ok = {"Authorization": f"Bearer {helper_token('org_h', 'n')}"}
    agent = {
        "mode": "agent",
        "agent_id": "friday",
        "lease_until": int(time.time()) + 100,
        "session_id": "s1",
        "helper_nonce": "n",
    }
    idle = {"mode": "idle", "session_id": "s1", "helper_nonce": "n"}
    for state, touched, status in ((agent, time.time(), 409), (idle, time.time() - 60, 403), (idle, None, 403)):
        with (
            patch("routers.container.container_repo.get_browser_state", return_value=state),
            patch("routers.container.container_repo.get_browser_input_at", return_value=touched),
            patch("routers.container.browser_control.fill_login") as fill,
        ):
            resp = await async_client.post(
                "/api/v1/browser/helper/logins/fill-request", json={"id": "vault_aaaaaaaaaaaa"}, headers=ok
            )
        assert resp.status_code == status
        fill.assert_not_called()

    with (
        patch("routers.container.container_repo.get_browser_state", return_value=idle),
        patch("routers.container.container_repo.get_browser_input_at", return_value=time.time() - 1),
        patch("routers.container.browser_control.fill_login", return_value={"ok": True}) as fill,
    ):
        resp = await async_client.post(
            "/api/v1/browser/helper/logins/fill-request", json={"id": "vault_aaaaaaaaaaaa"}, headers=ok
        )
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    fill.assert_awaited_once_with("org_h", "vault_aaaaaaaaaaaa")


@pytest.mark.asyncio
async def test_helper_save_needs_a_persons_fresh_click_and_no_agent(async_client):
    """The helper's token can be read by a page or an agent inside the browser, and a save
    replaces the password of an existing origin + username: the same two checks as Fill."""
    import time

    from core.services.browser_extensions import helper_token

    ok = {"Authorization": f"Bearer {helper_token('org_h', 'n')}"}
    body = {"origin": "https://a.example", "username": "me", "password": "pw"}
    agent = {
        "mode": "agent",
        "agent_id": "friday",
        "lease_until": int(time.time()) + 100,
        "session_id": "s1",
        "helper_nonce": "n",
    }
    idle = {"mode": "idle", "session_id": "s1", "helper_nonce": "n"}
    for state, touched, status in ((agent, time.time(), 409), (idle, time.time() - 60, 403), (idle, None, 403)):
        with (
            patch("routers.container.container_repo.get_browser_state", return_value=state),
            patch("routers.container.container_repo.get_browser_input_at", return_value=touched),
            patch("core.services.website_logins.add_login") as add,
        ):
            resp = await async_client.post("/api/v1/browser/helper/logins", json=body, headers=ok)
        assert resp.status_code == status
        add.assert_not_called()

    with (
        patch("routers.container.container_repo.get_browser_state", return_value=idle),
        patch("routers.container.container_repo.get_browser_input_at", return_value=time.time() - 1),
        patch("core.services.website_logins.add_login", return_value={"id": "vault_aaaaaaaaaaaa"}) as add,
    ):
        resp = await async_client.post("/api/v1/browser/helper/logins", json=body, headers=ok)
    assert resp.status_code == 201 and resp.json() == {"id": "vault_aaaaaaaaaaaa"}
    add.assert_awaited_once_with(
        "org_h", "https://a.example", "me", "pw", added_by="browser", added_by_name="Shared browser"
    )


@pytest.mark.asyncio
async def test_agent_extension_request_is_hook_token_gated_and_only_asks(async_client):
    with (
        patch("routers.messaging._require_hook_token") as hook,
        patch("routers.container.browser_extensions.request_install", return_value={"status": "requested"}) as ask,
    ):
        resp = await async_client.post(
            "/api/v1/browser/extensions/u/org_h/requests", json={"idOrUrl": "abc", "agentId": "friday"}
        )
    assert resp.status_code == 201
    hook.assert_awaited_once()
    ask.assert_awaited_once_with("org_h", "abc", agent_id="friday")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tier",
    [
        "thumb",
        "view",
    ],
)
async def test_mac_frames_heartbeat_routes_to_one_live_run(async_client_org_member, tier):
    from unittest.mock import AsyncMock

    with (
        patch(
            "core.services.connection_service.ConnectionService.desktop_task_address",
            AsyncMock(return_value="http://here"),
        ),
        patch("core.services.task_address.own_address", return_value="http://here"),
        patch("routers.container.mac_live.heartbeat", AsyncMock()) as heartbeat,
        patch("routers.container.browser_control.start") as start,
    ):
        response = await async_client_org_member.post(
            "/api/v1/browser/frames", json={"runId": "r1", "deviceId": "mac_1", "tier": tier}
        )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    heartbeat.assert_awaited_once_with("org_test_456", "user_test_789", "r1", tier, viewer_owner="org_test_456")
    start.assert_not_called()


@pytest.mark.asyncio
async def test_mac_frames_refuses_an_unknown_tier(async_client_org_member):
    response = await async_client_org_member.post(
        "/api/v1/browser/frames", json={"runId": "r1", "deviceId": "mac_1", "tier": "hd"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_shared_room_guest_routes_frames_to_the_mac_owner(async_client_org_member):
    from unittest.mock import AsyncMock

    room = "22222222-2222-4222-8222-222222222222"
    with (
        patch("routers.container.resolve_owner_id", return_value="org_guest"),
        patch(
            "core.services.messaging.require_room_member", AsyncMock(return_value=({"ownerId": "org_test_456"}, {}))
        ) as member,
        patch(
            "core.services.connection_service.ConnectionService.desktop_task_address",
            AsyncMock(return_value="http://peer"),
        ) as address,
        patch("core.services.task_address.own_address", return_value="http://here"),
        patch("core.services.mac_browser._forward_mac", AsyncMock()) as forward,
    ):
        response = await async_client_org_member.post(
            "/api/v1/browser/frames", json={"runId": "r1", "deviceId": "mac_1", "tier": "view", "roomId": room}
        )
    assert response.status_code == 200
    member.assert_awaited_once_with("org_guest", "user_test_789", room)
    address.assert_awaited_once_with("org_test_456", "mac_1")
    assert forward.await_args.args[1]["viewerOwnerId"] == "org_guest"


@pytest.mark.asyncio
async def test_mac_frames_heartbeat_forwards_the_authenticated_viewer(async_client_org_member):
    from unittest.mock import AsyncMock

    with (
        patch(
            "core.services.connection_service.ConnectionService.desktop_task_address",
            AsyncMock(return_value="http://peer"),
        ),
        patch("core.services.task_address.own_address", return_value="http://here"),
        patch("core.services.mac_browser._forward_mac", AsyncMock()) as forward,
    ):
        response = await async_client_org_member.post(
            "/api/v1/browser/frames", json={"runId": "r1", "deviceId": "mac_1", "tier": "thumb"}
        )
    assert response.status_code == 200
    forward.assert_awaited_once_with(
        "http://peer",
        {"ownerId": "org_test_456", "userId": "user_test_789", "runId": "r1", "tier": "thumb"},
        "/api/v1/desktop-execution/internal/mac-frames",
        timeout=5,
    )
