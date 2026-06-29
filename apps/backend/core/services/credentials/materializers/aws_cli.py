"""Materializer for the AWS read-only STS rail: writes a 0600 ``~/.aws/config``
so the baked ``aws`` CLI assumes the owner's cross-account role. The ``[default]``
profile uses ``credential_source = EcsContainer`` (pull the task-role creds from
the ECS metadata endpoint, then AssumeRole into ``role_arn`` with ``external_id``).
``source_profile`` is deliberately absent — it is mutually exclusive with
``credential_source`` and the CLI rejects a profile that sets both."""

from __future__ import annotations

from core.services.credentials.materializers.base import atomic_write, owner_efs_root


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    path = owner_efs_root(owner_id) / ".aws" / "config"
    body = (
        "[default]\n"
        f"role_arn = {credential['role_arn']}\n"
        "credential_source = EcsContainer\n"
        f"external_id = {credential['external_id']}\n"
        f"region = {credential['region']}\n"
    )
    atomic_write(path, body.encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    (owner_efs_root(owner_id) / ".aws" / "config").unlink(missing_ok=True)
