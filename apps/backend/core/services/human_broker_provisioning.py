"""Durable, server-owned OpenClaw identity for each verified organization human."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.crypto import kms_secrets
from core.crypto.operator_device import (
    build_paired_operator_entry,
    generate_operator_device,
    load_operator_device_from_seed,
)
from core.dynamodb import utc_now_iso
from core.gateway.native_rooms import (
    HUMAN_SCOPES,
    HumanIdentity,
    QualifiedHumanDevice,
    invalidate_qualification,
    require_current_member,
    revoke_member_connections,
)
from core.gateway.operator_rpc import gateway_rpc_oneshot
from core.repositories import container_repo
from core.services.config_patcher import set_openclaw_config_path
from core.services.desktop_pair import EfsWorkspaceAdapter
from core.services.paperclip_openclaw_pairing import (
    _approve_pairing_request,
    _find_pending_request_id,
    _probe_pairing_request,
)

_FIELD = "human_broker_devices"
_ALIAS = re.compile(r"clerk-[a-f0-9]{64}@identity\.lightbulb\.invalid\Z")
_DEVICE_ID = re.compile(r"[a-f0-9]{64}\Z")
_PUBLIC_KEY = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_PROFILE_ID = re.compile(r"(?!gateway-owner\Z)[A-Za-z0-9_-]{1,128}\Z")
_ANY_PROFILE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_STATES = frozenset({"provisioning", "active", "disabled"})
_PROFILE_FIELDS = frozenset(
    {
        "id",
        "displayName",
        "avatarMime",
        "mergedInto",
        "createdAt",
        "updatedAt",
        "emails",
        "githubIdentity",
        "hasAvatar",
    }
)


class HumanBrokerProvisioningError(RuntimeError):
    pass


class _CredentialChanged(HumanBrokerProvisioningError):
    pass


@dataclass(frozen=True)
class HumanDeviceReceipt:
    generation: int
    state: str
    device_id: str


def _validate_identity(identity: HumanIdentity) -> None:
    if not (
        identity.owner_id.startswith("org_")
        and identity.issuer == settings.CLERK_ISSUER
        and isinstance(identity.subject, str)
        and identity.subject
        and _ALIAS.fullmatch(identity.alias)
    ):
        raise HumanBrokerProvisioningError("verified organization identity required")


def _kms_context(owner_id: str, alias: str, generation: int) -> dict[str, str]:
    return {
        "owner_id": owner_id,
        "human_alias": alias,
        "generation": str(generation),
        "purpose": "human-broker-device-seed",
    }


def _records(row: dict | None) -> dict[str, dict]:
    records = (row or {}).get(_FIELD, {})
    if not isinstance(records, dict):
        raise HumanBrokerProvisioningError("human credential state unavailable")
    return records


def _validate_record(alias: str, record: Any, *, require_profile: bool = False) -> dict:
    valid = (
        _ALIAS.fullmatch(alias)
        and isinstance(record, dict)
        and isinstance(record.get("generation"), int)
        and not isinstance(record.get("generation"), bool)
        and record["generation"] >= 1
        and record.get("state") in _STATES
        and isinstance(record.get("seed_enc"), str)
        and bool(record["seed_enc"])
        and isinstance(record.get("device_id"), str)
        and _DEVICE_ID.fullmatch(record["device_id"])
        and isinstance(record.get("public_key"), str)
        and _PUBLIC_KEY.fullmatch(record["public_key"])
    )
    profile_id = record.get("profile_id") if isinstance(record, dict) else None
    if require_profile:
        valid = valid and isinstance(profile_id, str) and _PROFILE_ID.fullmatch(profile_id)
    elif profile_id is not None:
        valid = valid and isinstance(profile_id, str) and _PROFILE_ID.fullmatch(profile_id)
    if not valid:
        raise HumanBrokerProvisioningError("human credential state unavailable")
    return record


async def _load_identity(owner_id: str, alias: str, record: dict):
    try:
        seed = await asyncio.to_thread(
            kms_secrets.decrypt_bytes,
            record["seed_enc"],
            encryption_context=_kms_context(owner_id, alias, record["generation"]),
        )
        identity = load_operator_device_from_seed(seed)
    except Exception as exc:
        raise HumanBrokerProvisioningError("human credential unavailable") from exc
    if identity.device_id != record["device_id"] or identity.public_key_b64 != record["public_key"]:
        raise HumanBrokerProvisioningError("human credential integrity check failed")
    return identity


async def _runtime(owner_id: str) -> tuple[dict, str, str]:
    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    token = (container or {}).get("gateway_token")
    if not container or not ip or not isinstance(token, str) or not token:
        raise HumanBrokerProvisioningError("running human broker unavailable")
    return container, ip, token


async def _runtime_for_teardown(owner_id: str) -> tuple[tuple[dict, str, str] | None, Exception | None]:
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    except Exception as exc:
        return None, exc
    token = (container or {}).get("gateway_token")
    if not container or not ip or not isinstance(token, str) or not token:
        return None, None
    return (container, ip, token), None


def _validated_profile_result(payload: Any, alias: str, *, expected_profile_id: str | None = None) -> str:
    profile = payload.get("profile") if isinstance(payload, dict) and set(payload) == {"profile"} else None
    if not isinstance(profile, dict) or not (_PROFILE_FIELDS <= set(profile) <= _PROFILE_FIELDS | {"role"}):
        raise HumanBrokerProvisioningError("canonical human profile unavailable")

    profile_id = profile.get("id")
    emails = profile.get("emails")
    github = profile.get("githubIdentity")
    merged_into = profile.get("mergedInto")
    display_name = profile.get("displayName")
    role = profile.get("role")
    valid = (
        isinstance(profile_id, str)
        and _PROFILE_ID.fullmatch(profile_id)
        and (expected_profile_id is None or profile_id == expected_profile_id)
        and (display_name is None or isinstance(display_name, str) and len(display_name) <= 256)
        and profile.get("avatarMime") in {None, "image/png", "image/jpeg", "image/webp"}
        and (merged_into is None or isinstance(merged_into, str) and _ANY_PROFILE_ID.fullmatch(merged_into))
        and isinstance(profile.get("createdAt"), int)
        and not isinstance(profile.get("createdAt"), bool)
        and profile["createdAt"] >= 0
        and isinstance(profile.get("updatedAt"), int)
        and not isinstance(profile.get("updatedAt"), bool)
        and profile["updatedAt"] >= 0
        and isinstance(emails, list)
        and all(isinstance(email, str) and bool(email) for email in emails)
        and alias in emails
        and isinstance(profile.get("hasAvatar"), bool)
        and (
            github is None
            or isinstance(github, dict)
            and set(github) == {"login", "profileUrl", "avatarUrl"}
            and isinstance(github.get("login"), str)
            and 1 <= len(github["login"]) <= 39
            and all(isinstance(github.get(field), str) and bool(github[field]) for field in ("profileUrl", "avatarUrl"))
        )
        and (
            "role" not in profile
            or isinstance(role, str)
            and 1 <= len(role) <= 128
            and any(not char.isspace() for char in role)
        )
    )
    if not valid:
        raise HumanBrokerProvisioningError("canonical human profile unavailable")
    return profile_id


async def _ensure_profile(owner_id: str, alias: str, ip: str, token: str) -> str:
    try:
        payload = await gateway_rpc_oneshot(
            ip,
            owner_id=owner_id,
            token=token,
            method="users.ensureProfile",
            params={"alias": alias},
        )
        return _validated_profile_result(payload, alias)
    except HumanBrokerProvisioningError:
        raise
    except Exception as exc:
        raise HumanBrokerProvisioningError("canonical human profile unavailable") from exc


async def _prove_v4_profile(
    owner_id: str,
    alias: str,
    profile_id: str,
    ip: str,
    token: str,
    device,
) -> None:
    last_error: Exception | None = None
    # The EFS watcher can briefly restart or reject while it advances to the
    # just-written trustedBrokerProfiles snapshot. Do not activate until the
    # exact v4 assertion is accepted and users.self reads back the same profile.
    for delay in (0.0, 0.25, 0.5, 1.0, 2.0, 4.0):
        if delay:
            await asyncio.sleep(delay)
        try:
            payload = await gateway_rpc_oneshot(
                ip,
                owner_id=owner_id,
                token=token,
                method="users.self",
                params={},
                scopes=HUMAN_SCOPES,
                identity=device,
                trusted_broker_profile_id=profile_id,
            )
            _validated_profile_result(payload, alias, expected_profile_id=profile_id)
            return
        except HumanBrokerProvisioningError:
            raise
        except Exception as exc:
            last_error = exc
    raise HumanBrokerProvisioningError("trusted human profile activation proof unavailable") from last_error


async def _pair_live(owner_id: str, ip: str, token: str, identity) -> None:
    try:
        paired, request_id = await _probe_pairing_request(
            ip=ip,
            owner_id=owner_id,
            token=token,
            identity=identity,
        )
        if paired:
            return
        if not request_id:
            request_id = await _find_pending_request_id(
                ip=ip,
                owner_id=owner_id,
                token=token,
                device_id=identity.device_id,
            )
        if not request_id:
            raise RuntimeError("pending pairing request unavailable")
        await _approve_pairing_request(
            ip=ip,
            owner_id=owner_id,
            token=token,
            device_id=identity.device_id,
            request_id=request_id,
        )
    except Exception as exc:
        raise HumanBrokerProvisioningError("live human device pairing unavailable") from exc


def _paired_entry(alias: str, identity, token: str) -> dict:
    entry = build_paired_operator_entry(
        identity,
        gateway_token=token,
        scopes=HUMAN_SCOPES,
        display_name="lightbulb-human",
    )
    entry["lightbulbHumanAlias"] = alias
    return entry


def _adapter(owner_id: str) -> EfsWorkspaceAdapter:
    return EfsWorkspaceAdapter(
        owner_id=owner_id,
        workspace=get_workspace(),
        container_repo=container_repo,
        gateway_ws_url=settings.WS_GATEWAY_URL,
    )


async def _merge_pair(owner_id: str, alias: str, entry: dict) -> None:
    def mutate(paired: dict) -> bool:
        changed = False
        for device_id, current in list(paired.items()):
            if (
                isinstance(current, dict)
                and current.get("lightbulbHumanAlias") == alias
                and device_id != entry["deviceId"]
            ):
                paired.pop(device_id)
                changed = True
        current = paired.get(entry["deviceId"])
        if current is not None and (
            not isinstance(current, dict)
            or current.get("publicKey") != entry["publicKey"]
            or current.get("lightbulbHumanAlias") != alias
        ):
            raise HumanBrokerProvisioningError("human device id collision")
        wanted = {**current, **entry} if isinstance(current, dict) else entry
        if current != wanted:
            paired[entry["deviceId"]] = wanted
            changed = True
        return changed

    await _adapter(owner_id).mutate_paired_json(mutate)


async def _remove_pair(owner_id: str, alias: str, device_id: str) -> None:
    def mutate(paired: dict) -> bool:
        removed = False
        for candidate, entry in list(paired.items()):
            if candidate == device_id or (isinstance(entry, dict) and entry.get("lightbulbHumanAlias") == alias):
                paired.pop(candidate)
                removed = True
        return removed

    await _adapter(owner_id).mutate_paired_json(mutate)


async def _remove_exact_pair(owner_id: str, device_id: str) -> None:
    def mutate(paired: dict) -> bool:
        if device_id not in paired:
            return False
        paired.pop(device_id)
        return True

    await _adapter(owner_id).mutate_paired_json(mutate)


def _trusted_profiles(records: dict[str, dict], *, include_provisioning: bool) -> dict[str, str]:
    states = {"active", "provisioning"} if include_provisioning else {"active"}
    bindings = {}
    profiles = set()
    for alias, raw in records.items():
        if not isinstance(raw, dict) or raw.get("state") not in states:
            continue
        if raw.get("state") == "provisioning" and not raw.get("profile_id"):
            continue
        record = _validate_record(alias, raw, require_profile=True)
        if record["device_id"] in bindings or record["profile_id"] in profiles:
            raise HumanBrokerProvisioningError("human credential bindings are ambiguous")
        bindings[record["device_id"]] = record["profile_id"]
        profiles.add(record["profile_id"])
    if len(bindings) > 256:
        raise HumanBrokerProvisioningError("human credential binding limit exceeded")
    return bindings


async def active_trusted_broker_profiles(owner_id: str) -> dict[str, str]:
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    return _trusted_profiles(_records(row), include_provisioning=False)


async def provisioned_trusted_broker_profiles(owner_id: str) -> dict[str, str]:
    """Bindings a full config rewrite must retain while activation is pending."""
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    return _trusted_profiles(_records(row), include_provisioning=True)


async def _human_pair_entries(
    owner_id: str,
    gateway_token: str,
    records: dict[str, dict],
) -> list[dict]:
    entries = []
    for alias, raw in records.items():
        if not isinstance(raw, dict) or raw.get("state") not in {"active", "provisioning"}:
            continue
        if raw.get("state") == "provisioning" and not raw.get("profile_id"):
            continue
        record = _validate_record(alias, raw, require_profile=True)
        entries.append(_paired_entry(alias, await _load_identity(owner_id, alias, record), gateway_token))
    return entries


async def provisioned_human_pair_entries(owner_id: str, gateway_token: str) -> list[dict]:
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    return await _human_pair_entries(owner_id, gateway_token, _records(row))


def _pair_state_fingerprint(records: dict[str, dict]) -> tuple:
    return tuple(
        sorted(
            (
                alias,
                raw.get("generation") if isinstance(raw, dict) else None,
                raw.get("state") if isinstance(raw, dict) else None,
                raw.get("seed_enc") if isinstance(raw, dict) else None,
                raw.get("device_id") if isinstance(raw, dict) else None,
                raw.get("public_key") if isinstance(raw, dict) else None,
                raw.get("profile_id") if isinstance(raw, dict) else None,
            )
            for alias, raw in records.items()
        )
    )


async def reconcile_provisioned_human_pairs(owner_id: str, gateway_token: str) -> None:
    """Make tagged human pairing entries match a fresh durable-state read."""
    for _ in range(3):
        records = _records(await container_repo.get_by_owner_id(owner_id, consistent=True))
        fingerprint = _pair_state_fingerprint(records)
        entries = await _human_pair_entries(owner_id, gateway_token, records)
        desired = {entry["lightbulbHumanAlias"]: entry for entry in entries}

        def mutate(paired: dict) -> bool:
            changed = False
            for device_id, current in list(paired.items()):
                if not isinstance(current, dict) or "lightbulbHumanAlias" not in current:
                    continue
                wanted = desired.get(current["lightbulbHumanAlias"])
                if wanted is None or wanted["deviceId"] != device_id:
                    paired.pop(device_id)
                    changed = True
            for entry in entries:
                current = paired.get(entry["deviceId"])
                if current is not None and (
                    not isinstance(current, dict)
                    or current.get("publicKey") != entry["publicKey"]
                    or current.get("lightbulbHumanAlias") != entry["lightbulbHumanAlias"]
                ):
                    raise HumanBrokerProvisioningError("human device id collision")
                wanted = {**current, **entry} if isinstance(current, dict) else entry
                if current != wanted:
                    paired[entry["deviceId"]] = wanted
                    changed = True
            return changed

        await _adapter(owner_id).mutate_paired_json(mutate)
        latest = _records(await container_repo.get_by_owner_id(owner_id, consistent=True))
        if _pair_state_fingerprint(latest) == fingerprint:
            return
    raise HumanBrokerProvisioningError("human pairing state kept changing during reconciliation")


async def load_qualified_humans(owner_id: str) -> dict[str, QualifiedHumanDevice]:
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    humans = {}
    for alias, raw in _records(row).items():
        if not isinstance(raw, dict) or raw.get("state") != "active":
            continue
        record = _validate_record(alias, raw, require_profile=True)
        humans[alias] = QualifiedHumanDevice(
            await _load_identity(owner_id, alias, record),
            record["profile_id"],
        )
    # Exercise the same one-to-one check the room qualification enforces.
    _trusted_profiles(_records(row), include_provisioning=False)
    return humans


async def require_active_human_device(
    owner_id: str,
    alias: str,
    device_id: str,
    profile_id: str,
) -> None:
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    record = _validate_record(alias, _records(row).get(alias), require_profile=True)
    if record["state"] != "active" or record["device_id"] != device_id or record["profile_id"] != profile_id:
        raise HumanBrokerProvisioningError("human credential revoked")


async def _write_trusted_profiles(
    owner_id: str,
    *,
    include_provisioning: bool,
    exclude_alias: str | None = None,
) -> None:
    for _ in range(3):
        records = _records(await container_repo.get_by_owner_id(owner_id, consistent=True))
        if exclude_alias is not None:
            records = {alias: record for alias, record in records.items() if alias != exclude_alias}
        bindings = _trusted_profiles(records, include_provisioning=include_provisioning)
        await set_openclaw_config_path(
            owner_id,
            ["gateway", "auth", "trustedBrokerProfiles"],
            bindings,
        )
        latest = _records(await container_repo.get_by_owner_id(owner_id, consistent=True))
        if exclude_alias is not None:
            latest = {alias: record for alias, record in latest.items() if alias != exclude_alias}
        if _trusted_profiles(latest, include_provisioning=include_provisioning) == bindings:
            return
    raise HumanBrokerProvisioningError("human profile bindings kept changing during reconciliation")


async def reconcile_trusted_broker_profiles(owner_id: str) -> None:
    """Re-read durable state after a competing full-config snapshot/write."""
    await _write_trusted_profiles(owner_id, include_provisioning=True)


async def _cleanup_superseded_reconciliation(
    identity: HumanIdentity,
    record: dict,
    *,
    ip: str,
    token: str,
) -> None:
    """Undo an old ensure that lost a concurrent disable/rotation race."""
    invalidate_qualification(identity.owner_id)
    try:
        current = _records(await container_repo.get_by_owner_id(identity.owner_id, consistent=True)).get(identity.alias)
    except Exception:
        return
    if (
        isinstance(current, dict)
        and current.get("generation") == record["generation"]
        and current.get("state") == record["state"]
        and current.get("device_id") == record["device_id"]
    ):
        return

    for operation in (
        lambda: _write_trusted_profiles(identity.owner_id, include_provisioning=True),
        lambda: _remove_exact_pair(identity.owner_id, record["device_id"]),
        lambda: gateway_rpc_oneshot(
            ip,
            owner_id=identity.owner_id,
            token=token,
            method="device.pair.remove",
            params={"deviceId": record["device_id"]},
        ),
    ):
        try:
            await operation()
        except Exception:
            pass


async def _disable_record(owner_id: str, alias: str, record: dict) -> dict:
    if record["state"] == "disabled":
        return record
    disabled = {**record, "state": "disabled", "updated_at": utc_now_iso()}
    try:
        changed = await container_repo.cas_human_broker_device(
            owner_id,
            alias,
            disabled,
            generation=record["generation"],
            state=record["state"],
        )
    except Exception:
        # A timed-out conditional write may have landed. One consistent read
        # resolves that ambiguity; a still-active value gets one safe retry.
        row = await container_repo.get_by_owner_id(owner_id, consistent=True)
        current = _validate_record(alias, _records(row).get(alias))
        if current["state"] == "disabled":
            return current
        if current["generation"] != record["generation"] or current["state"] != record["state"]:
            raise _CredentialChanged("human credential changed during revocation")
        changed = await container_repo.cas_human_broker_device(
            owner_id,
            alias,
            disabled,
            generation=record["generation"],
            state=record["state"],
        )
    if changed:
        return disabled
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    current = _validate_record(alias, _records(row).get(alias))
    if current["state"] != "disabled":
        raise _CredentialChanged("human credential changed during revocation")
    return current


async def _teardown_disabled(
    identity: HumanIdentity,
    record: dict,
    *,
    runtime: tuple[dict, str, str] | None,
    close_connections: bool,
    runtime_error: Exception | None = None,
) -> None:
    invalidate_qualification(identity.owner_id)
    errors = [runtime_error] if runtime_error is not None else []
    try:
        await _write_trusted_profiles(
            identity.owner_id,
            include_provisioning=True,
            exclude_alias=identity.alias,
        )
    except Exception as exc:
        errors.append(exc)
    try:
        await _remove_pair(identity.owner_id, identity.alias, record["device_id"])
    except Exception as exc:
        errors.append(exc)
    if runtime is not None:
        _, ip, token = runtime
        try:
            await gateway_rpc_oneshot(
                ip,
                owner_id=identity.owner_id,
                token=token,
                method="device.pair.remove",
                params={"deviceId": record["device_id"]},
            )
        except RuntimeError as exc:
            if "unknown deviceId" not in str(exc):
                errors.append(exc)
        except Exception as exc:
            errors.append(exc)
    if close_connections:
        try:
            await revoke_member_connections(identity.owner_id, identity.subject)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise HumanBrokerProvisioningError("human credential teardown incomplete; remains disabled") from errors[0]


async def ensure_human_device(identity: HumanIdentity, conn_svc) -> HumanDeviceReceipt:
    _validate_identity(identity)
    await require_current_member(identity, conn_svc)
    _, ip, token = await _runtime(identity.owner_id)
    row = await container_repo.get_by_owner_id(identity.owner_id, consistent=True)
    record = _records(row).get(identity.alias)
    if record is None:
        device = generate_operator_device()
        try:
            seed_enc = await asyncio.to_thread(
                kms_secrets.encrypt_bytes,
                device.private_key_seed,
                encryption_context=_kms_context(identity.owner_id, identity.alias, 1),
            )
        except Exception as exc:
            raise HumanBrokerProvisioningError("human credential unavailable") from exc
        now = utc_now_iso()
        record = await container_repo.create_human_broker_device(
            identity.owner_id,
            identity.alias,
            {
                "generation": 1,
                "state": "provisioning",
                "seed_enc": seed_enc,
                "device_id": device.device_id,
                "public_key": device.public_key_b64,
                "created_at": now,
                "updated_at": now,
            },
        )
    record = _validate_record(identity.alias, record)
    if record["state"] == "disabled":
        raise HumanBrokerProvisioningError("human credential revoked; rotate before reconnecting")
    try:
        device = await _load_identity(identity.owner_id, identity.alias, record)
    except HumanBrokerProvisioningError:
        if record["state"] == "active":
            disabled = await _disable_record(identity.owner_id, identity.alias, record)
            await _teardown_disabled(identity, disabled, runtime=({}, ip, token), close_connections=True)
        raise
    profile_id = await _ensure_profile(identity.owner_id, identity.alias, ip, token)
    if record.get("profile_id") not in (None, profile_id):
        raise HumanBrokerProvisioningError("canonical human profile changed")
    if record.get("profile_id") is None:
        with_profile = {**record, "profile_id": profile_id, "updated_at": utc_now_iso()}
        if not await container_repo.cas_human_broker_device(
            identity.owner_id,
            identity.alias,
            with_profile,
            generation=record["generation"],
            state="provisioning",
        ):
            current = _validate_record(
                identity.alias,
                _records(await container_repo.get_by_owner_id(identity.owner_id, consistent=True)).get(identity.alias),
            )
            if current["state"] != "provisioning" or current.get("profile_id") != profile_id:
                raise HumanBrokerProvisioningError("human credential changed during profile binding")
            record = current
        else:
            record = with_profile

    try:
        await _merge_pair(identity.owner_id, identity.alias, _paired_entry(identity.alias, device, token))
        await _pair_live(identity.owner_id, ip, token, device)
        await _write_trusted_profiles(identity.owner_id, include_provisioning=True)
        await _prove_v4_profile(identity.owner_id, identity.alias, profile_id, ip, token, device)
        await require_current_member(identity, conn_svc)
        if record["state"] == "active":
            await require_active_human_device(
                identity.owner_id,
                identity.alias,
                record["device_id"],
                profile_id,
            )
    except Exception:
        await _cleanup_superseded_reconciliation(identity, record, ip=ip, token=token)
        raise
    if record["state"] == "active":
        return HumanDeviceReceipt(record["generation"], "active", record["device_id"])
    active = {**record, "state": "active", "updated_at": utc_now_iso()}
    if not await container_repo.cas_human_broker_device(
        identity.owner_id,
        identity.alias,
        active,
        generation=record["generation"],
        state="provisioning",
    ):
        current = _records(await container_repo.get_by_owner_id(identity.owner_id, consistent=True)).get(identity.alias)
        if (
            isinstance(current, dict)
            and current.get("state") == "active"
            and current.get("generation") == record["generation"]
            and current.get("device_id") == record["device_id"]
            and current.get("profile_id") == record["profile_id"]
        ):
            return HumanDeviceReceipt(current["generation"], "active", current["device_id"])
        if isinstance(current, dict) and current.get("state") == "disabled":
            await _teardown_disabled(identity, current, runtime=({}, ip, token), close_connections=True)
        raise HumanBrokerProvisioningError("human credential changed during activation")
    return HumanDeviceReceipt(active["generation"], "active", active["device_id"])


async def _revoke_human_device(
    identity: HumanIdentity,
    *,
    missing_ok: bool,
) -> HumanDeviceReceipt | None:
    invalidate_qualification(identity.owner_id)
    row = await container_repo.get_by_owner_id(identity.owner_id, consistent=True)
    raw = _records(row).get(identity.alias)
    if raw is None:
        await revoke_member_connections(identity.owner_id, identity.subject)
        if missing_ok:
            return None
        raise HumanBrokerProvisioningError("human credential does not exist")
    record = _validate_record(identity.alias, raw)
    runtime, runtime_error = await _runtime_for_teardown(identity.owner_id)
    try:
        for _ in range(2):
            try:
                record = await _disable_record(identity.owner_id, identity.alias, record)
                break
            except _CredentialChanged:
                row = await container_repo.get_by_owner_id(identity.owner_id, consistent=True)
                record = _validate_record(identity.alias, _records(row).get(identity.alias))
        else:
            raise _CredentialChanged("human credential kept changing during revocation")
    except Exception as exc:
        await _teardown_disabled(
            identity,
            record,
            runtime=runtime,
            close_connections=True,
            runtime_error=runtime_error,
        )
        raise HumanBrokerProvisioningError("human credential revocation uncertain; admission closed") from exc
    await _teardown_disabled(
        identity,
        record,
        runtime=runtime,
        close_connections=True,
        runtime_error=runtime_error,
    )
    return HumanDeviceReceipt(record["generation"], "disabled", record["device_id"])


async def revoke_human_device(identity: HumanIdentity, conn_svc) -> HumanDeviceReceipt:
    _validate_identity(identity)
    await require_current_member(identity, conn_svc)
    receipt = await _revoke_human_device(identity, missing_ok=False)
    assert receipt is not None
    return receipt


async def revoke_human_subject(owner_id: str, issuer: str, subject: str) -> HumanDeviceReceipt | None:
    """Server-authenticated Clerk lifecycle revocation; no membership lookup."""
    identity = HumanIdentity(
        owner_id=owner_id,
        issuer=issuer,
        subject=subject,
        connection_id="",
        source_ip="",
    )
    _validate_identity(identity)
    return await _revoke_human_device(identity, missing_ok=True)


async def revoke_human_subject_everywhere(issuer: str, subject: str) -> None:
    """Disable a deleted Clerk subject in every org container that stores it."""
    probe = HumanIdentity("org_probe", issuer, subject, "", "")
    _validate_identity(probe)
    alias = probe.alias
    errors = []
    for row in await container_repo.scan_all():
        owner_id = row.get("owner_id")
        records = row.get(_FIELD)
        if not (
            isinstance(owner_id, str) and owner_id.startswith("org_") and isinstance(records, dict) and alias in records
        ):
            continue
        try:
            await revoke_human_subject(owner_id, issuer, subject)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise HumanBrokerProvisioningError("deleted human credential revocation incomplete") from errors[0]


async def rotate_human_device(identity: HumanIdentity, conn_svc) -> HumanDeviceReceipt:
    _validate_identity(identity)
    await require_current_member(identity, conn_svc)
    row = await container_repo.get_by_owner_id(identity.owner_id, consistent=True)
    raw = _records(row).get(identity.alias)
    if raw is None:
        return await ensure_human_device(identity, conn_svc)
    old = _validate_record(identity.alias, raw)
    runtime, runtime_error = await _runtime_for_teardown(identity.owner_id)
    try:
        try:
            for _ in range(2):
                try:
                    old = await _disable_record(identity.owner_id, identity.alias, old)
                    break
                except _CredentialChanged:
                    row = await container_repo.get_by_owner_id(identity.owner_id, consistent=True)
                    old = _validate_record(identity.alias, _records(row).get(identity.alias))
            else:
                raise _CredentialChanged("human credential kept changing during rotation")
        except Exception as exc:
            await _teardown_disabled(
                identity,
                old,
                runtime=runtime,
                close_connections=False,
                runtime_error=runtime_error,
            )
            raise HumanBrokerProvisioningError("human credential rotation uncertain; admission closed") from exc
        await _teardown_disabled(
            identity,
            old,
            runtime=runtime,
            close_connections=False,
            runtime_error=runtime_error,
        )
        generation = old["generation"] + 1
        device = generate_operator_device()
        try:
            seed_enc = await asyncio.to_thread(
                kms_secrets.encrypt_bytes,
                device.private_key_seed,
                encryption_context=_kms_context(identity.owner_id, identity.alias, generation),
            )
        except Exception as exc:
            raise HumanBrokerProvisioningError("human credential unavailable; old credential remains disabled") from exc
        now = utc_now_iso()
        replacement = {
            "generation": generation,
            "state": "provisioning",
            "seed_enc": seed_enc,
            "device_id": device.device_id,
            "public_key": device.public_key_b64,
            "profile_id": old.get("profile_id"),
            "created_at": now,
            "updated_at": now,
        }
        if not await container_repo.cas_human_broker_device(
            identity.owner_id,
            identity.alias,
            replacement,
            generation=old["generation"],
            state="disabled",
        ):
            current = _validate_record(
                identity.alias,
                _records(await container_repo.get_by_owner_id(identity.owner_id, consistent=True)).get(identity.alias),
            )
            if current["generation"] == generation and current["state"] in {"provisioning", "active"}:
                return await ensure_human_device(identity, conn_svc)
            raise HumanBrokerProvisioningError("human credential changed during rotation")
        return await ensure_human_device(identity, conn_svc)
    finally:
        await revoke_member_connections(identity.owner_id, identity.subject)
