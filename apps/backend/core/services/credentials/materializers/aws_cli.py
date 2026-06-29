"""Materializer for the AWS read-only STS rail: writes a 0600 ``~/.aws/config``
so the baked ``aws`` CLI assumes the owner's cross-account role. The profile uses
``credential_source = EcsContainer`` (pull the task-role creds from the ECS
metadata endpoint, then AssumeRole into ``role_arn`` with ``external_id``).
``source_profile`` is deliberately absent — it is mutually exclusive with
``credential_source`` and the CLI rejects a profile that sets both.

The profile is **named** (``[profile isol8-readonly]``), NOT ``[default]``, and
this is security-critical. A ``[default]`` profile in ``~/.aws/config`` would be
picked up by the AWS SDK's default credential chain — the same chain the Bedrock
provider uses inside the node process. That would silently make Bedrock inference
AssumeRole into the owner's read-only role and fail with AccessDenied (the
read-only role can't invoke Bedrock). Keeping the profile named makes the
read-only role strictly opt-in (``aws --profile isol8-readonly …`` /
``AWS_PROFILE=isol8-readonly``) and leaves Bedrock on the ECS task role."""

from __future__ import annotations

from core.services.credentials.materializers.base import atomic_write, owner_efs_root

AWS_READONLY_PROFILE = "isol8-readonly"


def write(owner_id: str, provider_key: str, credential: dict) -> None:
    path = owner_efs_root(owner_id) / ".aws" / "config"
    body = (
        f"[profile {AWS_READONLY_PROFILE}]\n"
        f"role_arn = {credential['role_arn']}\n"
        "credential_source = EcsContainer\n"
        f"external_id = {credential['external_id']}\n"
        f"region = {credential['region']}\n"
    )
    atomic_write(path, body.encode())


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    (owner_efs_root(owner_id) / ".aws" / "config").unlink(missing_ok=True)
