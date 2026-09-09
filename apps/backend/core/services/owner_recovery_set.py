"""Fail-closed seam for post-exit, pre-migration owner recovery sets.

The current runtime can restore Litestream's live replica prefix, but it has no
primitive that checkpoints every shared/agent WAL, snapshots the resulting
generations under an isolated prefix after clean gateway exit, and coherently
archives the matching EFS and Workshop state. A schema-forward release must not
turn ordinary live replicas into fabricated rollback evidence.
"""


class OwnerRecoverySetUnavailable(RuntimeError):
    pass


async def create_verified_owner_recovery_set(
    *,
    owner_id: str,
    update_id: str,
    source_task_arns: list[str],
    claim_id: str,
    release_revision: int,
    effect_claim_id: str,
) -> dict:
    """Fail closed until a fenced checkpoint/archive provider is installed."""

    del owner_id, update_id, source_task_arns, claim_id, release_revision, effect_claim_id
    raise OwnerRecoverySetUnavailable(
        "no coherent post-exit Litestream/EFS/Workshop recovery-set provider is installed"
    )
