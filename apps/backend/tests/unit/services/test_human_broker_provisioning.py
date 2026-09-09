import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.gateway.native_rooms import HumanIdentity
from core.services import human_broker_provisioning as provisioner


class _Repo:
    def __init__(self, events):
        self.events = events
        self.devices = {}
        self.access = {}

    async def get_by_owner_id(self, owner_id, *, consistent=False):
        return {
            "owner_id": owner_id,
            "gateway_token": "gateway-secret",
            "human_broker_devices": copy.deepcopy(self.devices),
            "human_broker_access": copy.deepcopy(self.access),
        }

    async def create_human_broker_device(
        self,
        owner_id,
        alias,
        record,
        *,
        access_event_id=None,
        subject_tombstone_key=None,
        membership_lifecycle_key=None,
        membership_claim_id=None,
    ):
        self.devices.setdefault(alias, copy.deepcopy(record))
        return copy.deepcopy(self.devices[alias])

    async def cas_human_broker_device(
        self,
        owner_id,
        alias,
        record,
        *,
        generation,
        state,
        access_event_id=None,
        access_state=None,
        subject_tombstone_key=None,
        membership_lifecycle_key=None,
        membership_claim_id=None,
    ):
        current = self.devices.get(alias)
        if not current or current["generation"] != generation or current["state"] != state:
            return False
        if access_state is not None:
            access = self.access.get(alias)
            if not access or access.get("state") != access_state or access.get("event_id") != access_event_id:
                return False
        elif subject_tombstone_key is not None:
            access = self.access.get(alias)
            if access_event_id is None:
                if access is not None:
                    return False
            elif not access or access.get("state") != "allowed" or access.get("event_id") != access_event_id:
                return False
        self.devices[alias] = copy.deepcopy(record)
        if record["state"] == "disabled":
            self.events.append("durable-disabled")
        return True

    async def set_human_broker_access(self, owner_id, alias, marker):
        current = self.access.get(alias)
        if current is None or current["sequence"] <= marker["sequence"]:
            self.access[alias] = copy.deepcopy(marker)
        return copy.deepcopy(self.access[alias])

    async def scan_all(self, *, consistent=False):
        return [await self.get_by_owner_id("org_founder", consistent=consistent)]


class _Kms:
    def __init__(self):
        self.values = {}
        self.encrypts = 0
        self.fail_encrypt = False
        self.fail_decrypt = False

    def encrypt_bytes(self, value, *, encryption_context):
        if self.fail_encrypt:
            raise RuntimeError("kms unavailable")
        self.encrypts += 1
        ciphertext = f"ciphertext-{self.encrypts}"
        self.values[(ciphertext, tuple(sorted(encryption_context.items())))] = value
        return ciphertext

    def decrypt_bytes(self, value, *, encryption_context):
        if self.fail_decrypt:
            raise RuntimeError("kms unavailable")
        return self.values[(value, tuple(sorted(encryption_context.items())))]


class _Adapter:
    def __init__(self, paired):
        self.paired = paired

    async def mutate_paired_json(self, mutator):
        mutator(self.paired)


class _Ecs:
    async def resolve_running_container(self, owner_id):
        return ({"owner_id": owner_id, "gateway_token": "gateway-secret"}, "10.0.0.8")


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace()
    events = []
    repo = _Repo(events)
    kms = _Kms()
    paired = {
        "node-device": {"deviceId": "node-device", "role": "node"},
        "backend-device": {"deviceId": "backend-device", "role": "operator"},
        "paperclip-device": {"deviceId": "paperclip-device", "displayName": "paperclip:Friday"},
    }
    profiles = {}
    deleted_subjects = set()

    def profile(alias, profile_id):
        return {
            "id": profile_id,
            "displayName": None,
            "avatarMime": None,
            "mergedInto": None,
            "createdAt": 1,
            "updatedAt": 1,
            "emails": [alias],
            "githubIdentity": None,
            "hasAvatar": False,
        }

    async def rpc(ip, *, method, params, **kwargs):
        if method == "users.ensureProfile":
            profile_id = profiles.setdefault(params["alias"], f"profile_{len(profiles) + 1}")
            return {"profile": profile(params["alias"], profile_id)}
        if method == "users.self":
            profile_id = kwargs["trusted_broker_profile_id"]
            alias = next(alias for alias, candidate in profiles.items() if candidate == profile_id)
            events.append("v4-profile-proved")
            return {"profile": profile(alias, profile_id)}
        if method == "device.pair.remove":
            events.append("live-pair-removed")
            return {}
        raise AssertionError(method)

    async def set_config(owner_id, path, value):
        assert path == ["gateway", "auth", "trustedBrokerProfiles"]
        state.trusted = copy.deepcopy(value)
        events.append("config-written")

    async def close_connections(owner_id, user_id):
        events.append("connections-closed")

    monkeypatch.setattr(provisioner, "container_repo", repo)
    monkeypatch.setattr(provisioner, "kms_secrets", kms)
    monkeypatch.setattr(provisioner, "get_workspace", lambda: object())
    monkeypatch.setattr(provisioner, "EfsWorkspaceAdapter", lambda **kwargs: _Adapter(paired))
    monkeypatch.setattr(provisioner, "get_ecs_manager", lambda: _Ecs())
    monkeypatch.setattr(provisioner, "gateway_rpc_oneshot", rpc)
    monkeypatch.setattr(provisioner, "set_openclaw_config_path", set_config)
    monkeypatch.setattr(provisioner, "_probe_pairing_request", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(provisioner, "require_current_member", AsyncMock())
    monkeypatch.setattr(provisioner, "revoke_member_connections", close_connections)
    monkeypatch.setattr(
        provisioner.webhook_dedup,
        "has_event",
        AsyncMock(side_effect=lambda event_id: event_id in deleted_subjects),
    )
    monkeypatch.setattr(
        provisioner.webhook_dedup,
        "record_permanent_marker",
        AsyncMock(side_effect=lambda event_id, **kwargs: deleted_subjects.add(event_id)),
    )
    async def get_membership_lifecycle(owner_id, alias):
        return copy.deepcopy(
            repo.access.get(alias)
            or {
                "event_id": f"membership-created:{alias}",
                "sequence": f"00000000000000000001:0:{alias}",
                "state": "allowed",
            }
        )

    monkeypatch.setattr(
        provisioner.webhook_dedup,
        "get_membership_lifecycle",
        AsyncMock(side_effect=get_membership_lifecycle),
    )
    monkeypatch.setattr(
        provisioner, "invalidate_qualification", lambda owner_id: events.append("qualification-disabled")
    )

    @asynccontextmanager
    async def unlocked_human_lifecycle(_owner_id):
        yield {}

    monkeypatch.setattr(provisioner, "owner_lifecycle_guard", unlocked_human_lifecycle)
    monkeypatch.setattr(provisioner.settings, "CLERK_ISSUER", "https://issuer.example")
    state.repo = repo
    state.kms = kms
    state.paired = paired
    state.profiles = profiles
    state.deleted_subjects = deleted_subjects
    state.events = events
    state.trusted = {}
    state.conn_svc = object()
    return state


def _human(subject, connection_id=None):
    return HumanIdentity(
        owner_id="org_founder",
        issuer="https://issuer.example",
        subject=subject,
        connection_id=connection_id or f"connection-{subject}",
        source_ip="203.0.113.8",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"emails": "not-an-array"},
        {"unexpected": True},
    ],
)
def test_profile_contract_rejects_non_array_emails_and_extra_profile_fields(mutation):
    human = _human("user_alice")
    profile = {
        "id": "profile_1",
        "displayName": None,
        "avatarMime": None,
        "mergedInto": None,
        "createdAt": 1,
        "updatedAt": 1,
        "emails": [human.alias],
        "githubIdentity": None,
        "hasAvatar": False,
        **mutation,
    }
    with pytest.raises(provisioner.HumanBrokerProvisioningError):
        provisioner._validated_profile_result({"profile": profile}, human.alias)


@pytest.mark.asyncio
async def test_local_access_marker_cannot_replace_central_clerk_lifecycle(harness, monkeypatch):
    human = _human("user_alice")
    harness.repo.access[human.alias] = {
        "event_id": "local-only-membership",
        "sequence": "00000000000000000001:0:local-only-membership",
        "state": "allowed",
    }
    monkeypatch.setattr(
        provisioner.webhook_dedup,
        "get_membership_lifecycle",
        AsyncMock(return_value=None),
    )

    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="membership epoch unavailable"):
        await provisioner.ensure_human_device(human, harness.conn_svc)

    assert human.alias not in harness.repo.devices


@pytest.mark.asyncio
async def test_ensure_serializes_the_full_owner_effect_sequence(harness, monkeypatch):
    lock = asyncio.Lock()
    first_runtime_started = asyncio.Event()
    release_first_runtime = asyncio.Event()
    second_lock_attempted = asyncio.Event()
    attempts = 0
    active_guards = 0
    max_active_guards = 0
    runtime_calls = 0

    @asynccontextmanager
    async def tracked_guard(owner_id):
        nonlocal attempts, active_guards, max_active_guards
        assert owner_id == "org_founder"
        attempts += 1
        if attempts == 2:
            second_lock_attempted.set()
        async with lock:
            active_guards += 1
            max_active_guards = max(max_active_guards, active_guards)
            try:
                yield
            finally:
                active_guards -= 1

    async def paused_runtime(owner_id):
        nonlocal runtime_calls
        assert active_guards == 1
        runtime_calls += 1
        if runtime_calls == 1:
            first_runtime_started.set()
            await release_first_runtime.wait()
        return {"owner_id": owner_id, "gateway_token": "gateway-secret"}, "10.0.0.8", "gateway-secret"

    monkeypatch.setattr(provisioner, "owner_lifecycle_guard", tracked_guard)
    monkeypatch.setattr(provisioner, "_runtime", paused_runtime)

    first = asyncio.create_task(provisioner.ensure_human_device(_human("user_alice"), harness.conn_svc))
    await first_runtime_started.wait()
    second = asyncio.create_task(provisioner.ensure_human_device(_human("user_bob"), harness.conn_svc))
    await asyncio.wait_for(second_lock_attempted.wait(), timeout=1)

    assert runtime_calls == 1
    assert max_active_guards == 1

    release_first_runtime.set()
    await asyncio.gather(first, second)
    assert runtime_calls == 2
    assert max_active_guards == 1


@pytest.mark.asyncio
async def test_clerk_revocation_effects_run_inside_owner_lifecycle_guard(harness, monkeypatch):
    human = _human("user_alice")
    held = False
    events = []

    @asynccontextmanager
    async def tracked_guard(owner_id):
        nonlocal held
        assert owner_id == human.owner_id
        held = True
        events.append("guard-entered")
        try:
            yield
        finally:
            events.append("guard-exited")
            held = False

    async def revoke_effect(identity, **kwargs):
        assert held is True
        assert identity == HumanIdentity(human.owner_id, human.issuer, human.subject, "", "")
        events.append("revoked")
        return None

    monkeypatch.setattr(provisioner, "owner_lifecycle_guard", tracked_guard)
    monkeypatch.setattr(provisioner, "_revoke_human_device", revoke_effect)

    outcome = await provisioner.revoke_human_subject(human.owner_id, human.issuer, human.subject)

    assert outcome is provisioner.HumanRevocationOutcome.APPLIED
    assert events == ["guard-entered", "revoked", "guard-exited"]


@pytest.mark.asyncio
async def test_two_verified_humans_get_distinct_durable_profiles_and_authorship(harness):
    alice, bob = _human("user_alice"), _human("user_bob")

    alice_receipt = await provisioner.ensure_human_device(alice, harness.conn_svc)
    bob_receipt = await provisioner.ensure_human_device(bob, harness.conn_svc)
    qualified = await provisioner.load_qualified_humans("org_founder")

    assert alice.alias != bob.alias
    assert alice_receipt.device_id != bob_receipt.device_id
    assert qualified[alice.alias].profile_id != qualified[bob.alias].profile_id
    assert harness.trusted == {
        qualified[alice.alias].identity.device_id: qualified[alice.alias].profile_id,
        qualified[bob.alias].identity.device_id: qualified[bob.alias].profile_id,
    }


@pytest.mark.asyncio
async def test_ensure_is_idempotent_and_seed_survives_a_restart_load(harness):
    human = _human("user_alice")

    first = await provisioner.ensure_human_device(human, harness.conn_svc)
    harness.paired.pop(first.device_id)
    harness.trusted = {}
    second = await provisioner.ensure_human_device(human, harness.conn_svc)
    reloaded = (await provisioner.load_qualified_humans(human.owner_id))[human.alias]

    assert second == first
    assert reloaded.identity.device_id == first.device_id
    assert harness.kms.encrypts == 1
    assert len(harness.repo.devices) == 1
    assert first.device_id in harness.paired
    assert harness.trusted
    assert harness.events.count("v4-profile-proved") == 2


@pytest.mark.asyncio
async def test_profile_contract_requires_exact_shape_and_requested_alias(harness, monkeypatch):
    async def wrong_emails(*args, method, params, **kwargs):
        assert method == "users.ensureProfile"
        return {
            "profile": {
                "id": "profile_1",
                "displayName": None,
                "avatarMime": None,
                "mergedInto": None,
                "createdAt": 1,
                "updatedAt": 1,
                "emails": ["somebody-else@identity.lightbulb.invalid"],
                "githubIdentity": None,
                "hasAvatar": False,
            }
        }

    monkeypatch.setattr(provisioner, "gateway_rpc_oneshot", wrong_emails)
    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="profile unavailable"):
        await provisioner.ensure_human_device(_human("user_alice"), harness.conn_svc)

    assert harness.repo.devices[_human("user_alice").alias]["state"] == "provisioning"


@pytest.mark.asyncio
async def test_provisioning_retry_reconciles_pair_config_and_v4_before_activation(harness):
    human = _human("user_alice")
    device = provisioner.generate_operator_device()
    encrypted = harness.kms.encrypt_bytes(
        device.private_key_seed,
        encryption_context=provisioner._kms_context(human.owner_id, human.alias, 1),
    )
    harness.repo.devices[human.alias] = {
        "generation": 1,
        "state": "provisioning",
        "seed_enc": encrypted,
        "device_id": device.device_id,
        "public_key": device.public_key_b64,
        "profile_id": "profile_1",
        "created_at": "now",
        "updated_at": "now",
    }
    harness.profiles[human.alias] = "profile_1"

    receipt = await provisioner.ensure_human_device(human, harness.conn_svc)

    assert receipt.state == "active"
    assert device.device_id in harness.paired
    assert harness.trusted == {device.device_id: "profile_1"}
    assert "v4-profile-proved" in harness.events


@pytest.mark.asyncio
async def test_full_config_rewrite_reconcile_restores_active_and_provisioning_bindings(harness):
    alice = _human("user_alice")
    await provisioner.ensure_human_device(alice, harness.conn_svc)
    bob = _human("user_bob")
    device = provisioner.generate_operator_device()
    encrypted = harness.kms.encrypt_bytes(
        device.private_key_seed,
        encryption_context=provisioner._kms_context(bob.owner_id, bob.alias, 1),
    )
    harness.repo.devices[bob.alias] = {
        "generation": 1,
        "state": "provisioning",
        "seed_enc": encrypted,
        "device_id": device.device_id,
        "public_key": device.public_key_b64,
        "profile_id": "profile_bob",
        "created_at": "now",
        "updated_at": "now",
    }
    harness.trusted = {}  # a stale full-file snapshot just clobbered the map

    await provisioner.reconcile_trusted_broker_profiles(alice.owner_id)

    assert set(harness.trusted.values()) == {"profile_1", "profile_bob"}


@pytest.mark.asyncio
async def test_full_pairing_rewrite_reconcile_removes_stale_human_but_preserves_unrelated(harness):
    human = _human("user_alice")
    receipt = await provisioner.ensure_human_device(human, harness.conn_svc)
    harness.repo.devices[human.alias] = {
        **harness.repo.devices[human.alias],
        "state": "disabled",
    }

    await provisioner.reconcile_provisioned_human_pairs(human.owner_id, "gateway-secret")

    assert receipt.device_id not in harness.paired
    assert {"node-device", "backend-device", "paperclip-device"} <= set(harness.paired)


@pytest.mark.asyncio
async def test_rotation_disables_old_device_before_activating_a_new_generation(harness):
    human = _human("user_alice")
    old = await provisioner.ensure_human_device(human, harness.conn_svc)

    new = await provisioner.rotate_human_device(human, harness.conn_svc)

    assert new.generation == old.generation + 1
    assert new.device_id != old.device_id
    assert old.device_id not in harness.paired
    assert new.device_id in harness.paired
    assert old.device_id not in harness.trusted
    assert harness.repo.devices[human.alias]["state"] == "active"


@pytest.mark.asyncio
async def test_stale_manual_revoke_waiting_on_rotation_preserves_successor_generation(harness, monkeypatch):
    human = _human("user_alice")
    old = await provisioner.ensure_human_device(human, harness.conn_svc)
    lifecycle_lock = asyncio.Lock()
    rotation_entered = asyncio.Event()
    stale_revoke_waiting = asyncio.Event()
    continue_rotation = asyncio.Event()
    lifecycle_attempts = 0
    disable_calls = 0
    real_disable = provisioner._disable_record

    @asynccontextmanager
    async def serialized_lifecycle(_owner_id):
        nonlocal lifecycle_attempts
        lifecycle_attempts += 1
        if lifecycle_attempts == 2:
            stale_revoke_waiting.set()
        async with lifecycle_lock:
            yield {}

    async def pause_rotation_disable(*args, **kwargs):
        nonlocal disable_calls
        disable_calls += 1
        if disable_calls == 1:
            rotation_entered.set()
            await continue_rotation.wait()
        return await real_disable(*args, **kwargs)

    monkeypatch.setattr(provisioner, "owner_lifecycle_guard", serialized_lifecycle)
    monkeypatch.setattr(provisioner, "_disable_record", pause_rotation_disable)

    rotation = asyncio.create_task(provisioner.rotate_human_device(human, harness.conn_svc))
    await rotation_entered.wait()
    stale_revoke = asyncio.create_task(provisioner.revoke_human_device(human, harness.conn_svc))
    await stale_revoke_waiting.wait()
    continue_rotation.set()

    new = await rotation
    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="credential changed"):
        await stale_revoke

    assert new.generation == old.generation + 1
    assert harness.repo.devices[human.alias]["generation"] == new.generation
    assert harness.repo.devices[human.alias]["state"] == "active"
    assert new.device_id in harness.paired
    assert harness.trusted == {new.device_id: "profile_1"}
    assert harness.events.count("connections-closed") == 1


@pytest.mark.asyncio
async def test_revoke_mid_connection_disables_admission_before_live_teardown(harness):
    human = _human("user_alice")
    active = await provisioner.ensure_human_device(human, harness.conn_svc)
    harness.events.clear()

    revoked = await provisioner.revoke_human_device(human, harness.conn_svc)

    assert revoked.state == "disabled"
    assert active.device_id not in harness.paired
    assert harness.trusted == {}
    assert harness.events.index("durable-disabled") < harness.events.index("config-written")
    assert harness.events.index("config-written") < harness.events.index("live-pair-removed")
    assert harness.events.index("live-pair-removed") < harness.events.index("connections-closed")


@pytest.mark.asyncio
async def test_clerk_lifecycle_revoke_does_not_require_deleted_membership(harness):
    human = _human("user_alice")
    await provisioner.ensure_human_device(human, harness.conn_svc)
    provisioner.require_current_member.reset_mock()

    outcome = await provisioner.revoke_human_subject(
        human.owner_id,
        human.issuer,
        human.subject,
        revocation_id="msg_membership_deleted",
        revocation_sequence="00000000000000000001:msg_membership_deleted",
    )

    assert outcome is provisioner.HumanRevocationOutcome.APPLIED
    provisioner.require_current_member.assert_not_awaited()
    assert harness.repo.devices[human.alias]["state"] == "disabled"


@pytest.mark.asyncio
async def test_newer_membership_epoch_supersedes_delete_before_disable(harness, monkeypatch):
    human = _human("user_alice")
    active = await provisioner.ensure_human_device(human, harness.conn_svc)
    harness.events.clear()
    real_cas = harness.repo.cas_human_broker_device

    async def readmit_before_disable(owner_id, alias, record, **kwargs):
        if record["state"] == "disabled" and kwargs.get("access_state") == "revoked":
            harness.repo.access[alias] = {
                "event_id": "msg_membership_created_newer",
                "sequence": "00000000000000000002:0:msg_membership_created_newer",
                "state": "allowed",
            }
        return await real_cas(owner_id, alias, record, **kwargs)

    monkeypatch.setattr(harness.repo, "cas_human_broker_device", readmit_before_disable)
    outcome = await provisioner.revoke_human_subject(
        human.owner_id,
        human.issuer,
        human.subject,
        revocation_id="msg_membership_deleted",
        revocation_sequence="00000000000000000001:1:msg_membership_deleted",
    )

    assert outcome is provisioner.HumanRevocationOutcome.STALE
    assert harness.repo.devices[human.alias]["state"] == "active"
    assert harness.repo.devices[human.alias]["device_id"] == active.device_id
    assert "durable-disabled" not in harness.events
    assert "config-written" not in harness.events
    assert "live-pair-removed" not in harness.events
    assert "connections-closed" not in harness.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("supersede_after", "must_not_happen"),
    [
        ("durable-disabled", "config-written"),
        ("config-written", "live-pair-removed"),
        ("live-pair-removed", "connections-closed"),
    ],
)
async def test_newer_admission_stops_old_delete_at_each_teardown_boundary(
    harness,
    monkeypatch,
    supersede_after,
    must_not_happen,
):
    human = _human("user_alice")
    await provisioner.ensure_human_device(human, harness.conn_svc)
    harness.events.clear()
    deleted = {
        "event_id": "msg_membership_deleted",
        "sequence": "00000000000000000001:1:msg_membership_deleted",
        "state": "revoked",
    }
    admitted = {
        "event_id": "msg_membership_created_newer",
        "sequence": "00000000000000000002:0:msg_membership_created_newer",
        "state": "allowed",
    }
    central = deleted

    async def renew(owner_id, alias, claim_id):
        nonlocal central
        if supersede_after in harness.events:
            central = admitted
            raise provisioner.webhook_dedup.MembershipLifecycleSuperseded("newer create owns lifecycle")

    monkeypatch.setattr(provisioner.webhook_dedup, "renew_membership_lifecycle", AsyncMock(side_effect=renew))
    monkeypatch.setattr(
        provisioner.webhook_dedup,
        "get_membership_lifecycle",
        AsyncMock(side_effect=lambda owner_id, alias: central),
    )

    outcome = await provisioner.revoke_human_subject(
        human.owner_id,
        human.issuer,
        human.subject,
        revocation_id=deleted["event_id"],
        revocation_sequence=deleted["sequence"],
        lifecycle_claim_id="delete-claim",
    )

    assert outcome is provisioner.HumanRevocationOutcome.STALE
    assert must_not_happen not in harness.events


@pytest.mark.asyncio
async def test_membership_tombstone_fences_rotation_even_if_clerk_read_lags(harness):
    human = _human("user_alice")
    await provisioner.ensure_human_device(human, harness.conn_svc)

    await provisioner.revoke_human_subject(
        human.owner_id,
        human.issuer,
        human.subject,
        revocation_id="msg_membership_deleted",
        revocation_sequence="00000000000000000001:msg_membership_deleted",
    )

    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="membership credential revoked"):
        await provisioner.rotate_human_device(human, harness.conn_svc)

    assert harness.repo.devices[human.alias]["state"] == "disabled"


@pytest.mark.asyncio
async def test_container_created_after_membership_delete_honors_central_revoked_epoch(harness, monkeypatch):
    human = _human("user_alice")
    monkeypatch.setattr(
        provisioner.webhook_dedup,
        "get_membership_lifecycle",
        AsyncMock(
            return_value={
                "event_id": "msg_membership_deleted_before_container",
                "sequence": "00000000000000000001:1:msg_membership_deleted_before_container",
                "state": "revoked",
            }
        ),
    )

    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="membership credential revoked"):
        await provisioner.ensure_human_device(human, harness.conn_svc)

    assert human.alias not in harness.repo.devices


@pytest.mark.asyncio
async def test_user_deleted_global_tombstone_denies_omitted_active_scan_row(harness, monkeypatch):
    human = _human("user_alice")
    receipt = await provisioner.ensure_human_device(human, harness.conn_svc)
    monkeypatch.setattr(harness.repo, "scan_all", AsyncMock(return_value=[]))

    await provisioner.revoke_human_subject_everywhere(
        human.issuer,
        human.subject,
    )

    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="subject deleted"):
        await provisioner.require_active_human_device(
            human.owner_id,
            human.alias,
            receipt.device_id,
            harness.repo.devices[human.alias]["profile_id"],
        )
    # The scan deliberately omitted this row; the permanent marker is the deny authority.
    assert harness.repo.devices[human.alias]["state"] == "active"


@pytest.mark.asyncio
async def test_human_pair_merge_preserves_node_backend_and_paperclip_entries(harness):
    await provisioner.ensure_human_device(_human("user_alice"), harness.conn_svc)

    assert harness.paired["node-device"]["role"] == "node"
    assert harness.paired["backend-device"]["role"] == "operator"
    assert harness.paired["paperclip-device"]["displayName"] == "paperclip:Friday"
    assert len(harness.paired) == 4


@pytest.mark.asyncio
async def test_kms_failure_creates_no_credential_or_trust_binding(harness):
    harness.kms.fail_encrypt = True

    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="credential unavailable"):
        await provisioner.ensure_human_device(_human("user_alice"), harness.conn_svc)

    assert harness.repo.devices == {}
    assert len(harness.paired) == 3
    assert harness.trusted == {}


@pytest.mark.asyncio
async def test_kms_failure_loading_an_active_seed_revokes_it_fail_closed(harness):
    human = _human("user_alice")
    await provisioner.ensure_human_device(human, harness.conn_svc)
    harness.kms.fail_decrypt = True

    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="credential unavailable"):
        await provisioner.ensure_human_device(human, harness.conn_svc)

    assert harness.repo.devices[human.alias]["state"] == "disabled"
    assert harness.trusted == {}
    assert (await provisioner.load_qualified_humans(human.owner_id)) == {}


@pytest.mark.asyncio
async def test_uncertain_activation_stays_disabled_and_fail_closed(harness, monkeypatch):
    human = _human("user_alice")
    real_cas = harness.repo.cas_human_broker_device

    async def revoke_before_activation(owner_id, alias, record, *, generation, state, **fence):
        if record["state"] == "active":
            current = harness.repo.devices[alias]
            harness.repo.devices[alias] = {**current, "state": "disabled"}
            return False
        return await real_cas(owner_id, alias, record, generation=generation, state=state, **fence)

    monkeypatch.setattr(harness.repo, "cas_human_broker_device", revoke_before_activation)

    with pytest.raises(provisioner.HumanBrokerProvisioningError, match="changed during activation"):
        await provisioner.ensure_human_device(human, harness.conn_svc)

    assert harness.repo.devices[human.alias]["state"] == "disabled"
    assert (await provisioner.load_qualified_humans(human.owner_id)) == {}


@pytest.mark.asyncio
async def test_uncertain_revoke_write_is_resolved_by_consistent_disabled_state(harness, monkeypatch):
    human = _human("user_alice")
    await provisioner.ensure_human_device(human, harness.conn_svc)
    real_cas = harness.repo.cas_human_broker_device

    async def landed_without_response(owner_id, alias, record, *, generation, state, **fence):
        monkeypatch.setattr(harness.repo, "cas_human_broker_device", real_cas)
        await real_cas(owner_id, alias, record, generation=generation, state=state, **fence)
        raise TimeoutError("response lost")

    monkeypatch.setattr(harness.repo, "cas_human_broker_device", landed_without_response)

    receipt = await provisioner.revoke_human_device(human, harness.conn_svc)

    assert receipt.state == "disabled"
    assert harness.repo.devices[human.alias]["state"] == "disabled"
    assert harness.trusted == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["revoke_human_device", "rotate_human_device"])
async def test_revoke_and_rotate_never_report_success_while_old_record_stays_active(harness, monkeypatch, operation):
    human = _human("user_alice")
    old = await provisioner.ensure_human_device(human, harness.conn_svc)

    async def rejected_cas(owner_id, alias, record, *, generation, state, **fence):
        if record["state"] == "disabled":
            return False
        return await _Repo.cas_human_broker_device(
            harness.repo, owner_id, alias, record, generation=generation, state=state, **fence
        )

    monkeypatch.setattr(harness.repo, "cas_human_broker_device", rejected_cas)
    with pytest.raises(provisioner.HumanBrokerProvisioningError):
        await getattr(provisioner, operation)(human, harness.conn_svc)

    assert harness.repo.devices[human.alias]["state"] == "active"
    assert old.device_id not in harness.trusted


@pytest.mark.asyncio
async def test_receipts_and_errors_never_return_seed_token_or_profile(harness, monkeypatch):
    receipt = await provisioner.ensure_human_device(_human("user_alice"), harness.conn_svc)

    rendered = repr(receipt)
    assert "gateway-secret" not in rendered
    assert "ciphertext" not in rendered
    assert "profile_" not in rendered
    assert not hasattr(receipt, "profile_id")
    assert not hasattr(receipt, "private_key_seed")

    async def leaking_rpc(*args, method, params, **kwargs):
        if method == "users.ensureProfile":
            return {"profile": {"id": "profile_2", "emails": [params["alias"]]}, "deviceToken": "must-not-return"}
        return {}

    monkeypatch.setattr(provisioner, "gateway_rpc_oneshot", leaking_rpc)
    with pytest.raises(provisioner.HumanBrokerProvisioningError) as exc:
        await provisioner.ensure_human_device(_human("user_bob"), harness.conn_svc)
    assert "must-not-return" not in str(exc.value)
