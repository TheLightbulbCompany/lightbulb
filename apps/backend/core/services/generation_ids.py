"""Generation-id minting helpers for the two onboarding/provisioning fences.

See ``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md``
§2 ("Two generation ids, each minted at exactly one code point"). Two
independently-scoped ULIDs fence different classes of facts so a
container-only recreation can never invalidate onboarding truth (and vice
versa):

- ``provision_generation`` (``containers`` row, PK ``owner_id``) -- the
  container-incarnation fence. Minted exactly once, at event **G1**, inside
  ``EcsManager.create_user_service``'s row-creating write. Preserved by
  scale-from-zero, force-redeploy, restart, provider hot-swap, Bedrock
  revoke/restore, and Stripe conversion -- see INV-2.
- ``onboarding_generation`` (future ``onboarding-state`` row, PK
  ``owner_id``) -- the company-incarnation fence. Minted exactly once, at
  event **G2**, inside ``paperclip_provisioning.provision_org``'s
  fresh-create branch -- NOT on reconcile-adopt of an existing
  ``company_id`` and NOT on ``provision_member`` (an org seat) -- see INV-3.

THIS MODULE IS INERT as of PR-3 (GEN): it only mints ids. No consumer reads
either generation yet, and the ``onboarding-state`` table does not exist yet
(it lands in the ONBOARD-TABLE PR). ``bump_onboarding_generation`` here is
just the id-minting primitive that PR will wire into a conditioned
``UpdateItem``.
"""

from ulid import ULID


def new_provision_generation() -> str:
    """Mint a fresh ``provision_generation`` ULID.

    Call this from EXACTLY ONE place: ``EcsManager.create_user_service``'s
    row-creating write (event G1). See INV-2. Every other containers-row
    write path must leave an existing ``provision_generation`` untouched.
    """
    return str(ULID())


def bump_onboarding_generation() -> str:
    """Mint a fresh ``onboarding_generation`` ULID.

    Call this from EXACTLY ONE place, once the ``onboarding-state`` table
    lands: ``provision_org``'s fresh-create branch (event G2), never on
    reconcile-adopt of an existing company nor on a seat-only
    ``provision_member``. See INV-3. Unused by any caller as of this PR
    (GEN) -- the table it will be stamped onto doesn't exist yet.
    """
    return str(ULID())
