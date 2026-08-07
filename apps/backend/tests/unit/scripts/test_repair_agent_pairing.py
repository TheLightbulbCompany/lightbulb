"""Unit tests for the repair-agent-pairing incident script.

``ensure_operator_device_paired`` is best-effort: every one of its sub-steps
swallows its own exceptions and the function never raises or returns a
status. So what matters here is NOT that the script calls it -- it's that the
script's own independent verification (fresh Paperclip re-fetch +
``device.pair.list``) correctly tells OK from ERR afterwards, that target
auto-selection finds exactly the drifted agents, and that dry-run never
mutates anything.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scripts.repair_agent_pairing as mod
from core.crypto.operator_device import generate_operator_device, private_key_pem
from core.services.paperclip_adapter_config import claimed_api_key_path

GW_TOKEN = "gw-tok"
OWNER = "org_x"


def _agent(
    agent_id, *, pem, auth_token="stale-jwt", agent_id_field=None, claimed_path=None, adapter_type="openclaw_gateway"
):
    cfg = {"devicePrivateKeyPem": pem, "authToken": auth_token}
    if agent_id_field is not None:
        cfg["agentId"] = agent_id_field
    if claimed_path is not None:
        cfg["claimedApiKeyPath"] = claimed_path
    return {"id": agent_id, "name": f"agent-{agent_id}", "adapterType": adapter_type, "adapterConfig": cfg}


def _healthy_agent(agent_id, pem):
    """An agent whose adapterConfig is already fully bound to GW_TOKEN."""
    return _agent(
        agent_id,
        pem=pem,
        auth_token=GW_TOKEN,
        agent_id_field=agent_id,
        claimed_path=claimed_api_key_path(agent_id),
    )


def _patches(
    *,
    container,
    agents_by_fetch,
    trusted_by_call,
    ensure_paired_mock=None,
    ip="10.0.0.5",
):
    """Patch every collaborator ``main`` lazy-imports.

    ``agents_by_fetch``: list of dicts (id -> agent) returned by successive
    calls to ``fetch_paperclip_agents_for_owner`` (one call per script-level
    ``_fetch_agents()`` invocation -- once up front, once more per repaired
    agent under --apply).

    ``trusted_by_call``: list of sets returned by successive
    ``device.pair.list`` RPCs (once up front, once more per repaired agent).

    ``ip``: what Cloud Map "discovers" for this owner's task. ``None``
    simulates no reachable running task.
    """
    fetch_mock = AsyncMock(side_effect=[list(a.values()) for a in agents_by_fetch])
    rpc_mock = AsyncMock(side_effect=[{"paired": [{"deviceId": d} for d in s]} for s in trusted_by_call])
    ecs_manager = MagicMock()
    ecs_manager.resolve_running_container = AsyncMock(return_value=(container, ip))

    cms = [
        patch("core.repositories.container_repo.get_by_owner_id", AsyncMock(return_value=container)),
        patch("core.containers.get_ecs_manager", MagicMock(return_value=ecs_manager)),
        patch("core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner", fetch_mock),
        patch("core.gateway.operator_rpc.gateway_rpc_oneshot", rpc_mock),
        patch(
            "core.services.paperclip_openclaw_pairing.ensure_operator_device_paired",
            ensure_paired_mock if ensure_paired_mock is not None else AsyncMock(),
        ),
    ]
    return cms, fetch_mock, rpc_mock


def _start(cms):
    for cm in cms:
        cm.start()


def _stop(cms):
    for cm in cms:
        cm.stop()


# --------------------------------------------------------------------------- #
# Preflight: no container / no gateway_token / no reachable task
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_container_row_aborts():
    cms, _, _ = _patches(container=None, agents_by_fetch=[{}], trusted_by_call=[set()])
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=[], apply=False)
    finally:
        _stop(cms)
    assert rc == 1


@pytest.mark.asyncio
async def test_missing_gateway_token_aborts():
    cms, _, _ = _patches(container={"service_name": "svc"}, agents_by_fetch=[{}], trusted_by_call=[set()])
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=[], apply=False)
    finally:
        _stop(cms)
    assert rc == 1


@pytest.mark.asyncio
async def test_no_running_task_aborts():
    cms, _, _ = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{}],
        trusted_by_call=[set()],
        ip=None,  # Cloud Map found no running task
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=[], apply=False)
    finally:
        _stop(cms)
    assert rc == 1


# --------------------------------------------------------------------------- #
# Target auto-selection
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_owner_with_no_paperclip_agents_at_all_aborts():
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{}],
        trusted_by_call=[],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=[], apply=False)
    finally:
        _stop(cms)
    assert rc == 1
    rpc_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_select_skips_healthy_agent_and_picks_broken_one():
    """A healthy agent (adapterConfig fully current) is not selected; a
    broken one (missing agentId/claimedApiKeyPath, wrong authToken) is."""
    ident_ok = generate_operator_device()
    ident_broken = generate_operator_device()
    healthy = _healthy_agent("ok1", private_key_pem(ident_ok))
    broken = _agent("broken1", pem=private_key_pem(ident_broken), auth_token="stale-service-jwt")
    by_id = {"ok1": healthy, "broken1": broken}

    trusted = {ident_ok.device_id}  # only the healthy one is trusted
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[by_id],
        trusted_by_call=[trusted],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=[], apply=False)
    finally:
        _stop(cms)

    # Dry run with a target found -> rc 0, no error.
    assert rc == 0
    # Only one fetch (up front) and one device.pair.list call (up front) in
    # dry-run -- no per-agent re-verification happens without --apply.
    fetch_mock.assert_awaited_once()
    rpc_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_agent_id_bypasses_auto_selection():
    """An explicit --agent-id is processed even though its adapterConfig is
    already current (e.g. the device-trust-only failure mode) -- auto-select
    would have skipped it, but an explicit id must not be silently dropped."""
    ident = generate_operator_device()
    healthy = _healthy_agent("thread1", private_key_pem(ident))
    by_id = {"thread1": healthy}

    ensure_mock = AsyncMock()
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        # up-front fetch, then one more re-fetch after the repair attempt
        agents_by_fetch=[by_id, by_id],
        # up-front trust check (not yet trusted), then after-repair check (now trusted)
        trusted_by_call=[set(), {ident.device_id}],
        ensure_paired_mock=ensure_mock,
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["thread1"], apply=True)
    finally:
        _stop(cms)

    assert rc == 0
    ensure_mock.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Dry run makes no writes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dry_run_never_calls_ensure_paired():
    ident = generate_operator_device()
    broken = _agent("broken1", pem=private_key_pem(ident))
    by_id = {"broken1": broken}

    ensure_mock = AsyncMock()
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[by_id],
        trusted_by_call=[set()],
        ensure_paired_mock=ensure_mock,
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["broken1"], apply=False)
    finally:
        _stop(cms)

    assert rc == 0
    ensure_mock.assert_not_awaited()
    # Only the up-front fetch/list -- no post-repair re-verification round trip.
    fetch_mock.assert_awaited_once()
    rpc_mock.assert_awaited_once()


# --------------------------------------------------------------------------- #
# Verification: OK vs ERR after --apply
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_apply_reports_ok_when_verification_confirms_repair():
    """ensure_operator_device_paired is mocked as a no-op (it never tells us
    anything useful) -- the fresh re-fetch + device.pair.list after it is
    what must report OK, because both now show the repaired state."""
    ident = generate_operator_device()
    broken = _agent("a1", pem=private_key_pem(ident), auth_token="stale")
    fixed = _healthy_agent("a1", private_key_pem(ident))

    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{"a1": broken}, {"a1": fixed}],
        trusted_by_call=[set(), {ident.device_id}],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["a1"], apply=True)
    finally:
        _stop(cms)

    assert rc == 0
    assert fetch_mock.await_count == 2
    assert rpc_mock.await_count == 2


@pytest.mark.asyncio
async def test_apply_reports_err_when_adapter_config_still_wrong_after_repair():
    """The load-bearing case: ensure_operator_device_paired ran (its internal
    warning was swallowed) but the PATCH never actually landed -- the fresh
    re-fetch still shows drift, so the script must report ERR, not OK."""
    ident = generate_operator_device()
    broken = _agent("a1", pem=private_key_pem(ident), auth_token="stale")
    # "after" fetch is IDENTICAL to "before" -- the repair silently no-opped.
    still_broken = _agent("a1", pem=private_key_pem(ident), auth_token="stale")

    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{"a1": broken}, {"a1": still_broken}],
        # device gets trusted, but adapterConfig drift remains -> still ERR.
        trusted_by_call=[set(), {ident.device_id}],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["a1"], apply=True)
    finally:
        _stop(cms)

    assert rc == 1


@pytest.mark.asyncio
async def test_apply_reports_err_when_device_still_not_trusted_after_repair():
    """adapterConfig now matches, but the device never landed in the trust
    list (e.g. the live approval attempt failed) -- must still be ERR."""
    ident = generate_operator_device()
    broken = _agent("a1", pem=private_key_pem(ident), auth_token="stale")
    adapter_fixed_only = _healthy_agent("a1", private_key_pem(ident))

    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{"a1": broken}, {"a1": adapter_fixed_only}],
        # Device never shows up as trusted, either before or after.
        trusted_by_call=[set(), set()],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["a1"], apply=True)
    finally:
        _stop(cms)

    assert rc == 1


# --------------------------------------------------------------------------- #
# Skips: no PEM, wrong adapter type, unknown id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_targets_found_is_a_clean_noop():
    """Auto-select with every agent already healthy: rc 0, no repair calls,
    and the RPC still ran once (device.pair.list) up front only if there ARE
    targets -- with zero targets, main must return before even that call."""
    ident = generate_operator_device()
    healthy = _healthy_agent("ok1", private_key_pem(ident))
    ensure_mock = AsyncMock()
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{"ok1": healthy}],
        trusted_by_call=[],
        ensure_paired_mock=ensure_mock,
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=[], apply=True)
    finally:
        _stop(cms)

    assert rc == 0
    ensure_mock.assert_not_awaited()
    rpc_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_non_gateway_adapter_type():
    agent = {"id": "slack1", "name": "x", "adapterType": "slack", "adapterConfig": {}}
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{"slack1": agent}],
        trusted_by_call=[set()],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["slack1"], apply=True)
    finally:
        _stop(cms)
    assert rc == 0  # skip, not error


@pytest.mark.asyncio
async def test_skips_agent_without_device_pem():
    agent = {"id": "no-pem", "name": "x", "adapterType": "openclaw_gateway", "adapterConfig": {}}
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{"no-pem": agent}],
        trusted_by_call=[set()],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["no-pem"], apply=True)
    finally:
        _stop(cms)
    assert rc == 0  # skip, not error


@pytest.mark.asyncio
async def test_unknown_agent_id_is_an_error():
    """--agent-id for a uuid absent from this owner's Paperclip company (not
    merely a missing one overall) must be reported ERR, not silently
    swallowed by the empty-company early-return."""
    ident = generate_operator_device()
    other = _healthy_agent("other1", private_key_pem(ident))
    cms, fetch_mock, rpc_mock = _patches(
        container={"service_name": "svc", "gateway_token": GW_TOKEN},
        agents_by_fetch=[{"other1": other}],
        trusted_by_call=[{ident.device_id}],
    )
    _start(cms)
    try:
        rc = await mod.main(env="prod", owner_id=OWNER, agent_ids=["ghost"], apply=True)
    finally:
        _stop(cms)
    assert rc == 1


# --------------------------------------------------------------------------- #
# _split_ids
# --------------------------------------------------------------------------- #


def test_split_ids_flattens_repeat_and_comma_dedupes_preserves_order():
    assert mod._split_ids(["a,b", "b,c", " ", "d"]) == ["a", "b", "c", "d"]


def test_split_ids_empty():
    assert mod._split_ids(None) == []
    assert mod._split_ids([]) == []


# --------------------------------------------------------------------------- #
# _needs_repair
# --------------------------------------------------------------------------- #


def test_needs_repair_reports_all_mismatched_keys():
    desired = {"authToken": "GW", "agentId": "a1", "claimedApiKeyPath": "path"}
    cfg = {"authToken": "wrong", "agentId": "a1"}
    assert mod._needs_repair(cfg, desired=desired) == ["authToken", "claimedApiKeyPath"]


def test_needs_repair_empty_when_current():
    desired = {"authToken": "GW", "agentId": "a1"}
    cfg = {"authToken": "GW", "agentId": "a1", "extra": "ignored"}
    assert mod._needs_repair(cfg, desired=desired) == []
