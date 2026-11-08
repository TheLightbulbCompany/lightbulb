"""Delete every Secrets Manager secret scoped to one owner.

The account wipe used to leave all of these behind, and the leftovers are the
credential material specifically: BYOK LLM keys, integration credentials, and
per-owner OAuth client apps. A "wiped" account whose Anthropic key and ChatGPT
tokens are still recoverable from Secrets Manager is not wiped.

Both live under owner-scoped path prefixes, which is what makes a sweep
possible without enumerating providers:

    lightbulb/{env}/user-keys/{owner_id}/{provider}              key_service
    lightbulb/{env}/integrations/{owner_id}/{provider}           integration_secret_store
    lightbulb/{env}/integrations/{owner_id}/oauth-app/{provider} oauth_app_store

Listing by prefix rather than iterating a provider registry is deliberate: a
provider that has since been removed from the registry still has secrets on
disk, and those are exactly the ones nobody would think to delete.

Integration credentials written under ``CREDENTIAL_KMS_KEY_ARN`` live in the
``credential-envelopes`` table instead; both sweeps cover that owner partition too and report
those rows as ``credential-envelopes/<owner>/<provider>``. The legacy secret such a write retired
sits scheduled for deletion, so listing includes planned deletions and the sweep force-deletes those
too.

`force_delete_without_recovery` because the point is removal, not a 30-day
soft-delete that keeps the plaintext retrievable and — more practically —
holds the name reserved so the same owner re-signing up collides with their
own tombstone.
"""

from __future__ import annotations

import logging

import boto3

from core.config import settings
from core.dynamodb import run_in_thread
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)


def owner_secret_prefixes(owner_id: str) -> list[str]:
    """Every Secrets Manager name prefix that belongs to this owner."""
    env = settings.ENVIRONMENT or "dev"
    return [
        f"lightbulb/{env}/user-keys/{owner_id}/",
        f"lightbulb/{env}/integrations/{owner_id}/",
    ]


def _list_names_with_prefix(sm, prefix: str) -> list[str]:
    """Secret names under ``prefix``.

    Filtered client-side on top of the API's own prefix filter: ListSecrets'
    "!" negation and prefix semantics have bitten us before, and the owner id
    is a path segment, so a server-side prefix match on a SHORTER owner id
    could otherwise sweep a longer one that starts with the same characters
    (u_1 vs u_10). The explicit startswith is the guard that makes that
    impossible.
    """
    names: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict = {
            "MaxResults": 100,
            "Filters": [{"Key": "name", "Values": [prefix]}],
            # A sealed put soft-deletes the legacy secret; a wipe must not leave it recoverable.
            "IncludePlannedDeletion": True,
        }
        if token:
            kwargs["NextToken"] = token
        page = sm.list_secrets(**kwargs)
        for s in page.get("SecretList", []):
            name = s.get("Name") or ""
            if name.startswith(prefix):
                names.append(name)
        token = page.get("NextToken")
        if not token:
            return names


def _envelope_name(owner_id: str, provider: str) -> str:
    return f"{integration_secret_store.ENVELOPE_TABLE}/{owner_id}/{provider}"


async def delete_owner_secrets(owner_id: str) -> list[str]:
    """Delete every secret under this owner's prefixes. Returns the names.

    Never raises for an individual secret — a single failure must not abort
    the sweep and strand the rest. The caller (the wipe orchestrator) records
    the step outcome; per-secret failures are logged and the name is omitted
    from the returned list so the report undercounts rather than lies.
    """

    def _sweep() -> list[str]:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        deleted: list[str] = []
        for prefix in owner_secret_prefixes(owner_id):
            for name in _list_names_with_prefix(sm, prefix):
                try:
                    integration_secret_store.force_delete_secret(sm, name)
                    deleted.append(name)
                except Exception:
                    logger.exception("owner_secret_sweep: failed to delete %s", name)
        for provider in integration_secret_store.envelope_providers(owner_id):
            name = _envelope_name(owner_id, provider)
            try:
                integration_secret_store.delete_envelope(owner_id, provider)
                deleted.append(name)
            except Exception:
                logger.exception("owner_secret_sweep: failed to delete %s", name)
        return deleted

    return await run_in_thread(_sweep)


async def list_owner_secret_names(owner_id: str) -> list[str]:
    """READ-ONLY counterpart of :func:`delete_owner_secrets`, for the wipe
    preview. Same prefix logic, no deletes — an admin has to be able to see
    what the wipe will take before committing to it."""

    def _list() -> list[str]:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        names: list[str] = []
        for prefix in owner_secret_prefixes(owner_id):
            names.extend(_list_names_with_prefix(sm, prefix))
        names += [_envelope_name(owner_id, p) for p in integration_secret_store.envelope_providers(owner_id)]
        return names

    return await run_in_thread(_list)


def _agent_key_prefix(owner_id: str) -> str:
    """Old per-agent Paperclip key secrets (no longer written). The wipes sweep them until the prefix is empty."""
    return f"lightbulb/{settings.ENVIRONMENT or 'dev'}/paperclip-agent-keys/{owner_id}/"


async def list_owner_agent_key_secret_names(owner_id: str) -> list[str]:
    """READ-ONLY: this owner's old per-agent key secret names, for the wipe preview."""
    return await run_in_thread(
        lambda: _list_names_with_prefix(
            boto3.client("secretsmanager", region_name=settings.AWS_REGION), _agent_key_prefix(owner_id)
        )
    )


async def delete_owner_agent_key_secrets(owner_id: str) -> int:
    """Force-delete this owner's old per-agent key secrets. Idempotent; returns the count."""
    names = await list_owner_agent_key_secret_names(owner_id)

    def _delete() -> None:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        for name in names:
            try:
                sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
            except sm.exceptions.ResourceNotFoundException:
                pass

    if names:
        await run_in_thread(_delete)
    return len(names)
