"""Paperclip Aurora (Better Auth) user delete — Admin Account-Wipe teardown
step 2 (docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md).

Paperclip exposes no user-delete/reset API, so the only way to remove a
member's Better Auth identity is a direct RDS Data API ``DELETE`` against the
``"user"`` row (cascades ``account``/``session`` via FK). This is the exact
RDS Data API wiring ``scripts/recover_orphan_paperclip_member.py`` uses for
the same DELETE (cluster ARN shape, secret lookup via paginated
``list_secrets``, ``database="paperclip"``), extracted here as a reusable
async helper so the wipe orchestrator (Task 3) can call it directly instead
of shelling out to a script.

Matched by id, not email: the wipe orchestrator already knows
``paperclip_user_id`` from the owner's ``paperclip-companies`` DDB row
(``PaperclipCompanyRow.paperclip_user_id``), so unlike the recovery script —
which starts from only an email and must disambiguate among possibly
multiple Aurora rows — this deletes directly by primary key. ``email`` is
still required as an argument so the bootstrap-admin refusal below can run
without a second lookup.
"""

from __future__ import annotations

import logging

from core.config import settings  # noqa: F401 -- kept for tests that patch mod.settings

logger = logging.getLogger(__name__)

# THE one definition. account_wipe and scripts/recover_orphan_paperclip_member
# import this — never re-declare the pair, or narrowing it later fixes only the
# copy you happened to edit.
#
# BOTH entries stay, and the second one is not redundant: the bootstrap admin's
# address differs PER ENVIRONMENT. Prod was renamed to the plus-addressed alias
# so the instance_admin role can't leak into admin@isol8.co's regular Isol8
# identity; dev still carries the bare address from before that rename (see
# auth-stack.ts's PaperclipAdminCredentials comment, and
# paperclip_admin_session's module docstring). The live email is in Secrets
# Manager, not here — this set is the union that is safe in every env, and
# deleting whichever one IS this env's bootstrap admin breaks the
# PaperclipAdminClient session every provision call depends on.
#
# The cost is known and accepted: in prod, admin@isol8.co is an ordinary org
# owner, so wiping THAT account needs a manual Aurora delete. Narrowing to the
# live per-env address means reading the secret from inside a delete guard —
# a read whose failure mode is "delete the bootstrap admin". Not worth it for
# a two-element allowlist.
# DO NOT RENAME / REWRITE these addresses: they are live Better Auth rows in
# Aurora and values inside Secrets Manager lightbulb/{env}/paperclip_admin_credentials —
# no repo edit changes either. Editing them here silently disarms the guard and
# the next admin wipe deletes the bootstrap admin, breaking provisioning fleet-wide.
# Every address the Paperclip bootstrap admin has ever been minted under.
#
# This guard is keyed on the EMAIL, but the address it has to match lives in the
# `paperclip_admin_credentials` secret -- so the secret moving to a new mail
# domain silently disarmed it. Verified against the live account 2026-08-28:
# dev's secret reads admin@thelightbulb.company and prod's reads
# admin+paperclip@thelightbulb.company, while this set listed only the isol8.co
# pair. An account wipe of the owning org would therefore have deleted the
# bootstrap admin that owns every Paperclip company -- a fleet-wide provisioning
# outage, which is precisely what this guard exists to prevent.
#
# Keep the retired addresses: the set is a protection list, not a source of
# truth, and an entry for an account that no longer exists costs nothing while a
# missing one is unrecoverable. If the address moves again, ADD to this set in
# the same change that rotates the secret.
BOOTSTRAP_EMAILS: frozenset[str] = frozenset(
    {
        "admin@isol8.co",
        "admin+paperclip@isol8.co",
        "admin@thelightbulb.company",
        "admin+paperclip@thelightbulb.company",
    }
)


class BootstrapUserProtectedError(Exception):
    """Raised when a wipe targets a bootstrap Paperclip admin identity."""


# Addressing lives in paperclip_sql, which this module is now the only consumer
# of; the private aliases are kept so the existing tests' patch targets work.
from core.services.paperclip_sql import (  # noqa: E402
    cluster_arn as _cluster_arn,
    find_secret_arn as _find_secret_arn,
    rds_data_client as _rds_data_client,
    secretsmanager_client as _secretsmanager_client,
)


async def delete_paperclip_user(*, paperclip_user_id: str, email: str, env: str) -> None:
    """RDS Data API ``DELETE FROM "user" WHERE id = :id`` (cascades
    account/session). Refuses — raises ``BootstrapUserProtectedError`` — for
    either bootstrap Paperclip admin email; see ``BOOTSTRAP_EMAILS``.

    Idempotent: deleting an already-gone row is a normal 0-row DELETE (RDS
    Data API does not error on that), so this is safe to re-run after a
    partial wipe failure. Any AWS/Data-API failure (throttling, bad
    credentials, etc.) propagates so the wipe orchestrator's per-step
    try/except can record it as failed.
    """
    if email.strip().lower() in BOOTSTRAP_EMAILS:
        raise BootstrapUserProtectedError(
            f"Refusing to delete Paperclip user {paperclip_user_id}: {email} is a bootstrap admin."
        )

    from core.dynamodb import run_in_thread

    sm = _secretsmanager_client()
    secret_arn = await run_in_thread(_find_secret_arn, sm, env)

    rds = _rds_data_client()
    await run_in_thread(
        rds.execute_statement,
        resourceArn=_cluster_arn(env),
        secretArn=secret_arn,
        database="paperclip",
        sql='DELETE FROM "user" WHERE id = :id',
        parameters=[{"name": "id", "value": {"stringValue": paperclip_user_id}}],
    )
